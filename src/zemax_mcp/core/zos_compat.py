"""ZOS-API / OpticStudio 2024 R2 compatibility helpers and JSON-safe serialization."""

from __future__ import annotations

import math
from typing import Any


EVEN_ASPHERE_ORDER_TO_PAR: dict[int, int] = {
    2: 1,
    4: 2,
    6: 3,
    8: 4,
    10: 5,
    12: 6,
    14: 7,
    16: 8,
}


def _change_surface_type(surf: Any, surface_type: str, zp_mod: Any = None) -> None:
    """切换表面类型（Standard / EvenAsphere）。"""
    zp = zp_mod or get_zospy()
    norm = surface_type.strip().lower().replace("_", "").replace(" ", "")
    st_enum = zp.constants.Editors.LDE.SurfaceType
    if norm == "standard":
        target_type = st_enum.Standard
    elif norm in ("evenasphere", "evenaspheric"):
        target_type = st_enum.EvenAspheric
    else:
        raise ValueError(f"不支持的表面类型: '{surface_type}'，仅支持 'Standard' 或 'EvenAsphere'")
    settings = surf.GetSurfaceTypeSettings(target_type)
    surf.ChangeType(settings)


def _get_par_cell(surf: Any, order: int, zp_mod: Any = None) -> Any:
    """根据偶次非球面阶次 (2..16) 获取对应的 Par1..Par8 单元格。"""
    par_idx = EVEN_ASPHERE_ORDER_TO_PAR.get(int(order))
    if par_idx is None:
        raise ValueError(f"不支持的偶次非球面阶次: {order}，仅支持 {list(EVEN_ASPHERE_ORDER_TO_PAR.keys())}")
    zp = zp_mod or get_zospy()
    col_enum = zp.constants.Editors.LDE.SurfaceColumn
    col = getattr(col_enum, f"Par{par_idx}")
    return surf.GetSurfaceCell(col)


def _set_asphere_terms(surf: Any, terms: dict[int, float], zp_mod: Any = None) -> None:
    """写入偶次非球面高次项系数。"""
    for order, value in terms.items():
        cell = _get_par_cell(surf, int(order), zp_mod=zp_mod)
        cell.DoubleValue = float(value)


def _set_asphere_variables(surf: Any, var_map: dict[int, bool], zp_mod: Any = None) -> None:
    """设置偶次非球面高次项变量状态。"""
    for order, is_var in var_map.items():
        cell = _get_par_cell(surf, int(order), zp_mod=zp_mod)
        _set_variable(cell, bool(is_var), zp_mod=zp_mod)


def _extract_asphere_info(surf: Any, zp_mod: Any = None) -> dict[str, Any]:
    """安全提取表面的 TypeName、Conic 及偶次非球面非零系数。"""
    info: dict[str, Any] = {}
    type_name = getattr(surf, "TypeName", None)
    if type_name is not None:
        info["surface_type"] = str(type_name)
    conic = getattr(surf, "Conic", None)
    if conic is not None:
        try:
            info["conic"] = round(float(conic), 6)
        except Exception:
            pass
    if type_name and "aspher" in str(type_name).lower():
        terms: dict[str, float] = {}
        for order in EVEN_ASPHERE_ORDER_TO_PAR:
            try:
                cell = _get_par_cell(surf, order, zp_mod=zp_mod)
                val = float(cell.DoubleValue)
                if val != 0.0:
                    terms[str(order)] = val
            except Exception:
                pass
        if terms:
            info["asphere_terms"] = terms
    return info


def get_zospy():
    """Lazy-import zospy so offline tests and schema imports do not require OpticStudio."""
    import zospy as zp

    return zp


def _json_safe_state(value: Any) -> Any:
    """递归转换响应中的非有限浮点数，保留无限远的含义。"""
    if isinstance(value, float) and not math.isfinite(value):
        if math.isnan(value):
            return None
        return "Infinity" if value > 0 else "-Infinity"
    if isinstance(value, dict):
        return {key: _json_safe_state(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe_state(item) for item in value]
    return value


def _set_variable(cell: Any, is_var: bool, zp_mod: Any = None) -> None:
    """兼容 pythonnet 3.x 的变量状态设置。"""
    zp = zp_mod or get_zospy()
    solve_type = (
        zp.constants.Editors.SolveType.Variable
        if is_var
        else zp.constants.Editors.SolveType.Fixed
    )
    solve = cell.CreateSolveType(solve_type)
    cell.SetSolveData(solve)


def _set_material_substitute(
    cell: Any, enable: bool, catalog: str = "SCHOTT", zp_mod: Any = None
) -> None:
    """设置或取消表面材料的玻璃替换求解 (SolveType.MaterialSubstitute)。"""
    zp = zp_mod or get_zospy()
    solve_type = (
        zp.constants.Editors.SolveType.MaterialSubstitute
        if enable
        else zp.constants.Editors.SolveType.Fixed
    )
    solve = cell.CreateSolveType(solve_type)
    if enable:
        sub = getattr(solve, "_S_MaterialSubstitute", solve)
        if hasattr(sub, "Catalog") and catalog:
            sub.Catalog = str(catalog)
    cell.SetSolveData(solve)


def _set_marginal_ray_height_solve(
    cell: Any, height: float = 0.0, pupil_zone: float = 0.0, zp_mod: Any = None
) -> None:
    """将厚度单元格设置为边缘光线高度求解 (Marginal Ray Height) 以定位高斯像面。"""
    zp = zp_mod or get_zospy()
    solve = cell.CreateSolveType(zp.constants.Editors.SolveType.MarginalRayHeight)
    target = getattr(solve, "_S_MarginalRayHeight", getattr(solve, "MarginalRayHeight", solve))
    if hasattr(target, "Height"):
        target.Height = float(height)
    if hasattr(target, "PupilZone"):
        target.PupilZone = float(pupil_zone)
    cell.SetSolveData(solve)


def _insert_operand_row(
    mfe: Any,
    row_idx: int,
    op_enum: Any,
    target: float,
    weight: float,
    int1: int | None = None,
    int2: int | None = None,
    data1: float | None = None,
    data2: float | None = None,
    data3: float | None = None,
    data4: float | None = None,
    zp_mod: Any = None,
) -> Any:
    """在 MFE 指定行插入操作数并写入参数 (Cell 2=Int1, 3=Int2, 4=Data1, 5=Data2, 6=Data3, 7=Data4)。"""
    zp = zp_mod or get_zospy()
    op = mfe.InsertNewOperandAt(row_idx)
    op.ChangeType(op_enum)
    op.Target = float(target)
    op.Weight = float(weight)
    column = zp.constants.Editors.MFE.MeritColumn
    if int1 is not None:
        op.GetOperandCell(column.Param1).IntegerValue = int(int1)
    if int2 is not None:
        op.GetOperandCell(column.Param2).IntegerValue = int(int2)
    if data1 is not None:
        op.GetOperandCell(column.Param3).DoubleValue = float(data1)
    if data2 is not None:
        op.GetOperandCell(column.Param4).DoubleValue = float(data2)
    if data3 is not None:
        col5 = getattr(column, "Param5", 6)
        op.GetOperandCell(col5).DoubleValue = float(data3)
    if data4 is not None:
        col6 = getattr(column, "Param6", 7)
        op.GetOperandCell(col6).DoubleValue = float(data4)
    return op


def _get_primary_wavelength_index(oss: Any) -> int:
    """通过 2024 R2 支持的 IsPrimary 标记查找主波长序号。"""
    wavelengths = oss.SystemData.Wavelengths
    for index in range(1, wavelengths.NumberOfWavelengths + 1):
        if wavelengths.GetWavelength(index).IsPrimary:
            return index
    raise RuntimeError("系统未设置主波长，无法执行优化或评估")


def _get_normalized_field_coordinates(fields: Any, zp_mod: Any = None) -> list[tuple[float, float]]:
    """按系统的 Radial/Rectangular 设置转换为 RWCE 所需的 Hx、Hy。"""
    zp = zp_mod or get_zospy()
    coordinates = [
        (float(fields.GetField(index).X), float(fields.GetField(index).Y))
        for index in range(1, fields.NumberOfFields + 1)
    ]
    normalization = fields.Normalization
    normalization_type = zp.constants.SystemData.FieldNormalizationType
    if normalization == normalization_type.Radial:
        scale = max((math.hypot(x, y) for x, y in coordinates), default=0.0)
        x_scale, y_scale = scale, scale
    elif normalization == normalization_type.Rectangular:
        x_scale = max((abs(x) for x, _ in coordinates), default=0.0)
        y_scale = max((abs(y) for _, y in coordinates), default=0.0)
    else:
        raise RuntimeError(f"不支持的视场归一化方式: {normalization}")
    return [
        (x / x_scale if x_scale else 0.0, y / y_scale if y_scale else 0.0)
        for x, y in coordinates
    ]


def _get_rms_spot_radii(oss: Any) -> list[list[float]]:
    """使用原生工具读取 RMS 点斑半径，返回配置 × 视场的二维列表。"""
    spot = oss.Tools.OpenRMSSpotRadiusCalculator()
    if spot is None:
        raise RuntimeError("无法打开 RMS 点斑计算工具，请检查是否有其他工具正在运行")
    try:
        spot.SetMethodGaussianQuadrature(4, 6, 0.0)
        spot.RunAndWaitForCompletion()
        if not spot.Succeeded:
            raise RuntimeError(str(spot.ErrorMessage) or "RMS 点斑计算失败")
        radii = spot.RMSSpotRadii
        if radii.GetLength(0) == 0 or radii.GetLength(1) == 0:
            raise RuntimeError("RMS 点斑计算未返回配置或视场数据")
        result = [
            [float(radii.GetValue(config, field)) for field in range(radii.GetLength(1))]
            for config in range(radii.GetLength(0))
        ]
        if any(not math.isfinite(value) or value < 0.0 for row in result for value in row):
            raise RuntimeError("RMS 点斑计算返回无效数值")
        return result
    finally:
        spot.Close()
