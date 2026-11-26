"""Optical domain services decoupled from HTTP and MCP transports."""

from __future__ import annotations

import math
import os
import traceback
from typing import Any, Dict, List, Optional

from zemax_mcp.core.security import validate_file_path
from zemax_mcp.core.snapshots import SnapshotManager
from zemax_mcp.core.zos_compat import (
    _change_surface_type,
    _extract_asphere_info,
    _get_normalized_field_coordinates,
    _get_primary_wavelength_index,
    _get_rms_spot_radii,
    _insert_operand_row,
    _json_safe_state,
    _set_asphere_terms,
    _set_asphere_variables,
    _set_marginal_ray_height_solve,
    _set_material_substitute,
    _set_variable,
    get_zospy,
)
from zemax_mcp.schemas.optical import (
    EvaluateRequest,
    FileRequest,
    HammerOptimizeRequest,
    OptimizeRequest,
    ParameterTuneRequest,
    SurfacePatchRequest,
    SystemBuildRequest,
)


def analyze_sweep_trend(
    results: List[Dict[str, Any]],
    best_candidate: Optional[Dict[str, Any]],
    min_val: float,
    max_val: float,
) -> Dict[str, Any]:
    """分析调参扫描结果的灵敏度趋势并给出下一步推荐区间。"""
    valid_points = [
        r for r in results if r.get("ray_trace_valid") and r.get("metric_value") is not None
    ]
    valid_count = len(valid_points)

    if valid_count == 0 or best_candidate is None:
        return {
            "direction": "all_invalid",
            "summary": "所有扫描采样点光线追迹均失败 (全反射或严重切光)，无法形成有效像质评估。建议反转曲率方向或调整扫描区间。",
            "recommended_range": None,
        }

    best_val = best_candidate["value"]
    best_metric = best_candidate["metric_value"]

    if valid_count == 1:
        step_delta = abs(max_val - min_val) * 0.1
        return {
            "direction": "single_valid_point",
            "summary": f"仅在参数值 {best_val} 处光线追迹有效 (指标: {best_metric})，其余采样点均失效。建议以此点为中心缩小范围精细扫描。",
            "recommended_range": [
                round(best_val - step_delta, 4),
                round(best_val + step_delta, 4),
            ],
        }

    best_idx = None
    for idx, r in enumerate(results):
        if r["value"] == best_val:
            best_idx = idx
            break

    n = len(results)
    step_size = abs(max_val - min_val) / max(n - 1, 1)

    if best_idx == 0:
        rec_min = min_val - step_size * 2.0
        rec_max = min_val + step_size
        return {
            "direction": "monotonic_boundary_min",
            "summary": f"像质在起始边界 {best_val} 处最优 (指标: {best_metric})，呈现向外侧单调改善趋势。建议向该方向扩展扫描范围。",
            "recommended_range": [
                round(min(rec_min, rec_max), 4),
                round(max(rec_min, rec_max), 4),
            ],
        }
    elif best_idx == n - 1:
        rec_min = max_val - step_size
        rec_max = max_val + step_size * 2.0
        return {
            "direction": "monotonic_boundary_max",
            "summary": f"像质在终止边界 {best_val} 处最优 (指标: {best_metric})，呈现向外侧单调改善趋势。建议向该方向扩展扫描范围。",
            "recommended_range": [
                round(min(rec_min, rec_max), 4),
                round(max(rec_min, rec_max), 4),
            ],
        }
    else:
        prev_val = results[best_idx - 1]["value"]
        next_val = results[best_idx + 1]["value"]
        return {
            "direction": "interior_minimum",
            "summary": f"在参数值 {best_val} 处发现明确的像质最优极值点 (指标: {best_metric})，两侧像质均退化。此区域具备良好的局部收敛特征。",
            "recommended_range": [
                round(min(prev_val, next_val), 4),
                round(max(prev_val, next_val), 4),
            ],
        }


def extract_system_state(oss: Any, zp_mod: Any = None) -> Dict[str, Any]:
    """安全提取当前镜头表面与光路健康度，绝不抛未捕获异常。"""
    zp = zp_mod or get_zospy()
    lde = oss.LDE
    surfaces_info = []
    for i in range(lde.NumberOfSurfaces):
        s = lde.GetSurfaceAt(i)
        try:
            r, t, m, sd = s.Radius, s.Thickness, s.Material, s.SemiDiameter
        except Exception:
            r, t, m, sd = 0.0, 0.0, "", 0.0
        surf_dict = {
            "index": i,
            "comment": s.Comment,
            "is_stop": s.IsStop,
            "radius": round(float(r), 4) if r is not None else 0.0,
            "thickness": round(float(t), 4) if t is not None else 0.0,
            "material": str(m) if m else "",
            "semi_diameter": round(float(sd), 4) if sd is not None else 0.0,
        }
        surf_dict.update(_extract_asphere_info(s, zp_mod=zp))
        surfaces_info.append(surf_dict)

    merit_val, rms_spot, ray_trace_ok = None, None, True
    try:
        rms_spot = _get_rms_spot_radii(oss)
    except Exception:
        ray_trace_ok = False

    try:
        merit_val = float(oss.MFE.CalculateMeritFunction())
        if merit_val > 1e6:
            ray_trace_ok = False
    except Exception:
        pass

    geo_spot_radius_um = None
    geo_spot_diameter_um = None
    if ray_trace_ok:
        try:
            prim_wl = _get_primary_wavelength_index(oss)
            op_trac = zp.constants.Editors.MFE.MeritOperandType.TRAC
            py_samples = [0.0, 0.1, 0.3, 0.5, 0.7, 0.85, 0.9, 0.95, 1.0]
            max_trac = 0.0
            for py in py_samples:
                val = abs(
                    float(
                        oss.MFE.GetOperandValue(
                            op_trac, prim_wl, 0, 0.0, 0.0, 0.0, float(py), 0.0, 0.0
                        )
                    )
                )
                if math.isfinite(val) and val > max_trac:
                    max_trac = val
            geo_spot_radius_um = round(max_trac * 1000.0, 4)
            geo_spot_diameter_um = round(geo_spot_radius_um * 2.0, 4)
        except Exception:
            pass

    return _json_safe_state(
        {
            "num_surfaces": lde.NumberOfSurfaces,
            "ray_trace_valid": ray_trace_ok,
            "rms_spot_radius": rms_spot,
            "geo_spot_radius_um": geo_spot_radius_um,
            "geo_spot_diameter_um": geo_spot_diameter_um,
            "merit_function": merit_val,
            "surfaces": surfaces_info,
        }
    )


def get_status(oss: Any, zp_mod: Any = None) -> Dict[str, Any]:
    return {"status": "ok", "data": extract_system_state(oss, zp_mod=zp_mod)}


def load_zmx_file(
    oss: Any,
    req: FileRequest,
    zp_mod: Any = None,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> Dict[str, Any]:
    validated = validate_file_path(req.filepath, mode="read", allowed_extensions=(".zmx",))
    abs_path = os.path.abspath(str(validated))
    if not os.path.exists(abs_path):
        return {"status": "error", "message": f"找不到文件: {abs_path}"}
    if snapshot_manager is not None:
        try:
            snapshot_manager.create(oss, label="auto:before_load")
        except Exception:
            pass
    oss.load(abs_path)
    return {
        "status": "ok",
        "loaded_file": abs_path,
        "data": extract_system_state(oss, zp_mod=zp_mod),
    }


def save_zmx_file(oss: Any, req: FileRequest) -> Dict[str, Any]:
    validated = validate_file_path(req.filepath, mode="write", allowed_extensions=(".zmx",))
    abs_path = os.path.abspath(str(validated))
    parent = os.path.dirname(abs_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    oss.save_as(abs_path)
    return {"status": "ok", "saved_to": abs_path}


def build_system(
    oss: Any,
    req: SystemBuildRequest,
    zp_mod: Any = None,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> Dict[str, Any]:
    zp = zp_mod or get_zospy()
    if snapshot_manager is not None:
        try:
            snapshot_manager.create(oss, label="auto:before_build")
        except Exception:
            pass
    oss.new()
    ap_enum = zp.constants.SystemData.ZemaxApertureType
    ap_name = getattr(req, "aperture_type", "EntrancePupilDiameter")
    ap_attr_map = {
        "entrancepupildiameter": "EntrancePupilDiameter",
        "imagespacefnum": "ImageSpaceFNum",
        "objectspacena": "ObjectSpaceNA",
        "floatbystopsize": "FloatByStopSize",
    }
    ap_key = ap_name.strip().lower()
    if ap_key not in ap_attr_map:
        return {"status": "error", "message": f"未知的孔径类型: {ap_name}"}
    ap_val_enum = getattr(ap_enum, ap_attr_map[ap_key], None)
    if ap_val_enum is None:
        return {"status": "error", "message": f"当前 ZOS-API 不支持孔径类型: {ap_name}"}
    oss.SystemData.Aperture.ApertureType = ap_val_enum
    ap_val = getattr(req, "aperture_value", None)
    if ap_val is None:
        ap_val = req.epd
    if ap_key != "floatbystopsize" and ap_val is not None:
        oss.SystemData.Aperture.ApertureValue = float(ap_val)
    oss.SystemData.Aperture.AfocalImageSpace = bool(req.afocal_mode)

    custom_wls = getattr(req, "wavelengths", None)
    if custom_wls:
        wl_sys = oss.SystemData.Wavelengths
        while wl_sys.NumberOfWavelengths > 1:
            wl_sys.RemoveWavelength(wl_sys.NumberOfWavelengths)
        wl_sys.GetWavelength(1).Wavelength = float(custom_wls[0])
        for wl_um in custom_wls[1:]:
            wl_sys.AddWavelength(float(wl_um), 1.0)
        prim_idx = getattr(req, "primary_wavelength_index", 1)
        wl_sys.GetWavelength(int(prim_idx)).MakePrimary()
    else:
        preset_name = req.wavelength_preset
        preset_enum = getattr(zp.constants.SystemData.WavelengthPreset, preset_name, None)
        if preset_enum is not None:
            oss.SystemData.Wavelengths.SelectWavelengthPreset(preset_enum)
        else:
            return {"status": "error", "message": f"未知的波长预设: {preset_name}"}

    fields = oss.SystemData.Fields
    fields.DeleteAllFields()
    for idx, fov in enumerate(req.fovs):
        if isinstance(fov, (int, float)):
            fx, fy, fw = 0.0, float(fov), 1.0
        else:
            fx, fy, fw = float(fov.x), float(fov.y), float(fov.weight)
        if idx == 0:
            f1 = fields.GetField(1)
            f1.X = fx
            f1.Y = fy
            f1.Weight = fw
        else:
            fields.AddField(fx, fy, fw)

    lde = oss.LDE
    while lde.NumberOfSurfaces > 3:
        lde.RemoveSurfaceAt(1)

    for idx, item in enumerate(req.surfaces, start=1):
        surf = lde.GetSurfaceAt(1) if idx == 1 else lde.InsertNewSurfaceAt(idx)
        surf_type = getattr(item, "surface_type", "Standard")
        if surf_type and surf_type.strip().lower() != "standard":
            _change_surface_type(surf, surf_type, zp_mod=zp)
        surf.Comment = item.comment
        surf.Radius = float(item.radius)
        surf.Thickness = float(item.thickness)
        surf.Material = item.material.upper().strip()
        conic_val = getattr(item, "conic", 0.0)
        if conic_val != 0.0:
            surf.Conic = float(conic_val)
        if item.semi_diameter is not None and item.semi_diameter > 0:
            surf.SemiDiameter = float(item.semi_diameter)
        if item.is_stop:
            surf.IsStop = True
        _set_variable(surf.RadiusCell, item.radius_variable, zp_mod=zp)
        if item.marginal_ray_height_solve:
            _set_marginal_ray_height_solve(surf.ThicknessCell, 0.0, 0.0, zp_mod=zp)
        else:
            _set_variable(surf.ThicknessCell, item.thickness_variable, zp_mod=zp)
        if getattr(item, "conic_variable", False):
            _set_variable(surf.ConicCell, True, zp_mod=zp)
        if getattr(item, "material_substitute", False):
            _set_material_substitute(
                surf.MaterialCell, True, getattr(item, "substitute_catalog", "SCHOTT"), zp_mod=zp
            )
        asphere_terms = getattr(item, "asphere_terms", None)
        if asphere_terms:
            _set_asphere_terms(surf, asphere_terms, zp_mod=zp)
        asphere_vars = getattr(item, "asphere_terms_variable", None)
        if asphere_vars:
            _set_asphere_variables(surf, {int(k): True for k in asphere_vars}, zp_mod=zp)

    return {"status": "ok", "data": extract_system_state(oss, zp_mod=zp)}


def patch_surface(oss: Any, req: SurfacePatchRequest, zp_mod: Any = None) -> Dict[str, Any]:
    zp = zp_mod or get_zospy()
    lde = oss.LDE
    if req.surface_index < 1 or req.surface_index >= lde.NumberOfSurfaces - 1:
        return {"status": "error", "message": f"表面序号 {req.surface_index} 超出可编辑范围"}
    surf = lde.GetSurfaceAt(req.surface_index)
    if req.semi_diameter_auto and req.semi_diameter is not None:
        return {"status": "error", "message": "固定半口径与自动半口径不能同时设置"}
    if req.is_stop is False and surf.IsStop:
        return {"status": "error", "message": "不能直接取消当前光阑，请对替代表面设置 is_stop=true"}
    if req.is_stop is True and not surf.TypeData.CanBeStop:
        return {"status": "error", "message": "该表面类型不能作为光阑"}

    semi_cell = None
    semi_type = None
    semi_solve = None
    if req.semi_diameter_auto or req.semi_diameter is not None:
        semi_cell = surf.SemiDiameterCell
        semi_type = (
            zp.constants.Editors.SolveType.Automatic
            if req.semi_diameter_auto
            else zp.constants.Editors.SolveType.Fixed
        )
        if not semi_cell.IsSolveTypeSupported(semi_type):
            return {"status": "error", "message": "该表面不支持请求的半口径求解类型"}
        semi_solve = semi_cell.CreateSolveType(semi_type)
        if semi_solve is None or not semi_solve.IsValid:
            return {"status": "error", "message": "无法创建半口径求解"}

    if semi_cell is not None:
        result = semi_cell.SetSolveData(semi_solve)
        if result != zp.constants.Editors.SolveStatus.Success or semi_cell.Solve != semi_type:
            raise RuntimeError(f"设置半口径求解失败: {result}")
        if req.semi_diameter is not None:
            surf.SemiDiameter = float(req.semi_diameter)
    if req.is_stop is True:
        surf.IsStop = True
        if not surf.IsStop:
            raise RuntimeError("光阑移动失败")
    patch_type = getattr(req, "surface_type", None)
    if patch_type is not None:
        _change_surface_type(surf, patch_type, zp_mod=zp)
    if req.radius is not None:
        surf.Radius = float(req.radius)
    if req.thickness is not None:
        surf.Thickness = float(req.thickness)
    if req.material is not None:
        surf.Material = req.material.upper().strip()
    patch_conic = getattr(req, "conic", None)
    if patch_conic is not None:
        surf.Conic = float(patch_conic)
    if req.radius_variable is not None:
        _set_variable(surf.RadiusCell, req.radius_variable, zp_mod=zp)
    patch_conic_var = getattr(req, "conic_variable", None)
    if patch_conic_var is not None:
        _set_variable(surf.ConicCell, patch_conic_var, zp_mod=zp)
    patch_mat_sub = getattr(req, "material_substitute", None)
    if patch_mat_sub is not None:
        _set_material_substitute(
            surf.MaterialCell, patch_mat_sub, getattr(req, "substitute_catalog", "SCHOTT"), zp_mod=zp
        )
    patch_terms = getattr(req, "asphere_terms", None)
    if patch_terms:
        _set_asphere_terms(surf, patch_terms, zp_mod=zp)
    patch_term_vars = getattr(req, "asphere_terms_variable", None)
    if patch_term_vars:
        _set_asphere_variables(surf, patch_term_vars, zp_mod=zp)
    mrhs = getattr(req, "marginal_ray_height_solve", None)
    if mrhs is not None:
        if mrhs:
            _set_marginal_ray_height_solve(surf.ThicknessCell, 0.0, 0.0, zp_mod=zp)
        else:
            _set_variable(surf.ThicknessCell, False, zp_mod=zp)
    elif req.thickness_variable is not None:
        _set_variable(surf.ThicknessCell, req.thickness_variable, zp_mod=zp)
    return {"status": "ok", "data": extract_system_state(oss, zp_mod=zp)}


def quick_focus(oss: Any, zp_mod: Any = None) -> Dict[str, Any]:
    zp = zp_mod or get_zospy()
    qf = oss.Tools.OpenQuickFocus()
    if qf is None:
        raise RuntimeError("无法打开快速聚焦工具，请检查是否有其他工具正在运行")
    try:
        criterion = getattr(
            getattr(zp.constants.Tools, "General", None),
            "QuickFocusCriterion",
            getattr(zp.constants.Tools, "QuickFocusCriterion", None),
        )
        if criterion is not None and hasattr(criterion, "SpotSizeRadial"):
            qf.Criterion = criterion.SpotSizeRadial
        if hasattr(qf, "UseCentroid"):
            qf.UseCentroid = True
        qf.RunAndWaitForCompletion()
        if hasattr(qf, "Succeeded") and not qf.Succeeded:
            raise RuntimeError(str(getattr(qf, "ErrorMessage", "快速聚焦未成功")))
    finally:
        qf.Close()
    return {
        "status": "ok",
        "message": "快速聚焦完成",
        "data": extract_system_state(oss, zp_mod=zp),
    }


def _insert_glass_min_edge_constraints(
    mfe: Any,
    oss: Any,
    curr_row: int,
    min_edge: float,
    op_enum: Any,
    zp_mod: Any = None,
) -> int:
    """仅对含玻璃材料的表面插入 MNEG 最小边缘厚度约束，避免误约束空气间隔。"""
    lde = oss.LDE
    last_surf_idx = lde.NumberOfSurfaces - 2
    for s_idx in range(1, last_surf_idx + 1):
        mat = str(getattr(lde.GetSurfaceAt(s_idx), "Material", "") or "").strip()
        if mat:
            _insert_operand_row(
                mfe, curr_row, op_enum.MNEG, min_edge, 5.0, int1=s_idx, int2=s_idx, zp_mod=zp_mod
            )
            curr_row += 1
    return curr_row


def _build_merit_function(oss: Any, req: Any, zp_mod: Any = None) -> Optional[Dict[str, Any]]:
    """按本次请求重建 MFE，成功返回 None，若操作数非法则返回错误字典。"""
    zp = zp_mod or get_zospy()
    mfe = oss.MFE
    op_enum = zp.constants.Editors.MFE.MeritOperandType
    prim_wl = _get_primary_wavelength_index(oss)
    for item in req.custom_operands:
        op_name = item.operand_type.upper().strip()
        if not hasattr(op_enum, op_name):
            return {"status": "error", "message": f"未知的操作数名称: {op_name}"}

    if getattr(req, "use_trac_spherical_mf", False):
        if mfe.NumberOfOperands > 0:
            mfe.RemoveOperandsAt(1, mfe.NumberOfOperands)
        curr_row = 1
        py_list = [0.0, 0.3, 0.5, 0.7, 0.85, 1.0]
        for py in py_list:
            _insert_operand_row(
                mfe,
                curr_row,
                op_enum.TRAC,
                0.0,
                1.0,
                int1=prim_wl,
                data1=0.0,
                data2=0.0,
                data3=0.0,
                data4=py,
                zp_mod=zp,
            )
            curr_row += 1
        _insert_operand_row(mfe, curr_row, op_enum.MNCA, 0.1, 1.0, int1=1, int2=3, zp_mod=zp)
        curr_row += 1
        _insert_operand_row(mfe, curr_row, op_enum.MXCA, 50.0, 1.0, int1=1, int2=3, zp_mod=zp)
        curr_row += 1
        target_effl = req.target_effl if req.target_effl is not None else 70.0
        _insert_operand_row(
            mfe, curr_row, op_enum.EFFL, target_effl, req.effl_weight, int2=prim_wl, zp_mod=zp
        )
        curr_row += 1
        min_edge = getattr(req, "min_edge_thickness", None)
        if min_edge is not None:
            curr_row = _insert_glass_min_edge_constraints(
                mfe, oss, curr_row, float(min_edge), op_enum, zp_mod=zp
            )
        for item in req.custom_operands:
            op_name = item.operand_type.upper().strip()
            _insert_operand_row(
                mfe,
                curr_row,
                getattr(op_enum, op_name),
                item.target,
                item.weight,
                item.int1,
                item.int2,
                item.data1,
                item.data2,
                zp_mod=zp,
            )
            curr_row += 1
    else:
        wizard = mfe.SEQOptimizationWizard
        wizard.Type = 0
        wizard.Data = 0 if req.use_wavefront else 1
        wizard.OverallWeight = 1.0
        wizard.IsGlassUsed = True
        wizard.GlassMin = 2.0
        wizard.GlassMax = 30.0
        wizard.GlassEdge = 1.5
        wizard.IsAirUsed = True
        wizard.AirMin = 0.5
        wizard.AirMax = 500.0
        wizard.AirEdge = 0.5
        if mfe.NumberOfOperands > 0:
            mfe.RemoveOperandsAt(1, mfe.NumberOfOperands)
        wizard.StartAt = 1
        wizard.IsAddFavoriteOperandsUsed = False
        wizard.Apply()

        curr_row = 1
        if req.target_effl is not None:
            _insert_operand_row(
                mfe, curr_row, op_enum.EFFL, req.target_effl, req.effl_weight, int2=prim_wl, zp_mod=zp
            )
            curr_row += 1
        if req.minimize_axial_color:
            _insert_operand_row(
                mfe,
                curr_row,
                op_enum.AXCL,
                0.0,
                5.0,
                int1=1,
                int2=oss.SystemData.Wavelengths.NumberOfWavelengths,
                zp_mod=zp,
            )
            curr_row += 1
        if req.max_totr is not None:
            totr_row = curr_row
            _insert_operand_row(mfe, totr_row, op_enum.TOTR, 0.0, 0.0, zp_mod=zp)
            curr_row += 1
            _insert_operand_row(
                mfe, curr_row, op_enum.OPLT, req.max_totr, 5.0, int1=totr_row, zp_mod=zp
            )
            curr_row += 1
        if req.min_bfl is not None:
            last_surf_idx = oss.LDE.NumberOfSurfaces - 2
            if last_surf_idx >= 1:
                tthi_row = curr_row
                _insert_operand_row(
                    mfe,
                    tthi_row,
                    op_enum.TTHI,
                    0.0,
                    0.0,
                    int1=last_surf_idx,
                    int2=last_surf_idx,
                    zp_mod=zp,
                )
                curr_row += 1
                _insert_operand_row(
                    mfe, curr_row, op_enum.OPGT, req.min_bfl, 5.0, int1=tthi_row, zp_mod=zp
                )
                curr_row += 1
        min_edge = getattr(req, "min_edge_thickness", None)
        if min_edge is not None:
            curr_row = _insert_glass_min_edge_constraints(
                mfe, oss, curr_row, float(min_edge), op_enum, zp_mod=zp
            )
        for item in req.custom_operands:
            op_name = item.operand_type.upper().strip()
            _insert_operand_row(
                mfe,
                curr_row,
                getattr(op_enum, op_name),
                item.target,
                item.weight,
                item.int1,
                item.int2,
                item.data1,
                item.data2,
                zp_mod=zp,
            )
            curr_row += 1
    return None


def run_optimization(
    oss: Any,
    req: OptimizeRequest,
    zp_mod: Any = None,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> Dict[str, Any]:
    zp = zp_mod or get_zospy()
    if snapshot_manager is not None:
        try:
            snapshot_manager.create(oss, label="auto:before_optimize")
        except Exception:
            pass
    mfe = oss.MFE
    op_enum = zp.constants.Editors.MFE.MeritOperandType
    prim_wl = _get_primary_wavelength_index(oss)
    err = _build_merit_function(oss, req, zp_mod=zp)
    if err is not None:
        return err

    initial_mf = float(mfe.CalculateMeritFunction())
    if initial_mf > 1e6:
        return {
            "status": "error",
            "message": f"初始结构光线追迹失败(Merit={initial_mf:.2e})！发生全反射(TIR)或切光，请减缓曲率半径后再优化。",
            "data": extract_system_state(oss, zp_mod=zp),
        }

    opt = oss.Tools.OpenLocalOptimization()
    if opt is None:
        raise RuntimeError("无法打开优化工具，请检查是否有其他工具正在运行")
    try:
        opt.Algorithm = zp.constants.Tools.Optimization.OptimizationAlgorithm.DampedLeastSquares
        opt.Cycles = (
            zp.constants.Tools.Optimization.OptimizationCycles.Automatic
            if req.cycles == 0
            else zp.constants.Tools.Optimization.OptimizationCycles.Fixed_10_Cycles
        )
        opt.RunAndWaitForCompletion()
        if not opt.Succeeded:
            raise RuntimeError(str(opt.ErrorMessage) or "局部优化失败")
    finally:
        opt.Close()

    actual_effl = float(mfe.GetOperandValue(op_enum.EFFL, 0, prim_wl, 0, 0.0, 0.0, 0.0, 0.0, 0.0))
    actual_totr = float(mfe.GetOperandValue(op_enum.TOTR, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0))

    return {
        "status": "ok",
        "initial_merit": round(initial_mf, 6),
        "final_merit": round(float(mfe.CalculateMeritFunction()), 6),
        "achieved_constraints": {"effl_mm": round(actual_effl, 3), "totr_mm": round(actual_totr, 3)},
        "data": extract_system_state(oss, zp_mod=zp),
    }


def evaluate_image_quality(oss: Any, req: EvaluateRequest, zp_mod: Any = None) -> Dict[str, Any]:
    zp = zp_mod or get_zospy()
    mfe = oss.MFE
    op_type = zp.constants.Editors.MFE.MeritOperandType
    num_fields = oss.SystemData.Fields.NumberOfFields
    prim_wl = _get_primary_wavelength_index(oss)
    field_coordinates = _get_normalized_field_coordinates(oss.SystemData.Fields, zp_mod=zp)
    _get_rms_spot_radii(oss)
    issues: List[str] = []

    wavefront_results = []
    for f_idx in range(1, num_fields + 1):
        fov_y = float(oss.SystemData.Fields.GetField(f_idx).Y)
        hx, hy = field_coordinates[f_idx - 1]
        rms_wf = float(mfe.GetOperandValue(op_type.RWCE, 4, prim_wl, hx, hy, 0.0, 0.0, 0.0, 0.0))
        wavefront_valid = math.isfinite(rms_wf) and 0.0 < rms_wf < 1e6
        if not wavefront_valid:
            issues.append(f"视场 {f_idx} 的 RWCE 为零或无效，无法确认波前像质")
        strehl = None
        if wavefront_valid:
            strehl = math.exp(-((2 * math.pi * rms_wf) ** 2)) if rms_wf < 1.0 else 0.0
        wavefront_results.append(
            {
                "field_index": f_idx,
                "field_y_deg": fov_y,
                "rms_wavefront_waves": round(rms_wf, 4),
                "wavefront_valid": wavefront_valid,
                "diffraction_limited": bool(wavefront_valid and rms_wf < 0.0715),
                "strehl_ratio_approx": round(strehl, 4) if strehl is not None else None,
            }
        )

    freq_step = req.max_frequency / max(req.num_freq_points, 1)
    frequencies = [round(freq_step * i, 2) for i in range(1, req.num_freq_points + 1)]
    mtf_results = []
    for f_idx in range(1, num_fields + 1):
        fov_y = float(oss.SystemData.Fields.GetField(f_idx).Y)
        curve_data = []
        for freq in frequencies:
            mtf_t = float(
                mfe.GetOperandValue(op_type.MTFT, req.mtf_sampling, 0, f_idx, freq, 0.0, 0.0, 0.0, 0.0)
            )
            mtf_s = float(
                mfe.GetOperandValue(op_type.MTFS, req.mtf_sampling, 0, f_idx, freq, 0.0, 0.0, 0.0, 0.0)
            )
            mtf_dl = float(
                mfe.GetOperandValue(op_type.MTFA, req.mtf_sampling, 0, 0.0, freq, 1.0, 0.0, 0.0, 0.0)
            )
            mtf_valid = all(math.isfinite(v) and 0.0 <= v <= 1.0 for v in (mtf_t, mtf_s, mtf_dl))
            if not mtf_valid:
                issues.append(f"视场 {f_idx} 在频率 {freq} 的 MTF 返回无效数值")
            curve_data.append(
                {
                    "frequency": freq,
                    "mtf_valid": mtf_valid,
                    "tangential": round(mtf_t, 4),
                    "sagittal": round(mtf_s, 4),
                    "average": round((mtf_t + mtf_s) / 2.0, 4),
                    "diffraction_limit": round(mtf_dl, 4),
                }
            )
        mtf_results.append({"field_index": f_idx, "field_y_deg": fov_y, "mtf_curve": curve_data})

    effl = float(mfe.GetOperandValue(op_type.EFFL, 0, prim_wl, 0, 0.0, 0.0, 0.0, 0.0, 0.0))
    wfno = float(mfe.GetOperandValue(op_type.WFNO, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0))

    if not all(math.isfinite(v) and abs(v) < 1e6 for v in (effl, wfno)):
        issues.append("焦距或工作 F 数返回非有限数值或错误哨兵")
    return _json_safe_state(
        {
            "status": "error" if issues else "ok",
            "evaluation_valid": not issues,
            "issues": issues,
            "mtf_sampling": req.mtf_sampling,
            "mtf_grid": 0,
            "mtf_diffraction_limit_grid": 1,
            "mtf_sampling_converged": None,
            "paraxial_specs": {
                "effective_focal_length_mm": round(effl, 3),
                "working_f_number": round(wfno, 3),
            },
            "wavefront_summary": wavefront_results,
            "mtf_summary": mtf_results,
        }
    )


def tune_parameter(oss: Any, req: ParameterTuneRequest, zp_mod: Any = None) -> Dict[str, Any]:
    zp = zp_mod or get_zospy()
    lde = oss.LDE
    if req.surface_index < 1 or req.surface_index >= lde.NumberOfSurfaces - 1:
        return {
            "status": "error",
            "message": f"表面序号 {req.surface_index} 超出可编辑范围 (1 至 {lde.NumberOfSurfaces - 2})",
        }

    param = req.parameter.lower().strip()
    if param not in ("radius", "thickness", "semi_diameter", "conic"):
        return {
            "status": "error",
            "message": f"不支持的调参变量类型: '{req.parameter}'。仅支持 'radius', 'thickness', 'semi_diameter', 'conic'",
        }

    surf = lde.GetSurfaceAt(req.surface_index)
    attr_name = {
        "radius": "Radius",
        "thickness": "Thickness",
        "semi_diameter": "SemiDiameter",
        "conic": "Conic",
    }[param]
    orig_val = getattr(surf, attr_name)

    last_surf_idx = lde.NumberOfSurfaces - 2
    orig_last_thickness = lde.GetSurfaceAt(last_surf_idx).Thickness if last_surf_idx >= 1 else None

    if req.steps <= 1:
        test_values = [req.min_value]
    else:
        step_size = (req.max_value - req.min_value) / (req.steps - 1)
        test_values = [req.min_value + i * step_size for i in range(req.steps)]

    results: List[Dict[str, Any]] = []
    best_candidate: Optional[Dict[str, Any]] = None
    best_metric = float("inf")
    best_last_thickness = None
    applied = False

    try:
        for val in test_values:
            setattr(surf, attr_name, float(val))

            refocused_bfl = None
            if req.quick_focus:
                try:
                    qf = oss.Tools.OpenQuickFocus()
                    if qf is not None:
                        try:
                            criterion = getattr(
                                getattr(zp.constants.Tools, "General", None),
                                "QuickFocusCriterion",
                                getattr(zp.constants.Tools, "QuickFocusCriterion", None),
                            )
                            if criterion is not None and hasattr(criterion, "SpotSizeRadial"):
                                qf.Criterion = criterion.SpotSizeRadial
                            if hasattr(qf, "UseCentroid"):
                                qf.UseCentroid = True
                            qf.RunAndWaitForCompletion()
                        finally:
                            qf.Close()
                            if last_surf_idx >= 1:
                                refocused_bfl = round(
                                    float(lde.GetSurfaceAt(last_surf_idx).Thickness), 4
                                )
                except Exception:
                    pass

            state = extract_system_state(oss, zp_mod=zp)
            ray_ok = bool(state.get("ray_trace_valid", False))

            effl = None
            try:
                prim_wl = _get_primary_wavelength_index(oss)
                op_effl = zp.constants.Editors.MFE.MeritOperandType.EFFL
                effl_val = float(
                    oss.MFE.GetOperandValue(op_effl, 0, prim_wl, 0, 0.0, 0.0, 0.0, 0.0, 0.0)
                )
                if math.isfinite(effl_val) and abs(effl_val) < 1e6:
                    effl = round(effl_val, 4)
            except Exception:
                pass

            metric_val = None
            if ray_ok:
                crit = req.criterion.lower().strip()
                if crit in ("geo_spot", "geo_spot_diameter_um"):
                    metric_val = state.get("geo_spot_diameter_um")
                elif crit in ("rms_spot", "rms_spot_radius"):
                    rms_mat = state.get("rms_spot_radius")
                    if rms_mat and len(rms_mat) > 0 and len(rms_mat[0]) > 0:
                        metric_val = round(rms_mat[0][0] * 1000.0, 4)
                elif crit in ("merit_function", "merit", "mf"):
                    mf = state.get("merit_function")
                    if mf is not None and math.isfinite(mf) and mf < 1e6:
                        metric_val = round(mf, 6)
                elif crit in ("effl_diff", "effl"):
                    if effl is not None and req.target_effl is not None:
                        metric_val = round(abs(effl - req.target_effl), 4)

                if metric_val is None:
                    metric_val = state.get("geo_spot_diameter_um") or state.get("merit_function")

            item: Dict[str, Any] = {
                "value": round(float(val), 5),
                "ray_trace_valid": ray_ok,
                "metric_value": metric_val,
                "geo_spot_diameter_um": state.get("geo_spot_diameter_um"),
                "merit_function": state.get("merit_function"),
                "effl_mm": effl,
            }
            if refocused_bfl is not None:
                item["refocused_bfl_mm"] = refocused_bfl

            results.append(item)

            if ray_ok and metric_val is not None and metric_val < best_metric:
                best_metric = metric_val
                best_candidate = item
                if req.quick_focus and last_surf_idx >= 1:
                    best_last_thickness = lde.GetSurfaceAt(last_surf_idx).Thickness

        if req.apply_best and best_candidate is not None:
            setattr(surf, attr_name, float(best_candidate["value"]))
            if req.quick_focus and best_last_thickness is not None and last_surf_idx >= 1:
                lde.GetSurfaceAt(last_surf_idx).Thickness = float(best_last_thickness)
            applied = True
        else:
            setattr(surf, attr_name, float(orig_val))
            if req.quick_focus and orig_last_thickness is not None and last_surf_idx >= 1:
                lde.GetSurfaceAt(last_surf_idx).Thickness = float(orig_last_thickness)
            applied = False

    except Exception:
        if not applied:
            try:
                setattr(surf, attr_name, float(orig_val))
                if req.quick_focus and orig_last_thickness is not None and last_surf_idx >= 1:
                    lde.GetSurfaceAt(last_surf_idx).Thickness = float(orig_last_thickness)
            except Exception:
                pass
        raise

    trend_info = analyze_sweep_trend(results, best_candidate, req.min_value, req.max_value)
    valid_candidates = [
        r for r in results if r["ray_trace_valid"] and r["metric_value"] is not None
    ]
    sorted_candidates = sorted(valid_candidates, key=lambda x: x["metric_value"])
    top_candidates = [
        {
            "rank": idx + 1,
            "value": cand["value"],
            "metric_value": cand["metric_value"],
            "geo_spot_diameter_um": cand.get("geo_spot_diameter_um"),
            "effl_mm": cand.get("effl_mm"),
        }
        for idx, cand in enumerate(sorted_candidates[:3])
    ]

    best_cand_out = None
    if best_candidate is not None:
        best_cand_out = dict(best_candidate)
        best_cand_out["applied"] = applied

    return _json_safe_state(
        {
            "status": "ok",
            "surface_index": req.surface_index,
            "parameter": param,
            "original_value": round(float(orig_val), 5) if orig_val is not None else None,
            "scan_range": {"min": req.min_value, "max": req.max_value, "steps": req.steps},
            "criterion": req.criterion,
            "quick_focus": req.quick_focus,
            "applied": applied,
            "valid_count": len(valid_candidates),
            "invalid_count": len(results) - len(valid_candidates),
            "best_candidate": best_cand_out,
            "trend_analysis": trend_info,
            "top_candidates": top_candidates,
            "all_results": results,
        }
    )


def run_hammer_optimization(
    oss: Any,
    req: HammerOptimizeRequest,
    zp_mod: Any = None,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> Dict[str, Any]:
    """执行限时 Hammer 全局优化，支持跳出局部极小值与玻璃替换 (MaterialSubstitute)。"""
    zp = zp_mod or get_zospy()
    if snapshot_manager is not None:
        try:
            snapshot_manager.create(oss, label="auto:before_hammer")
        except Exception:
            pass
    mfe = oss.MFE
    op_enum = zp.constants.Editors.MFE.MeritOperandType
    prim_wl = _get_primary_wavelength_index(oss)
    err = _build_merit_function(oss, req, zp_mod=zp)
    if err is not None:
        return err

    initial_mf = float(mfe.CalculateMeritFunction())
    if initial_mf > 1e6:
        return {
            "status": "error",
            "message": f"初始结构光线追迹失败(Merit={initial_mf:.2e})！发生全反射(TIR)或切光，请减缓曲率半径后再运行 Hammer。",
            "data": extract_system_state(oss, zp_mod=zp),
        }

    hammer = oss.Tools.OpenHammerOptimization()
    if hammer is None:
        raise RuntimeError("无法打开 Hammer 优化工具，请检查是否有其他工具正在运行")
    try:
        hammer.Algorithm = zp.constants.Tools.Optimization.OptimizationAlgorithm.DampedLeastSquares
        if hasattr(hammer, "AutomaticOptimization"):
            hammer.AutomaticOptimization = bool(req.automatic_optimization)
        if hasattr(hammer, "TargetRunTimeM"):
            hammer.TargetRunTimeM = float(req.target_run_time_minutes)
        timeout_sec = float(req.target_run_time_minutes) * 60.0 + 10.0
        if hasattr(hammer, "RunAndWaitWithTimeout"):
            status = hammer.RunAndWaitWithTimeout(timeout_sec)
            timed_out_enum = getattr(
                getattr(zp.constants.Tools, "RunStatus", None), "TimedOut", None
            )
            if timed_out_enum is not None and status == timed_out_enum and hasattr(hammer, "Cancel"):
                hammer.Cancel()
        else:
            hammer.RunAndWaitForCompletion()
        if hasattr(hammer, "Succeeded") and not hammer.Succeeded and hasattr(hammer, "ErrorMessage"):
            err_msg = str(hammer.ErrorMessage or "")
            if err_msg:
                raise RuntimeError(err_msg)
    finally:
        hammer.Close()

    actual_effl = float(mfe.GetOperandValue(op_enum.EFFL, 0, prim_wl, 0, 0.0, 0.0, 0.0, 0.0, 0.0))
    actual_totr = float(mfe.GetOperandValue(op_enum.TOTR, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0))

    return {
        "status": "ok",
        "mode": "hammer",
        "target_run_time_minutes": req.target_run_time_minutes,
        "initial_merit": round(initial_mf, 6),
        "final_merit": round(float(mfe.CalculateMeritFunction()), 6),
        "achieved_constraints": {"effl_mm": round(actual_effl, 3), "totr_mm": round(actual_totr, 3)},
        "data": extract_system_state(oss, zp_mod=zp),
    }


def get_aberrations(oss: Any, zp_mod: Any = None) -> Dict[str, Any]:
    """计算系统级及逐面赛德尔像差、色差 (AXCL/LACL)、表面贡献占比与物理归因建议。"""
    zp = zp_mod or get_zospy()
    mfe = oss.MFE
    op_type = zp.constants.Editors.MFE.MeritOperandType
    prim_wl = _get_primary_wavelength_index(oss)
    lde = oss.LDE
    seidel_ops = ("SPHA", "COMA", "ASTI", "FCUR", "DIST", "PETZ")

    def _eval_surf(surf_idx: int) -> Dict[str, Any]:
        row: Dict[str, Any] = {}
        for name in seidel_ops:
            op_enum = getattr(op_type, name, None)
            if op_enum is not None:
                val = float(mfe.GetOperandValue(op_enum, surf_idx, prim_wl, 0, 0.0, 0.0, 0.0, 0.0, 0.0))
                row[name.lower()] = round(val, 6) if math.isfinite(val) else val
        return row

    system_totals = _eval_surf(0)
    num_wls = oss.SystemData.Wavelengths.NumberOfWavelengths
    if num_wls > 1:
        if hasattr(op_type, "AXCL"):
            axcl = float(mfe.GetOperandValue(op_type.AXCL, 1, num_wls, 0, 0.0, 0.0, 0.0, 0.0, 0.0))
            system_totals["axcl_mm"] = round(axcl, 6) if math.isfinite(axcl) else axcl
        if hasattr(op_type, "LACL"):
            lacl = float(mfe.GetOperandValue(op_type.LACL, 1, num_wls, 0, 0.0, 0.0, 0.0, 0.0, 0.0))
            system_totals["lacl_mm"] = round(lacl, 6) if math.isfinite(lacl) else lacl

    per_surface = []
    for s_idx in range(1, lde.NumberOfSurfaces - 1):
        surf = lde.GetSurfaceAt(s_idx)
        entry: Dict[str, Any] = {
            "surface_index": s_idx,
            "comment": getattr(surf, "Comment", ""),
            "material": str(getattr(surf, "Material", "") or ""),
        }
        try:
            r = float(getattr(surf, "Radius", 0.0) or 0.0)
            sd = float(getattr(surf, "SemiDiameter", 0.0) or 0.0)
            if r != 0.0 and math.isfinite(r) and sd > 0.0:
                entry["radius_to_semidiameter"] = round(abs(r) / sd, 3)
        except Exception:
            pass
        entry.update(_eval_surf(s_idx))
        per_surface.append(entry)

    # 计算各表面绝对贡献占比 (%)
    for key in ("spha", "coma", "asti"):
        total_abs = sum(abs(row.get(key, 0.0) or 0.0) for row in per_surface if isinstance(row.get(key), (int, float)))
        if total_abs > 1e-12:
            for row in per_surface:
                val = row.get(key)
                if isinstance(val, (int, float)):
                    row[f"{key}_share_pct"] = round(abs(val) / total_abs * 100.0, 1)

    # 联合实际 RWCE 判别三阶主导 vs 高级像差主导
    rwce_on_axis = None
    try:
        if hasattr(op_type, "RWCE"):
            wf = float(mfe.GetOperandValue(op_type.RWCE, 4, prim_wl, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0))
            if math.isfinite(wf) and 0.0 <= wf < 1e6:
                rwce_on_axis = round(wf, 4)
    except Exception:
        pass

    spha_tot = abs(system_totals.get("spha", 0.0) or 0.0)
    dominant_regime = "unknown"
    if rwce_on_axis is not None:
        if rwce_on_axis < 0.0715:
            dominant_regime = "diffraction_limited"
        elif spha_tot < 0.05 and rwce_on_axis > 0.25:
            dominant_regime = "high_order"
        else:
            dominant_regime = "third_order"

    # 生成结构化归因提示
    hints: List[str] = []
    if per_surface:
        top_spha = max(per_surface, key=lambda r: abs(r.get("spha", 0.0) or 0.0))
        if abs(top_spha.get("spha", 0.0) or 0.0) > 0.05:
            hints.append(
                f"表面 {top_spha['surface_index']} 贡献了 {top_spha.get('spha_share_pct', 0)}% 的三阶球差 (SPHA={top_spha.get('spha')})，"
                "建议优先对该面释放弯曲/Conic 或调用 zemax_promote_asphere / zemax_split_lens。"
            )
        top_coma = max(per_surface, key=lambda r: abs(r.get("coma", 0.0) or 0.0))
        if abs(top_coma.get("coma", 0.0) or 0.0) > 0.05:
            hints.append(
                f"表面 {top_coma['surface_index']} 贡献了 {top_coma.get('coma_share_pct', 0)}% 的彗差 (COMA={top_coma.get('coma')})，"
                "可考虑调整光阑位置或局部曲率弯曲。"
            )
        steep = [r for r in per_surface if r.get("radius_to_semidiameter", 99.0) < 1.5]
        for r in steep:
            hints.append(
                f"表面 {r['surface_index']} 曲率偏陡 (|R|/SD={r['radius_to_semidiameter']} < 1.5)，存在高级像差及 TIR 风险。"
            )
    if dominant_regime == "high_order":
        hints.append(
            f"系统三阶球差较小 (|SPHA|={spha_tot:.4f}) 但实际轴上波前 RMS={rwce_on_axis} waves 较大，说明已进入高级像差主导区，建议分裂光焦度 (zemax_split_lens) 或引入高阶非球面。"
        )
    axcl_val = system_totals.get("axcl_mm")
    if isinstance(axcl_val, (int, float)) and abs(axcl_val) > 0.2:
        hints.append(
            f"轴向色差偏大 (AXCL={axcl_val} mm)，建议优化时开启 minimize_axial_color 或设置 MaterialSubstitute 运行 Hammer 换玻璃。"
        )

    return _json_safe_state(
        {
            "status": "ok",
            "primary_wavelength_index": prim_wl,
            "on_axis_wavefront_rms": rwce_on_axis,
            "dominant_regime": dominant_regime,
            "system_totals": system_totals,
            "per_surface": per_surface,
            "attribution_hints": hints,
        }
    )


def safe_call(fn, *args, include_trace: bool = True, **kwargs) -> Dict[str, Any]:
    """Execute a service function and normalize unexpected exceptions to error dicts."""
    try:
        return fn(*args, **kwargs)
    except Exception as exc:
        resp: Dict[str, Any] = {"status": "error", "message": str(exc)}
        if include_trace:
            resp["trace"] = traceback.format_exc()
        return resp
