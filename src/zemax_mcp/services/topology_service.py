"""Atomic optical topology evolution services: safe Lens Splitting & Progressive Asphere Promotion."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

from zemax_mcp.core.snapshots import SnapshotManager
from zemax_mcp.core.zos_compat import (
    _change_surface_type,
    _set_asphere_terms,
    _set_asphere_variables,
    _set_variable,
    get_zospy,
)
from zemax_mcp.schemas.optical import OptimizeRequest, PromoteAsphereRequest, SplitLensRequest
from zemax_mcp.services import optical_service
from zemax_mcp.services.synthesis_service import _c_to_r, get_glass_props, safe_element_thickness


def promote_asphere(
    oss: Any,
    req: PromoteAsphereRequest,
    zp_mod: Any = None,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> Dict[str, Any]:
    """渐进式非球面升级：先快照 → 转 EvenAsphere → 仅释放 Conic 预优 → 再按 max_order 释放高次项 + MNEG。"""
    zp = zp_mod or get_zospy()
    lde = oss.LDE
    if req.surface_index < 1 or req.surface_index >= lde.NumberOfSurfaces - 1:
        return {"status": "error", "message": f"表面序号 {req.surface_index} 超出可编辑范围"}

    snap = snapshot_manager.create(oss, label="auto:before_promote_asphere") if snapshot_manager else None
    try:
        surf = lde.GetSurfaceAt(req.surface_index)
        type_name = str(getattr(surf, "TypeName", "") or "")
        if "aspher" not in type_name.lower():
            _change_surface_type(surf, "EvenAsphere", zp_mod=zp)
        # Stage 1 暂时锁定基圆曲率半径（防止 R->Inf 与 Conic->1e5 简并耦合），仅释放 Conic 预优
        orig_r_solve = getattr(getattr(surf, "RadiusCell", None), "Solve", None)
        var_solve = getattr(getattr(getattr(zp.constants, "Editors", None), "SolveType", None), "Variable", None)
        was_r_var = orig_r_solve == var_solve if var_solve is not None else False
        if hasattr(surf, "RadiusCell"):
            _set_variable(surf.RadiusCell, False, zp_mod=zp)
        _set_variable(surf.ConicCell, True, zp_mod=zp)

        opt1 = optical_service.run_optimization(
            oss,
            OptimizeRequest(
                use_wavefront=req.use_wavefront,
                target_effl=req.target_effl,
                min_edge_thickness=req.min_edge_thickness,
            ),
            zp_mod=zp,
            snapshot_manager=None,
        )
        if opt1.get("status") != "ok":
            raise RuntimeError(opt1.get("message", "Stage-1 Conic 预优化失败"))

        # Stage 2 恢复原曲率变量并渐进释放高次非球面项
        if was_r_var and hasattr(surf, "RadiusCell"):
            _set_variable(surf.RadiusCell, True, zp_mod=zp)

        orders = [o for o in (4, 6, 8) if o <= req.max_order]
        _set_asphere_terms(surf, {o: 0.0 for o in orders}, zp_mod=zp)
        _set_asphere_variables(surf, {o: True for o in orders}, zp_mod=zp)

        opt2 = optical_service.run_optimization(
            oss,
            OptimizeRequest(
                use_wavefront=req.use_wavefront,
                target_effl=req.target_effl,
                min_edge_thickness=req.min_edge_thickness,
            ),
            zp_mod=zp,
            snapshot_manager=None,
        )
        if opt2.get("status") != "ok":
            raise RuntimeError(opt2.get("message", "Stage-2 高次非球面优化失败"))

        conic_val = float(getattr(surf, "Conic", 0.0) or 0.0)
        if not math.isfinite(conic_val) or abs(conic_val) > 50.0:
            raise RuntimeError(f"非球面 Conic 发散为病态值 ({conic_val:.2f})，已触发防退化回滚")

        return {
            "status": "ok",
            "surface_index": req.surface_index,
            "released_orders": orders,
            "stage1_conic_merit": opt1.get("final_merit"),
            "stage2_asphere_merit": opt2.get("final_merit"),
            "achieved_constraints": opt2.get("achieved_constraints"),
            "data": opt2.get("data"),
        }
    except Exception as exc:
        rolled_back = False
        if snapshot_manager is not None and snap is not None:
            try:
                snapshot_manager.undo(oss)
                rolled_back = True
            except Exception:
                pass
        return {
            "status": "error",
            "message": f"非球面渐进升级失败: {exc}",
            "auto_rolled_back": rolled_back,
        }


def split_lens(
    oss: Any,
    req: SplitLensRequest,
    zp_mod: Any = None,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> Dict[str, Any]:
    """原子级安全透镜分裂：保持总光焦度将单片强弯曲透镜拆为两片同材料透镜，失败自动回滚快照。"""
    zp = zp_mod or get_zospy()
    lde = oss.LDE
    s_idx = req.surface_index
    if s_idx < 1 or s_idx + 1 >= lde.NumberOfSurfaces - 1:
        return {"status": "error", "message": f"表面序号 {s_idx} 无法构成完整单片透镜（缺少后表面）"}

    s_front = lde.GetSurfaceAt(s_idx)
    s_back = lde.GetSurfaceAt(s_idx + 1)
    mat = str(getattr(s_front, "Material", "") or "").strip().upper()
    if not mat:
        return {"status": "error", "message": f"表面 {s_idx} 不含玻璃材料，无法执行透镜分裂"}

    snap = snapshot_manager.create(oss, label="auto:before_split_lens") if snapshot_manager else None
    try:
        r1 = float(s_front.Radius or 0.0)
        r2 = float(s_back.Radius or 0.0)
        t_back_air = float(s_back.Thickness or 0.0)
        sd = max(float(getattr(s_front, "SemiDiameter", 0.0) or 0.0), float(getattr(s_back, "SemiDiameter", 0.0) or 0.0), 10.0)

        nd, _ = get_glass_props(mat)
        c1_orig = 0.0 if r1 == 0.0 else 1.0 / r1
        c2_orig = 0.0 if r2 == 0.0 else 1.0 / r2
        phi_orig = (nd - 1.0) * (c1_orig - c2_orig)
        if abs(phi_orig) < 1e-7:
            raise ValueError("当前透镜光焦度接近零，无须分裂")

        phi_a = req.split_ratio * phi_orig
        phi_b = (1.0 - req.split_ratio) * phi_orig
        min_r = 1.25 * sd

        def _symmetric_radii(phi_part: float) -> tuple[float, float]:
            c_sym = phi_part / (2.0 * (nd - 1.0))
            if abs(c_sym) > 1e-9 and (1.0 / abs(c_sym)) < min_r:
                c_sym = math.copysign(1.0 / min_r, c_sym)
            return _c_to_r(c_sym), _c_to_r(-c_sym)

        ra1, ra2 = _symmetric_radii(phi_a)
        rb1, rb2 = _symmetric_radii(phi_b)
        ta = safe_element_thickness(ra1, ra2, sd, req.min_edge_thickness)
        tb = safe_element_thickness(rb1, rb2, sd, req.min_edge_thickness)

        # 更新前两面为透镜 A，再插入 s_idx+2 和 s_idx+3 作为透镜 B
        s_front.Radius = ra1
        s_front.Thickness = ta
        s_front.Material = mat
        _set_variable(s_front.RadiusCell, True, zp_mod=zp)

        s_back.Radius = ra2
        s_back.Thickness = float(req.air_gap)
        s_back.Material = ""
        _set_variable(s_back.RadiusCell, True, zp_mod=zp)
        _set_variable(s_back.ThicknessCell, True, zp_mod=zp)

        sb_front = lde.InsertNewSurfaceAt(s_idx + 2)
        sb_front.Comment = f"{mat}_SplitB_Front"
        sb_front.Radius = rb1
        sb_front.Thickness = tb
        sb_front.Material = mat
        _set_variable(sb_front.RadiusCell, True, zp_mod=zp)

        sb_back = lde.InsertNewSurfaceAt(s_idx + 3)
        sb_back.Comment = f"{mat}_SplitB_Back"
        sb_back.Radius = rb2
        sb_back.Thickness = max(1.0, round(t_back_air - req.air_gap - tb * 0.5, 4))
        sb_back.Material = ""
        _set_variable(sb_back.RadiusCell, True, zp_mod=zp)
        _set_variable(sb_back.ThicknessCell, True, zp_mod=zp)

        state = optical_service.extract_system_state(oss, zp_mod=zp)
        if not state.get("ray_trace_valid", False):
            raise RuntimeError("分裂后光线追迹失效 (TIR 或切光)")

        return {
            "status": "ok",
            "split_surface_index": s_idx,
            "material": mat,
            "original_power_mm_inv": round(phi_orig, 6),
            "element_a": {"radius_front": ra1, "radius_back": ra2, "thickness": ta, "power_ratio": req.split_ratio},
            "element_b": {"radius_front": rb1, "radius_back": rb2, "thickness": tb, "power_ratio": round(1.0 - req.split_ratio, 4)},
            "data": state,
        }
    except Exception as exc:
        rolled_back = False
        if snapshot_manager is not None and snap is not None:
            try:
                snapshot_manager.undo(oss)
                rolled_back = True
            except Exception:
                pass
        return {
            "status": "error",
            "message": f"透镜分裂失败: {exc}",
            "auto_rolled_back": rolled_back,
        }
