"""Pydantic request and validation schemas for Zemax optical operations."""

from typing import Dict, List, Optional, Union
from pydantic import BaseModel, Field, field_validator, model_validator


VALID_EVEN_ASPHERE_ORDERS = (2, 4, 6, 8, 10, 12, 14, 16)


def _validate_asphere_orders(orders) -> None:
    for k in orders:
        if int(k) not in VALID_EVEN_ASPHERE_ORDERS:
            raise ValueError(f"偶次非球面仅支持阶次 {VALID_EVEN_ASPHERE_ORDERS}，收到: {k}")


class SurfaceItem(BaseModel):
    comment: str = Field(default="", description="表面备注名称")
    surface_type: str = Field(
        default="Standard", description="表面类型: 'Standard' (标准球面/圆锥) 或 'EvenAsphere' (偶次非球面)"
    )
    radius: float = Field(default=0.0, description="曲率半径(mm)，0代表无穷大平面")
    thickness: float = Field(default=0.0, description="厚度/空气间隔(mm)")
    material: str = Field(default="", description="材料牌号(如 N-BK7)，空气留空")
    conic: float = Field(default=0.0, allow_inf_nan=False, description="圆锥常数 (Conic k)，0为球面，-1为抛物面")
    semi_diameter: Optional[float] = Field(default=None, description="半口径(mm)，留空则自动计算")
    is_stop: bool = Field(default=False, description="是否为光阑面(STO)")
    radius_variable: bool = Field(default=False, description="曲率半径是否设为变量(V)")
    thickness_variable: bool = Field(default=False, description="厚度是否设为变量(V)")
    conic_variable: bool = Field(default=False, description="圆锥常数是否设为变量(V)")
    material_substitute: bool = Field(default=False, description="材料是否设为 Hammer 替换变量(Substitute)")
    substitute_catalog: str = Field(default="SCHOTT", description="玻璃替换库名称，如 SCHOTT, OHARA, CDGM")
    asphere_terms: Optional[Dict[int, float]] = Field(
        default=None, description="偶次非球面高次项系数，键为阶次 2,4,6,8,10,12,14,16"
    )
    asphere_terms_variable: Optional[List[int]] = Field(
        default=None, description="设为变量的偶次非球面阶次列表，如 [4, 6, 8]"
    )
    marginal_ray_height_solve: bool = Field(
        default=False, description="厚度是否设为边缘光线高度求解(高斯像面)"
    )

    @field_validator("asphere_terms")
    @classmethod
    def _check_asphere_terms(cls, v: Optional[Dict[int, float]]) -> Optional[Dict[int, float]]:
        if v is not None:
            _validate_asphere_orders(v.keys())
        return v

    @field_validator("asphere_terms_variable")
    @classmethod
    def _check_asphere_vars(cls, v: Optional[List[int]]) -> Optional[List[int]]:
        if v is not None:
            _validate_asphere_orders(v)
        return v


class FieldItem(BaseModel):
    x: float = Field(default=0.0, allow_inf_nan=False, description="X方向视场角 (度)")
    y: float = Field(default=0.0, allow_inf_nan=False, description="Y方向视场角 (度)")
    weight: float = Field(default=1.0, gt=0, allow_inf_nan=False, description="视场权重")


class SystemBuildRequest(BaseModel):
    epd: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False, description="入瞳直径 (mm)，向后兼容简写")
    aperture_type: str = Field(
        default="EntrancePupilDiameter",
        description="孔径类型: 'EntrancePupilDiameter', 'ImageSpaceFNum', 'ObjectSpaceNA', 'FloatByStopSize'",
    )
    aperture_value: Optional[float] = Field(
        default=None, gt=0, allow_inf_nan=False, description="孔径值（若未填则回退使用 epd）"
    )
    fovs: List[Union[float, FieldItem]] = Field(
        default=[0.0], description="视场列表：可传 Y 视场角浮点数，或 {x, y, weight} 对象"
    )
    afocal_mode: bool = Field(default=False, description="是否开启无焦像空间模式(望远系统用)")
    surfaces: List[SurfaceItem] = Field(description="从第1面到像面前一面的表面列表")
    wavelength_preset: str = Field(
        default="HeNe_0p6328", description="波长预设，可选 HeNe_0p6328 或 FdC_Visible"
    )
    wavelengths: Optional[List[float]] = Field(
        default=None, description="自定义波长列表 (μm)，如 [0.4861, 0.5876, 0.6563]，指定后覆盖 wavelength_preset"
    )
    primary_wavelength_index: int = Field(
        default=1, ge=1, description="自定义波长时的主波长序号 (1-based)"
    )

    @model_validator(mode="after")
    def _validate_aperture_and_wavelengths(self) -> "SystemBuildRequest":
        ap_norm = self.aperture_type.strip().lower()
        if ap_norm != "floatbystopsize" and self.aperture_value is None and self.epd is None:
            raise ValueError("必须提供 epd 或 aperture_value（FloatByStopSize 除外）")
        if self.wavelengths is not None:
            if len(self.wavelengths) == 0:
                raise ValueError("自定义 wavelengths 不能为空")
            if any(w <= 0 for w in self.wavelengths):
                raise ValueError("波长必须为正数 (μm)")
            if self.primary_wavelength_index > len(self.wavelengths):
                raise ValueError("primary_wavelength_index 超出 wavelengths 长度")
        return self


class SurfacePatchRequest(BaseModel):
    surface_index: int = Field(description="要修改的表面序号 (从1开始)")
    surface_type: Optional[str] = Field(
        default=None, description="切换表面类型: 'Standard' 或 'EvenAsphere'"
    )
    radius: Optional[float] = None
    thickness: Optional[float] = None
    material: Optional[str] = None
    conic: Optional[float] = Field(default=None, allow_inf_nan=False, description="圆锥常数 (Conic k)")
    radius_variable: Optional[bool] = None
    thickness_variable: Optional[bool] = None
    conic_variable: Optional[bool] = Field(default=None, description="圆锥常数是否设为变量(V)")
    material_substitute: Optional[bool] = Field(
        default=None, description="是否开启/关闭 Hammer 玻璃替换变量 (MaterialSubstitute)"
    )
    substitute_catalog: str = Field(default="SCHOTT", description="玻璃替换库名称，如 SCHOTT, OHARA, CDGM")
    asphere_terms: Optional[Dict[int, float]] = Field(
        default=None, description="设置偶次非球面高次项系数，键为阶次 2,4,6,8,10,12,14,16"
    )
    asphere_terms_variable: Optional[Dict[int, bool]] = Field(
        default=None, description="设置偶次非球面高次项变量开关，如 {4: true, 6: false}"
    )
    marginal_ray_height_solve: Optional[bool] = Field(
        default=None, description="是否将厚度设置为边缘光线高度求解"
    )
    semi_diameter: Optional[float] = Field(
        default=None, gt=0, allow_inf_nan=False, description="固定半口径(mm)，必须为正数"
    )
    semi_diameter_auto: bool = Field(default=False, strict=True, description="恢复半口径 Automatic 求解")
    is_stop: Optional[bool] = Field(
        default=None, strict=True, description="True 将光阑移至该面；不能用 False 取消现有光阑"
    )

    @field_validator("asphere_terms", "asphere_terms_variable")
    @classmethod
    def _check_patch_asphere_orders(cls, v):
        if v is not None:
            _validate_asphere_orders(v.keys())
        return v


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
    min_edge_thickness: Optional[float] = Field(
        default=None, gt=0, allow_inf_nan=False, description="限制玻璃最小边缘厚度 MNEG >= 该值 (mm)"
    )
    minimize_axial_color: bool = Field(default=False, description="是否加入 AXCL 消除轴向色差")
    use_trac_spherical_mf: bool = Field(
        default=False, description="使用讲义专用的6孔径带TRAC+MNCA+MXCA+EFFL评价函数"
    )
    custom_operands: List[CustomOperandItem] = Field(default=[])


class EvaluateRequest(BaseModel):
    max_frequency: float = Field(
        default=50.0, gt=0, allow_inf_nan=False, description="MTF 最大评估空间频率 (lp/mm)"
    )
    num_freq_points: int = Field(default=5, ge=1, description="频率采样点数量")
    mtf_sampling: int = Field(
        default=3, ge=1, le=4, strict=True, description="MTF 采样级别 (1–4)，Grid=0 使用稀疏积分，需逐级检查收敛"
    )


class ParameterTuneRequest(BaseModel):
    surface_index: int = Field(ge=1, description="要修改的表面序号 (从1开始)")
    parameter: str = Field(
        description="调参变量名称: 'radius', 'thickness', 'semi_diameter', 'conic'"
    )
    min_value: float = Field(allow_inf_nan=False, description="扫描起始/最小值")
    max_value: float = Field(allow_inf_nan=False, description="扫描终止/最大值")
    steps: int = Field(default=10, ge=2, le=100, description="采样点数 (默认 10 点，范围 2~100)")
    criterion: str = Field(
        default="geo_spot",
        description="优化/择优目标: 'geo_spot', 'rms_spot', 'merit_function', 'effl_diff'",
    )
    target_effl: Optional[float] = Field(
        default=None, allow_inf_nan=False, description="若 criterion 为 'effl_diff' 时的目标焦距 (mm)"
    )
    quick_focus: bool = Field(default=False, description="每个采样点是否先执行快速聚焦")
    apply_best: bool = Field(default=False, description="是否自动将表现最佳的参数应用到当前系统")


class FileRequest(BaseModel):
    filepath: str = Field(description="完整文件路径 (.zmx)")


class SnapshotCreateRequest(BaseModel):
    label: str = Field(default="manual", description="快照备注标签")


class SnapshotRollbackRequest(BaseModel):
    snapshot_id: str = Field(description="要回滚的目标快照 ID")


class ExportLayoutRequest(BaseModel):
    viewer_type: str = Field(
        default="cross_section",
        description="视图类型: 'cross_section' (2D光路), 'viewer_3d' (3D视图), 'shaded_model' (实体模型)",
    )
    output_path: Optional[str] = Field(
        default=None, description="可选输出 PNG 路径，留空自动保存到 outputs/exports/"
    )


class HammerOptimizeRequest(BaseModel):
    target_run_time_minutes: float = Field(
        default=0.25, gt=0.0, le=3.0, allow_inf_nan=False, description="Hammer 限时运行时间 (分钟，上限 3.0)"
    )
    automatic_optimization: bool = Field(default=True, description="是否启用 Hammer 自动优化")
    use_wavefront: bool = Field(default=False, description="True: 波前差基底; False: 点列图基底")
    target_effl: Optional[float] = Field(default=None, description="严格锁定有效焦距 EFFL (mm)")
    effl_weight: float = Field(default=10.0, description="焦距锁定权重")
    max_totr: Optional[float] = Field(default=None, description="限制最大总长 TOTR <= 该值 (mm)")
    min_bfl: Optional[float] = Field(default=None, description="限制最小后截距 >= 该值 (mm)")
    min_edge_thickness: Optional[float] = Field(
        default=None, gt=0, allow_inf_nan=False, description="限制玻璃最小边缘厚度 MNEG >= 该值 (mm)"
    )
    minimize_axial_color: bool = Field(default=False, description="是否加入 AXCL 消除轴向色差")
    use_trac_spherical_mf: bool = Field(default=False, description="使用讲义专用 6 孔径带 TRAC 评价函数")
    custom_operands: List[CustomOperandItem] = Field(default=[])


class SynthesizeDesignRequest(BaseModel):
    topology: str = Field(
        description="初始结构拓扑类型: 'singlet_min_spha', 'achromatic_doublet', 'cooke_triplet'"
    )
    target_effl: float = Field(gt=0, allow_inf_nan=False, description="目标有效焦距 (mm)")
    epd: float = Field(gt=0, allow_inf_nan=False, description="入瞳直径 EPD (mm)")
    fovs: List[float] = Field(default=[0.0], description="Y方向视场角列表 (度)")
    glasses: Optional[List[str]] = Field(
        default=None, description="指定玻璃牌号列表，如 ['N-BK7', 'N-F2'] 或 ['N-SK16', 'N-F2', 'N-SK16']"
    )
    edge_thickness: float = Field(default=1.5, gt=0.2, allow_inf_nan=False, description="边缘最小保留厚度 (mm)")
    wavelength_preset: Optional[str] = Field(default=None, description="可选波长预设覆盖")
    apply_to_system: bool = Field(default=True, description="是否立即构建到 OpticStudio 当前系统中")


class ScaleSystemRequest(BaseModel):
    scale_factor: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False, description="直接缩放倍率")
    target_effl: Optional[float] = Field(default=None, gt=0, allow_inf_nan=False, description="按目标焦距反算缩放倍率")

    @model_validator(mode="after")
    def _check_scale_input(self) -> "ScaleSystemRequest":
        if self.scale_factor is None and self.target_effl is None:
            raise ValueError("必须提供 scale_factor 或 target_effl")
        return self


class SplitLensRequest(BaseModel):
    surface_index: int = Field(ge=1, description="要分裂透镜的前表面序号 (须含玻璃材料)")
    split_ratio: float = Field(default=0.5, gt=0.1, lt=0.9, allow_inf_nan=False, description="光焦度分配比例 (0.1~0.9)")
    air_gap: float = Field(default=1.0, ge=0.1, allow_inf_nan=False, description="分裂后两透镜间的空气间隔 (mm)")
    min_edge_thickness: float = Field(default=1.2, gt=0.2, allow_inf_nan=False, description="分裂后每片透镜最小边缘厚度 (mm)")


class PromoteAsphereRequest(BaseModel):
    surface_index: int = Field(ge=1, description="目标表面序号")
    max_order: int = Field(default=6, description="最高释放偶次非球面阶次 (4, 6, 8)")
    target_effl: Optional[float] = Field(default=None, description="可选锁定有效焦距 (mm)")
    min_edge_thickness: float = Field(default=1.2, gt=0.2, allow_inf_nan=False, description="最小玻璃边缘厚度 MNEG (mm)")
    use_wavefront: bool = Field(default=True, description="是否以波前差为优化基底")

    @field_validator("max_order")
    @classmethod
    def _check_order(cls, v: int) -> int:
        if v not in (4, 6, 8):
            raise ValueError("max_order 仅支持 4, 6, 8")
        return v
