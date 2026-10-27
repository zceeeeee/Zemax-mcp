# zemax_server.py
import os
import math
import traceback
import uvicorn
from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any
import zospy as zp

app = FastAPI(title="Ansys Zemax 2024 R2 AI 守护进程")

# ==================== 单例长连接管理器 ====================
class ZemaxConnectionManager:
    def __init__(self):
        self.zos = None
        self.oss = None

    def connect(self):
        if self.oss is None:
            print("正在连接 Ansys Zemax OpticStudio Interactive Extension...")
            opticstudio_directory = os.environ.get(
                "OPTICSTUDIO_DIRECTORY", r"E:\Zemax\ANSYS Inc\v242\Zemax OpticStudio"
            )
            self.zos = zp.ZOS(opticstudio_directory=opticstudio_directory)
            self.oss = self.zos.connect(mode="extension")
            print("✅ 连接成功！Zemax 守护进程已就绪，监听端口 8765。")
        return self.oss

zm = ZemaxConnectionManager()

# ==================== 数据校验模型 (Pydantic) ====================

class SurfaceItem(BaseModel):
    comment: str = Field(default="", description="表面备注名称")
    radius: float = Field(default=0.0, description="曲率半径(mm)，0代表无穷大平面")
    thickness: float = Field(default=0.0, description="厚度/空气间隔(mm)")
    material: str = Field(default="", description="材料牌号(如 N-BK7)，空气留空")
    semi_diameter: Optional[float] = Field(default=None, description="半口径(mm)，留空则自动计算")
    is_stop: bool = Field(default=False, description="是否为光阑面(STO)")
    radius_variable: bool = Field(default=False, description="曲率半径是否设为变量(V)")
    thickness_variable: bool = Field(default=False, description="厚度是否设为变量(V)")

class SystemBuildRequest(BaseModel):
    epd: float = Field(description="入瞳直径 (mm)")
    fovs: List[float] = Field(default=[0.0], description="Y方向视场角列表 (度)")
    afocal_mode: bool = Field(default=False, description="是否开启无焦像空间模式(望远系统用)")
    surfaces: List[SurfaceItem] = Field(description="从第1面到像面前一面的表面列表")

class SurfacePatchRequest(BaseModel):
    surface_index: int = Field(description="要修改的表面序号 (从1开始)")
    radius: Optional[float] = None
    thickness: Optional[float] = None
    material: Optional[str] = None
    radius_variable: Optional[bool] = None
    thickness_variable: Optional[bool] = None
    semi_diameter: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False, description="固定半口径(mm)，必须为正数")
    semi_diameter_auto: bool = Field(default=False, strict=True, description="恢复半口径 Automatic 求解")
    is_stop: Optional[bool] = Field(default=None, strict=True, description="True 将光阑移至该面；不能用 False 取消现有光阑")

class CustomOperandItem(BaseModel):
    operand_type: str = Field(description="操作数名称，如 'EFFL', 'TOTR', 'AXCL', 'EXPP', 'PMAG'")
    int1: Optional[int] = None
    int2: Optional[int] = None
    data1: Optional[float] = None
    data2: Optional[float] = None
    target: float = 0.0
    weight: float = 1.0

class OptimizeRequest(BaseModel):
    cycles: int = Field(default=0, description="优化循环次数，0代表自动循环(Automatic)")
    use_wavefront: bool = Field(default=False, description="True: 波前差基底; False: 点列图基底")
    target_effl: Optional[float] = Field(default=None, description="严格锁定有效焦距 EFFL (mm)")
    effl_weight: float = Field(default=10.0, description="焦距锁定权重")
    max_totr: Optional[float] = Field(default=None, description="限制最大总长 TOTR <= 该值 (mm)")
    min_bfl: Optional[float] = Field(default=None, description="限制最小后截距 >= 该值 (mm)")
    minimize_axial_color: bool = Field(default=False, description="是否加入 AXCL 消除轴向色差")
    custom_operands: List[CustomOperandItem] = Field(default=[])

class EvaluateRequest(BaseModel):
    max_frequency: float = Field(default=50.0, gt=0, allow_inf_nan=False, description="MTF 最大评估空间频率 (lp/mm)")
    num_freq_points: int = Field(default=5, ge=1, description="频率采样点数量")
    mtf_sampling: int = Field(default=3, ge=1, le=4, strict=True, description="MTF 采样级别 (1–4)，Grid=0 使用稀疏积分，需逐级检查收敛")

class FileRequest(BaseModel):
    filepath: str = Field(description="完整文件路径 (.zmx)")

# ==================== 内部安全辅助函数 ====================

def _set_variable(cell, is_var: bool):
    """兼容 pythonnet 3.x 的变量状态设置"""
    solve_type = zp.constants.Editors.SolveType.Variable if is_var else zp.constants.Editors.SolveType.Fixed
    solve = cell.CreateSolveType(solve_type)
    cell.SetSolveData(solve)

def _insert_operand_row(mfe, row_idx: int, op_enum, target: float, weight: float,
                        int1=None, int2=None, data1=None, data2=None):
    """在 MFE 指定行插入操作数并写入参数 (Cell 2=Int1, 3=Int2, 4=Data1, 5=Data2)"""
    op = mfe.InsertNewOperandAt(row_idx)
    op.ChangeType(op_enum)
    op.Target = float(target)
    op.Weight = float(weight)
    column = zp.constants.Editors.MFE.MeritColumn
    if int1 is not None: op.GetOperandCell(column.Param1).IntegerValue = int(int1)
    if int2 is not None: op.GetOperandCell(column.Param2).IntegerValue = int(int2)
    if data1 is not None: op.GetOperandCell(column.Param3).DoubleValue = float(data1)
    if data2 is not None: op.GetOperandCell(column.Param4).DoubleValue = float(data2)
    return op

def _get_primary_wavelength_index(oss) -> int:
    """通过 2024 R2 支持的 IsPrimary 标记查找主波长序号。"""
    wavelengths = oss.SystemData.Wavelengths
    for index in range(1, wavelengths.NumberOfWavelengths + 1):
        if wavelengths.GetWavelength(index).IsPrimary:
            return index
    raise RuntimeError("系统未设置主波长，无法执行优化或评估")

def _get_normalized_field_coordinates(fields):
    """按系统的 Radial/Rectangular 设置转换为 RWCE 所需的 Hx、Hy。"""
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
    return [(x / x_scale if x_scale else 0.0, y / y_scale if y_scale else 0.0)
            for x, y in coordinates]

def _get_rms_spot_radii(oss):
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

def _json_safe_state(value):
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

def _extract_system_state(oss) -> Dict[str, Any]:
    """安全提取当前镜头表面与光路健康度，绝不抛未捕获异常"""
    lde = oss.LDE
    surfaces_info = []
    for i in range(lde.NumberOfSurfaces):
        s = lde.GetSurfaceAt(i)
        try:
            r, t, m, sd = s.Radius, s.Thickness, s.Material, s.SemiDiameter
        except Exception:
            r, t, m, sd = 0.0, 0.0, "", 0.0
        surfaces_info.append({
            "index": i,
            "comment": s.Comment,
            "is_stop": s.IsStop,
            "radius": round(float(r), 4) if r is not None else 0.0,
            "thickness": round(float(t), 4) if t is not None else 0.0,
            "material": str(m) if m else "",
            "semi_diameter": round(float(sd), 4) if sd is not None else 0.0
        })

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

    return _json_safe_state({
        "num_surfaces": lde.NumberOfSurfaces,
        "ray_trace_valid": ray_trace_ok,
        "rms_spot_radius": rms_spot,
        "merit_function": merit_val,
        "surfaces": surfaces_info
    })

# ==================== API 路由定义 ====================

@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(request: Request, exc: RequestValidationError):
    """校验错误保留原始输入，非有限数值也需先转换再序列化。"""
    return JSONResponse(status_code=422, content=_json_safe_state(jsonable_encoder({"detail": exc.errors()})))

@app.on_event("startup")
def startup_event():
    zm.connect()

@app.get("/status")
def get_status():
    try:
        oss = zm.connect()
        return {"status": "ok", "data": _extract_system_state(oss)}
    except Exception as e:
        return {"status": "error", "message": str(e), "trace": traceback.format_exc()}

@app.post("/load_file")
def load_zmx_file(req: FileRequest):
    try:
        oss = zm.connect()
        abs_path = os.path.abspath(req.filepath)
        if not os.path.exists(abs_path):
            return {"status": "error", "message": f"找不到文件: {abs_path}"}
        oss.load(abs_path)
        return {"status": "ok", "loaded_file": abs_path, "data": _extract_system_state(oss)}
    except Exception as e:
        return {"status": "error", "message": str(e), "trace": traceback.format_exc()}

@app.post("/build")
def build_system(req: SystemBuildRequest):
    try:
        oss = zm.connect()
        oss.new()
        oss.SystemData.Aperture.ApertureType = zp.constants.SystemData.ZemaxApertureType.EntrancePupilDiameter
        oss.SystemData.Aperture.ApertureValue = float(req.epd)
        oss.SystemData.Aperture.AfocalImageSpace = bool(req.afocal_mode)
        oss.SystemData.Wavelengths.SelectWavelengthPreset(zp.constants.SystemData.WavelengthPreset.FdC_Visible)

        fields = oss.SystemData.Fields
        fields.DeleteAllFields()
        for idx, fov in enumerate(req.fovs):
            if idx == 0:
                fields.GetField(1).Y = float(fov)
            else:
                fields.AddField(0.0, float(fov), 1.0)

        lde = oss.LDE
        while lde.NumberOfSurfaces > 3:
            lde.RemoveSurfaceAt(1)

        for idx, item in enumerate(req.surfaces, start=1):
            surf = lde.GetSurfaceAt(1) if idx == 1 else lde.InsertNewSurfaceAt(idx)
            surf.Comment = item.comment
            surf.Radius = float(item.radius)
            surf.Thickness = float(item.thickness)
            surf.Material = item.material.upper().strip()
            if item.semi_diameter is not None and item.semi_diameter > 0:
                surf.SemiDiameter = float(item.semi_diameter)
            if item.is_stop:
                surf.IsStop = True
            _set_variable(surf.RadiusCell, item.radius_variable)
            _set_variable(surf.ThicknessCell, item.thickness_variable)

        return {"status": "ok", "data": _extract_system_state(oss)}
    except Exception as e:
        return {"status": "error", "message": str(e), "trace": traceback.format_exc()}

@app.post("/patch_surface")
def patch_surface(req: SurfacePatchRequest):
    try:
        oss = zm.connect()
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
        if req.semi_diameter_auto or req.semi_diameter is not None:
            semi_cell = surf.SemiDiameterCell
            semi_type = (zp.constants.Editors.SolveType.Automatic if req.semi_diameter_auto
                         else zp.constants.Editors.SolveType.Fixed)
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
        if req.radius is not None: surf.Radius = float(req.radius)
        if req.thickness is not None: surf.Thickness = float(req.thickness)
        if req.material is not None: surf.Material = req.material.upper().strip()
        if req.radius_variable is not None: _set_variable(surf.RadiusCell, req.radius_variable)
        if req.thickness_variable is not None: _set_variable(surf.ThicknessCell, req.thickness_variable)
        return {"status": "ok", "data": _extract_system_state(oss)}
    except Exception as e:
        return {"status": "error", "message": str(e)}

@app.post("/optimize")
def run_optimization(req: OptimizeRequest):
    try:
        oss = zm.connect()
        mfe = oss.MFE
        op_enum = zp.constants.Editors.MFE.MeritOperandType
        prim_wl = _get_primary_wavelength_index(oss)
        for item in req.custom_operands:
            op_name = item.operand_type.upper().strip()
            if not hasattr(op_enum, op_name):
                return {"status": "error", "message": f"未知的操作数名称: {op_name}"}

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
        # 每次按本次请求重建，避免向导保留 DMFS 上方的旧约束并重复累积。
        if mfe.NumberOfOperands > 0:
            mfe.RemoveOperandsAt(1, mfe.NumberOfOperands)
        wizard.StartAt = 1
        wizard.IsAddFavoriteOperandsUsed = False
        wizard.Apply()

        curr_row = 1
        if req.target_effl is not None:
            _insert_operand_row(mfe, curr_row, op_enum.EFFL, req.target_effl, req.effl_weight, int2=prim_wl)
            curr_row += 1
        if req.minimize_axial_color:
            _insert_operand_row(mfe, curr_row, op_enum.AXCL, 0.0, 5.0, int1=1, int2=oss.SystemData.Wavelengths.NumberOfWavelengths)
            curr_row += 1
        if req.max_totr is not None:
            totr_row = curr_row
            _insert_operand_row(mfe, totr_row, op_enum.TOTR, 0.0, 0.0)
            curr_row += 1
            _insert_operand_row(mfe, curr_row, op_enum.OPLT, req.max_totr, 5.0, int1=totr_row)
            curr_row += 1
        if req.min_bfl is not None:
            last_surf_idx = oss.LDE.NumberOfSurfaces - 2
            if last_surf_idx >= 1:
                tthi_row = curr_row
                _insert_operand_row(mfe, tthi_row, op_enum.TTHI, 0.0, 0.0, int1=last_surf_idx, int2=last_surf_idx)
                curr_row += 1
                _insert_operand_row(mfe, curr_row, op_enum.OPGT, req.min_bfl, 5.0, int1=tthi_row)
                curr_row += 1
        for item in req.custom_operands:
            op_name = item.operand_type.upper().strip()
            _insert_operand_row(mfe, curr_row, getattr(op_enum, op_name), item.target, item.weight,
                                item.int1, item.int2, item.data1, item.data2)
            curr_row += 1

        initial_mf = float(mfe.CalculateMeritFunction())
        if initial_mf > 1e6:
            return {
                "status": "error",
                "message": f"初始结构光线追迹失败(Merit={initial_mf:.2e})！发生全反射(TIR)或切光，请减缓曲率半径后再优化。",
                "data": _extract_system_state(oss)
            }

        opt = oss.Tools.OpenLocalOptimization()
        if opt is None:
            raise RuntimeError("无法打开优化工具，请检查是否有其他工具正在运行")
        try:
            opt.Algorithm = zp.constants.Tools.Optimization.OptimizationAlgorithm.DampedLeastSquares
            opt.Cycles = (zp.constants.Tools.Optimization.OptimizationCycles.Automatic if req.cycles == 0
                          else zp.constants.Tools.Optimization.OptimizationCycles.Fixed_10_Cycles)
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
            "data": _extract_system_state(oss)
        }
    except Exception as e:
        return {"status": "error", "message": str(e), "trace": traceback.format_exc()}

@app.post("/evaluate")
def evaluate_image_quality(req: EvaluateRequest):
    try:
        oss = zm.connect()
        mfe = oss.MFE
        op_type = zp.constants.Editors.MFE.MeritOperandType
        num_fields = oss.SystemData.Fields.NumberOfFields
        prim_wl = _get_primary_wavelength_index(oss)
        field_coordinates = _get_normalized_field_coordinates(oss.SystemData.Fields)
        # 操作数的 0 值不能单独证明追迹成功，先运行有 Succeeded 状态的独立工具。
        _get_rms_spot_radii(oss)
        issues = []

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
            wavefront_results.append({
                "field_index": f_idx,
                "field_y_deg": fov_y,
                "rms_wavefront_waves": round(rms_wf, 4),
                "wavefront_valid": wavefront_valid,
                "diffraction_limited": bool(wavefront_valid and rms_wf < 0.0715),
                "strehl_ratio_approx": round(strehl, 4) if strehl is not None else None
            })

        freq_step = req.max_frequency / max(req.num_freq_points, 1)
        frequencies = [round(freq_step * i, 2) for i in range(1, req.num_freq_points + 1)]
        mtf_results = []
        for f_idx in range(1, num_fields + 1):
            fov_y = float(oss.SystemData.Fields.GetField(f_idx).Y)
            curve_data = []
            for freq in frequencies:
                mtf_t = float(mfe.GetOperandValue(op_type.MTFT, req.mtf_sampling, 0, f_idx, freq, 0.0, 0.0, 0.0, 0.0))
                mtf_s = float(mfe.GetOperandValue(op_type.MTFS, req.mtf_sampling, 0, f_idx, freq, 0.0, 0.0, 0.0, 0.0))
                # 2024 R2 手册 p.1078：Field=0、Grid=1 返回衍射极限。
                mtf_dl = float(mfe.GetOperandValue(op_type.MTFA, req.mtf_sampling, 0, 0.0, freq, 1.0, 0.0, 0.0, 0.0))
                mtf_valid = all(math.isfinite(value) and 0.0 <= value <= 1.0 for value in (mtf_t, mtf_s, mtf_dl))
                if not mtf_valid:
                    issues.append(f"视场 {f_idx} 在频率 {freq} 的 MTF 返回无效数值")
                curve_data.append({
                    "frequency": freq,
                    "mtf_valid": mtf_valid,
                    "tangential": round(mtf_t, 4),
                    "sagittal": round(mtf_s, 4),
                    "average": round((mtf_t + mtf_s) / 2.0, 4),
                    "diffraction_limit": round(mtf_dl, 4)
                })
            mtf_results.append({"field_index": f_idx, "field_y_deg": fov_y, "mtf_curve": curve_data})

        effl = float(mfe.GetOperandValue(op_type.EFFL, 0, prim_wl, 0, 0.0, 0.0, 0.0, 0.0, 0.0))
        wfno = float(mfe.GetOperandValue(op_type.WFNO, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0))

        if not all(math.isfinite(value) and abs(value) < 1e6 for value in (effl, wfno)):
            issues.append("焦距或工作 F 数返回非有限数值或错误哨兵")
        return _json_safe_state({
            "status": "error" if issues else "ok",
            "evaluation_valid": not issues,
            "issues": issues,
            "mtf_sampling": req.mtf_sampling,
            "mtf_grid": 0,
            "mtf_diffraction_limit_grid": 1,
            "mtf_sampling_converged": None,
            "paraxial_specs": {"effective_focal_length_mm": round(effl, 3), "working_f_number": round(wfno, 3)},
            "wavefront_summary": wavefront_results,
            "mtf_summary": mtf_results
        })
    except Exception as e:
        return {"status": "error", "message": str(e), "trace": traceback.format_exc()}

@app.post("/save")
def save_file(req: FileRequest):
    try:
        oss = zm.connect()
        abs_path = os.path.abspath(req.filepath)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        oss.save_as(abs_path)
        return {"status": "ok", "saved_to": abs_path}
    except Exception as e:
        return {"status": "error", "message": str(e)}

if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8765, workers=1, loop="asyncio")
