"""First-order / P-W optical initial structure synthesis and system scaling services."""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from zemax_mcp.core.snapshots import SnapshotManager
from zemax_mcp.core.zos_compat import _get_primary_wavelength_index, get_zospy
from zemax_mcp.schemas.optical import (
    ScaleSystemRequest,
    SurfaceItem,
    SynthesizeDesignRequest,
    SystemBuildRequest,
)
from zemax_mcp.services import optical_service

# 常用光学玻璃 d 光折射率与阿贝数内置表
GLASS_CATALOG: Dict[str, Tuple[float, float]] = {
    "N-BK7": (1.5168, 64.17),
    "N-F2": (1.6200, 36.37),
    "N-LAK9": (1.6910, 54.71),
    "N-SK16": (1.6204, 60.32),
    "N-SF11": (1.7847, 25.76),
    "N-SF57": (1.8467, 23.80),
    "N-LAF2": (1.7440, 44.77),
    "FUSED SILICA": (1.4585, 67.82),
    "SILICA": (1.4585, 67.82),
}


def get_glass_props(material: str) -> Tuple[float, float]:
    """返回指定玻璃的 (nd, vd)，未知牌号回退到标准冕牌近似 (1.52, 58.0)。"""
    key = material.strip().upper()
    return GLASS_CATALOG.get(key, (1.52, 58.0))


def _sag(radius: float, half_ap: float) -> float:
    if radius == 0.0:
        return 0.0
    r_abs = abs(radius)
    h = min(half_ap, 0.92 * r_abs)
    return r_abs - math.sqrt(max(r_abs * r_abs - h * h, 0.0))


def safe_element_thickness(r1: float, r2: float, half_ap: float, edge_t: float) -> float:
    """根据正负曲率矢高计算保证安全边缘厚度的中心厚度。"""
    sag1 = _sag(r1, half_ap) if r1 > 0 else 0.0
    sag2 = _sag(r2, half_ap) if r2 < 0 else 0.0
    return round(max(2.5, sag1 + sag2 + edge_t), 2)


def _c_to_r(c: float) -> float:
    return 0.0 if abs(c) < 1e-9 else round(1.0 / c, 4)


def _clamp_radius_pair(c1: float, c2: float, phi: float, n: float, min_r: float) -> Tuple[float, float]:
    """若任一曲率半径过小 (< min_r)，平缓缩放保持总光焦度 phi 不变以防止 TIR。"""
    r1 = _c_to_r(c1)
    r2 = _c_to_r(c2)
    if (r1 != 0.0 and abs(r1) < min_r) or (r2 != 0.0 and abs(r2) < min_r):
        # 回退到对称/平缓弯曲
        c_sym = phi / (2.0 * (n - 1.0))
        if abs(c_sym) > 1e-9 and (1.0 / abs(c_sym)) < min_r:
            c_sym = math.copysign(1.0 / min_r, c_sym)
        return c_sym, -c_sym
    return c1, c2


def synthesize_initial_design(
    req: SynthesizeDesignRequest,
    oss: Any = None,
    zp_mod: Any = None,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> Dict[str, Any]:
    """基于 Coddington / P-W 薄透镜初阶理论生成无 TIR 初始结构，并可选立即构建。"""
    topo = req.topology.strip().lower()
    f = float(req.target_effl)
    epd = float(req.epd)
    half_ap = epd / 2.0
    min_r = 1.35 * epd
    phi_tot = 1.0 / f

    surfaces: List[SurfaceItem] = []
    default_preset = "HeNe_0p6328"
    theory_notes: Dict[str, Any] = {}

    if topo == "singlet_min_spha":
        glass = (req.glasses[0] if req.glasses else "N-BK7").upper().strip()
        nd, vd = get_glass_props(glass)
        # 无穷远物距 p = -1 下的 Coddington 最小球差形状因子
        q = -2.0 * (nd * nd - 1.0) / (nd + 2.0)
        c1 = phi_tot * (q + 1.0) / (2.0 * (nd - 1.0))
        c2 = phi_tot * (q - 1.0) / (2.0 * (nd - 1.0))
        c1, c2 = _clamp_radius_pair(c1, c2, phi_tot, nd, min_r)
        r1, r2 = _c_to_r(c1), _c_to_r(c2)
        d1 = safe_element_thickness(r1, r2, half_ap, req.edge_thickness)
        bfl = round(max(1.0, f - d1 * 0.65), 3)
        surfaces = [
            SurfaceItem(
                comment=f"{glass}_Front",
                radius=r1,
                thickness=d1,
                material=glass,
                is_stop=True,
                radius_variable=True,
            ),
            SurfaceItem(
                comment=f"{glass}_Back",
                radius=r2,
                thickness=bfl,
                material="",
                radius_variable=True,
                thickness_variable=True,
            ),
        ]
        theory_notes = {
            "method": "Coddington minimum spherical aberration shape factor",
            "glass": glass,
            "nd": nd,
            "shape_factor_q": round(q, 4),
        }

    elif topo == "achromatic_doublet":
        default_preset = "FdC_Visible"
        g1 = (req.glasses[0] if req.glasses and len(req.glasses) >= 1 else "N-BK7").upper().strip()
        g2 = (req.glasses[1] if req.glasses and len(req.glasses) >= 2 else "N-F2").upper().strip()
        n1, v1 = get_glass_props(g1)
        n2, v2 = get_glass_props(g2)
        if abs(v1 - v2) < 1e-6:
            return {"status": "error", "message": f"双胶合两种玻璃阿贝数相同 ({v1})，无法消色差"}
        phi1 = phi_tot * v1 / (v1 - v2)
        phi2 = phi_tot - phi1
        # P-W 消球差胶合双镜初算：正透镜对称或微弯，胶合面匹配负光焦度
        c1 = phi1 / (2.0 * (n1 - 1.0))
        c2 = c1 - phi1 / (n1 - 1.0)
        c3 = c2 - phi2 / (n2 - 1.0)
        r1, r2, r3 = _c_to_r(c1), _c_to_r(c2), _c_to_r(c3)
        d1 = safe_element_thickness(r1, r2, half_ap, req.edge_thickness)
        d2 = round(max(2.5, req.edge_thickness + 1.0), 2)
        bfl = round(max(1.0, f - (d1 + d2) * 0.6), 3)
        surfaces = [
            SurfaceItem(comment=f"{g1}_CrownFront", radius=r1, thickness=d1, material=g1, is_stop=True, radius_variable=True),
            SurfaceItem(comment="CementedInterface", radius=r2, thickness=d2, material=g2, radius_variable=True),
            SurfaceItem(comment=f"{g2}_FlintBack", radius=r3, thickness=bfl, material="", radius_variable=True, thickness_variable=True),
        ]
        theory_notes = {
            "method": "Thin-lens achromatic power split + cemented P-W bending",
            "crown": {"glass": g1, "nd": n1, "vd": v1, "power_ratio": round(phi1 / phi_tot, 4)},
            "flint": {"glass": g2, "nd": n2, "vd": v2, "power_ratio": round(phi2 / phi_tot, 4)},
        }

    elif topo == "cooke_triplet":
        default_preset = "FdC_Visible"
        g1 = (req.glasses[0] if req.glasses and len(req.glasses) >= 1 else "N-SK16").upper().strip()
        g2 = (req.glasses[1] if req.glasses and len(req.glasses) >= 2 else "N-F2").upper().strip()
        g3 = (req.glasses[2] if req.glasses and len(req.glasses) >= 3 else g1).upper().strip()
        n1, v1 = get_glass_props(g1)
        n2, v2 = get_glass_props(g2)
        n3, v3 = get_glass_props(g3)
        # 经典库克三片式光焦度分配与空气间隔初值
        phi1 = 1.28 * phi_tot
        phi2 = -1.90 * phi_tot
        phi3 = 1.12 * phi_tot
        d_air1 = round(0.14 * f, 3)
        d_air2 = round(0.16 * f, 3)
        bfl = round(0.72 * f, 3)

        # L1: 平缓前凸后平弯 (c1 = 0.85 * phi1/(n1-1))
        c1 = 0.85 * phi1 / (n1 - 1.0)
        c2 = c1 - phi1 / (n1 - 1.0)
        # L2: 双凹负透镜
        c3 = phi2 / (2.0 * (n2 - 1.0))
        c4 = -c3
        # L3: 后正透镜
        c5 = 0.25 * phi3 / (n3 - 1.0)
        c6 = c5 - phi3 / (n3 - 1.0)

        r1, r2 = _c_to_r(c1), _c_to_r(c2)
        r3, r4 = _c_to_r(c3), _c_to_r(c4)
        r5, r6 = _c_to_r(c5), _c_to_r(c6)
        t1 = safe_element_thickness(r1, r2, half_ap, req.edge_thickness)
        t2 = round(max(2.0, req.edge_thickness + 0.8), 2)
        t3 = safe_element_thickness(r5, r6, half_ap * 0.8, req.edge_thickness)

        surfaces = [
            SurfaceItem(comment="L1_Front", radius=r1, thickness=t1, material=g1, radius_variable=True),
            SurfaceItem(comment="L1_Back", radius=r2, thickness=d_air1, material="", radius_variable=True, thickness_variable=True),
            SurfaceItem(comment="L2_FlintFront_Stop", radius=r3, thickness=t2, material=g2, is_stop=True, radius_variable=True),
            SurfaceItem(comment="L2_FlintBack", radius=r4, thickness=d_air2, material="", radius_variable=True, thickness_variable=True),
            SurfaceItem(comment="L3_Front", radius=r5, thickness=t3, material=g3, radius_variable=True),
            SurfaceItem(comment="L3_Back", radius=r6, thickness=bfl, material="", radius_variable=True, thickness_variable=True),
        ]
        theory_notes = {
            "method": "Cooke Triplet first-order power & spacing distribution",
            "glasses": [g1, g2, g3],
            "power_ratios": [1.28, -1.90, 1.12],
        }
    else:
        return {
            "status": "error",
            "message": f"未知拓扑类型 '{req.topology}'，仅支持 'singlet_min_spha', 'achromatic_doublet', 'cooke_triplet'",
        }

    build_req = SystemBuildRequest(
        epd=epd,
        fovs=req.fovs,
        afocal_mode=False,
        surfaces=surfaces,
        wavelength_preset=req.wavelength_preset or default_preset,
    )

    out: Dict[str, Any] = {
        "status": "ok",
        "topology": topo,
        "theory_notes": theory_notes,
        "synthesized_build_config": build_req.model_dump(exclude_none=True),
        "applied_to_system": False,
    }
    if req.apply_to_system and oss is not None:
        build_res = optical_service.build_system(
            oss, build_req, zp_mod=zp_mod, snapshot_manager=snapshot_manager
        )
        if build_res.get("status") != "ok":
            return build_res
        out["applied_to_system"] = True
        out["data"] = build_res.get("data")
    return out


def scale_system(
    oss: Any,
    req: ScaleSystemRequest,
    zp_mod: Any = None,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> Dict[str, Any]:
    """按倍率或目标焦距整体等比缩放当前光学系统（用于 Samples 快速适配指标）。"""
    zp = zp_mod or get_zospy()
    if snapshot_manager is not None:
        try:
            snapshot_manager.create(oss, label="auto:before_scale")
        except Exception:
            pass

    factor = req.scale_factor
    if factor is None and req.target_effl is not None:
        prim_wl = _get_primary_wavelength_index(oss)
        op_effl = zp.constants.Editors.MFE.MeritOperandType.EFFL
        curr_effl = float(oss.MFE.GetOperandValue(op_effl, 0, prim_wl, 0, 0.0, 0.0, 0.0, 0.0, 0.0))
        if not math.isfinite(curr_effl) or abs(curr_effl) < 1e-6 or abs(curr_effl) > 1e6:
            return {"status": "error", "message": f"当前系统焦距无效 ({curr_effl})，无法按目标焦距缩放"}
        factor = float(req.target_effl) / curr_effl

    if factor is None or not math.isfinite(factor) or factor <= 0:
        return {"status": "error", "message": f"无效的缩放倍率: {factor}"}

    lde = oss.LDE
    scaled_via_tool = False
    open_scale = getattr(oss.Tools, "OpenScale", None)
    if callable(open_scale):
        tool = open_scale()
        if tool is not None:
            try:
                if hasattr(tool, "ScaleByFactor"):
                    tool.ScaleByFactor = True
                if hasattr(tool, "ScaleFactor"):
                    tool.ScaleFactor = float(factor)
                if hasattr(tool, "FirstComponent"):
                    tool.FirstComponent = 1
                if hasattr(tool, "LastComponent"):
                    tool.LastComponent = max(1, lde.NumberOfSurfaces - 1)
                tool.RunAndWaitForCompletion()
                if hasattr(tool, "Succeeded") and not tool.Succeeded:
                    raise RuntimeError(str(getattr(tool, "ErrorMessage", "缩放工具运行失败")))
                scaled_via_tool = True
            finally:
                tool.Close()

    if not scaled_via_tool:
        # 兼容缩放兜底：缩放各表面曲率半径、有限厚度与入瞳口径
        for i in range(1, lde.NumberOfSurfaces - 1):
            s = lde.GetSurfaceAt(i)
            if s.Radius != 0.0 and math.isfinite(s.Radius):
                s.Radius = float(s.Radius) * factor
            if math.isfinite(s.Thickness):
                s.Thickness = float(s.Thickness) * factor
        ap = oss.SystemData.Aperture
        if hasattr(ap, "ApertureValue") and isinstance(ap.ApertureValue, (int, float)):
            ap.ApertureValue = float(ap.ApertureValue) * factor

    return {
        "status": "ok",
        "scale_factor": round(float(factor), 6),
        "scaled_via_native_tool": scaled_via_tool,
        "data": optical_service.extract_system_state(oss, zp_mod=zp),
    }
