"""Native MCP Server (Streamable HTTP & stdio) for Ansys Zemax OpticStudio 2024 R2."""

from __future__ import annotations

import argparse
import base64
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from mcp.server.mcpserver import Image, MCPServer
from mcp.types import ToolAnnotations

from zemax_mcp import __version__
from zemax_mcp.core.connection import ZemaxConnectionManager
from zemax_mcp.core.snapshots import SnapshotManager
from zemax_mcp.schemas.optical import (
    CustomOperandItem,
    EvaluateRequest,
    FieldItem,
    FileRequest,
    HammerOptimizeRequest,
    OptimizeRequest,
    ParameterTuneRequest,
    PromoteAsphereRequest,
    ScaleSystemRequest,
    SplitLensRequest,
    SurfaceItem,
    SurfacePatchRequest,
    SynthesizeDesignRequest,
    SystemBuildRequest,
)
from zemax_mcp.services import export_service, optical_service, synthesis_service, topology_service


OPTICAL_INSTRUCTIONS = (
    "Ansys Zemax OpticStudio 2024 R2 光学设计 MCP 服务。"
    "有焦系统调用 zemax_optimize 时必须指定 target_effl；"
    "若出现 9E9 光线追迹失败，请先用 zemax_patch_surface 放大曲率半径，禁止原地重试优化；"
    "单面区间寻优请直接调用 zemax_tune_parameter，避免逐轮手动盲猜。"
)


def create_mcp_server(
    connection_manager: Optional[ZemaxConnectionManager] = None,
    snapshot_manager: Optional[SnapshotManager] = None,
) -> MCPServer:
    """Create and configure the Zemax MCPServer instance."""
    zm = connection_manager or ZemaxConnectionManager()
    sm = snapshot_manager or SnapshotManager()
    mcp = MCPServer(
        name="zemax-opticstudio",
        title="Ansys Zemax OpticStudio 2024 R2",
        version=__version__,
        description="Automated optical design, optimization, evaluation, and parameter tuning via ZOS-API.",
        instructions=OPTICAL_INSTRUCTIONS,
    )

    # ==================== MCP Tools ====================

    @mcp.tool(
        name="zemax_get_status",
        title="Get Optical System Status",
        description="读取当前 Zemax 镜头表面参数表、RMS/GEO 光斑尺寸、评价函数及光路追迹健康状态 (ray_trace_valid)。",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def zemax_get_status() -> Dict[str, Any]:
        return optical_service.safe_call(
            lambda: zm.run_locked(optical_service.get_status)
        )

    @mcp.tool(
        name="zemax_load_file",
        title="Load ZMX Lens File",
        description="加载指定的 .zmx 镜头文件（如官方 Samples 或本地已保存设计），加载前自动创建快照并支持 zemax_undo。",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def zemax_load_file(filepath: str) -> Dict[str, Any]:
        req = FileRequest(filepath=filepath)
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: optical_service.load_zmx_file(oss, req, snapshot_manager=sm)
            )
        )

    @mcp.tool(
        name="zemax_save_file",
        title="Save ZMX Lens File",
        description="将当前 Zemax 光学系统保存到指定 .zmx 文件路径。",
        annotations=ToolAnnotations(idempotentHint=True),
    )
    def zemax_save_file(filepath: str) -> Dict[str, Any]:
        req = FileRequest(filepath=filepath)
        return optical_service.safe_call(
            lambda: zm.run_locked(lambda oss: optical_service.save_zmx_file(oss, req)),
            include_trace=False,
        )

    @mcp.tool(
        name="zemax_build_system",
        title="Build Optical System",
        description=(
            "根据结构参数从零构建全新光学系统（会替换当前系统，构建前自动创建快照，支持 zemax_undo）。"
            "支持多种孔径类型 (EntrancePupilDiameter / ImageSpaceFNum / ObjectSpaceNA / FloatByStopSize)、"
            "X/Y 视场权重及自定义波长列表；平面曲率半径填 0.0。"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def zemax_build_system(
        surfaces: List[SurfaceItem],
        epd: Optional[float] = None,
        aperture_type: str = "EntrancePupilDiameter",
        aperture_value: Optional[float] = None,
        fovs: List[float | FieldItem] = [0.0],
        afocal_mode: bool = False,
        wavelength_preset: str = "HeNe_0p6328",
        wavelengths: Optional[List[float]] = None,
        primary_wavelength_index: int = 1,
    ) -> Dict[str, Any]:
        req = SystemBuildRequest(
            epd=epd,
            aperture_type=aperture_type,
            aperture_value=aperture_value,
            fovs=fovs,
            afocal_mode=afocal_mode,
            surfaces=surfaces,
            wavelength_preset=wavelength_preset,
            wavelengths=wavelengths,
            primary_wavelength_index=primary_wavelength_index,
        )
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: optical_service.build_system(oss, req, snapshot_manager=sm)
            )
        )

    @mcp.tool(
        name="zemax_patch_surface",
        title="Patch Single Surface",
        description=(
            "修改单个表面（1 至 N-2）的表面类型（Standard / EvenAsphere）、曲率半径、厚度、材料、圆锥常数 Conic、"
            "偶次非球面高次项系数（2..16阶）、变量标记、边缘光线高度求解、半口径或移动光阑（is_stop=True）。"
        ),
        annotations=ToolAnnotations(idempotentHint=True),
    )
    def zemax_patch_surface(
        surface_index: int,
        surface_type: Optional[str] = None,
        radius: Optional[float] = None,
        thickness: Optional[float] = None,
        material: Optional[str] = None,
        conic: Optional[float] = None,
        radius_variable: Optional[bool] = None,
        thickness_variable: Optional[bool] = None,
        conic_variable: Optional[bool] = None,
        material_substitute: Optional[bool] = None,
        substitute_catalog: str = "SCHOTT",
        asphere_terms: Optional[Dict[int, float]] = None,
        asphere_terms_variable: Optional[Dict[int, bool]] = None,
        marginal_ray_height_solve: Optional[bool] = None,
        semi_diameter: Optional[float] = None,
        semi_diameter_auto: bool = False,
        is_stop: Optional[bool] = None,
    ) -> Dict[str, Any]:
        req = SurfacePatchRequest(
            surface_index=surface_index,
            surface_type=surface_type,
            radius=radius,
            thickness=thickness,
            material=material,
            conic=conic,
            radius_variable=radius_variable,
            thickness_variable=thickness_variable,
            conic_variable=conic_variable,
            material_substitute=material_substitute,
            substitute_catalog=substitute_catalog,
            asphere_terms=asphere_terms,
            asphere_terms_variable=asphere_terms_variable,
            marginal_ray_height_solve=marginal_ray_height_solve,
            semi_diameter=semi_diameter,
            semi_diameter_auto=semi_diameter_auto,
            is_stop=is_stop,
        )
        return optical_service.safe_call(
            lambda: zm.run_locked(lambda oss: optical_service.patch_surface(oss, req)),
            include_trace=False,
        )

    @mcp.tool(
        name="zemax_quick_focus",
        title="Quick Focus",
        description="调用 Zemax QuickFocus 工具（SpotSizeRadial + Centroid）寻找当前最佳像面位置。",
    )
    def zemax_quick_focus() -> Dict[str, Any]:
        return optical_service.safe_call(
            lambda: zm.run_locked(optical_service.quick_focus)
        )

    @mcp.tool(
        name="zemax_optimize",
        title="Optimize Optical System (DLS)",
        description=(
            "按本次请求重建评价函数并执行阻尼最小二乘（DLS）局部优化。"
            "有焦系统必须提供 target_effl；支持波前差基底、轴向色差 AXCL、总长上限 max_totr、"
            "最小后截距 min_bfl、6孔径带 TRAC 评价函数及自定义操作数。"
        ),
    )
    def zemax_optimize(
        cycles: int = 0,
        use_wavefront: bool = False,
        target_effl: Optional[float] = None,
        effl_weight: float = 10.0,
        max_totr: Optional[float] = None,
        min_bfl: Optional[float] = None,
        min_edge_thickness: Optional[float] = None,
        minimize_axial_color: bool = False,
        use_trac_spherical_mf: bool = False,
        custom_operands: List[CustomOperandItem] = [],
    ) -> Dict[str, Any]:
        req = OptimizeRequest(
            cycles=cycles,
            use_wavefront=use_wavefront,
            target_effl=target_effl,
            effl_weight=effl_weight,
            max_totr=max_totr,
            min_bfl=min_bfl,
            min_edge_thickness=min_edge_thickness,
            minimize_axial_color=minimize_axial_color,
            use_trac_spherical_mf=use_trac_spherical_mf,
            custom_operands=custom_operands,
        )
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: optical_service.run_optimization(oss, req, snapshot_manager=sm)
            )
        )

    @mcp.tool(
        name="zemax_evaluate",
        title="Evaluate Image Quality (Wavefront & MTF)",
        description=(
            "执行深度像质评估：各视场波前 RMS（waves）、Maréchal 衍射极限判据（<0.0715）、"
            "近似 Strehl 比、多色光 MTFT/MTFS 曲线及衍射极限参考曲线。"
        ),
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def zemax_evaluate(
        max_frequency: float = 50.0,
        num_freq_points: int = 5,
        mtf_sampling: int = 3,
    ) -> Dict[str, Any]:
        req = EvaluateRequest(
            max_frequency=max_frequency,
            num_freq_points=num_freq_points,
            mtf_sampling=mtf_sampling,
        )
        return optical_service.safe_call(
            lambda: zm.run_locked(lambda oss: optical_service.evaluate_image_quality(oss, req))
        )

    @mcp.tool(
        name="zemax_tune_parameter",
        title="Sweep & Tune Surface Parameter",
        description=(
            "在指定区间 [min_value, max_value] 对单个表面的 radius / thickness / semi_diameter "
            "进行自动化扫描评估与趋势诊断。自带沙箱回滚保护，仅当 apply_best=True 时写入最优值。"
        ),
    )
    def zemax_tune_parameter(
        surface_index: int,
        parameter: str,
        min_value: float,
        max_value: float,
        steps: int = 10,
        criterion: str = "geo_spot",
        target_effl: Optional[float] = None,
        quick_focus: bool = False,
        apply_best: bool = False,
    ) -> Dict[str, Any]:
        req = ParameterTuneRequest(
            surface_index=surface_index,
            parameter=parameter,
            min_value=min_value,
            max_value=max_value,
            steps=steps,
            criterion=criterion,
            target_effl=target_effl,
            quick_focus=quick_focus,
            apply_best=apply_best,
        )
        return optical_service.safe_call(
            lambda: zm.run_locked(lambda oss: optical_service.tune_parameter(oss, req))
        )

    @mcp.tool(
        name="zemax_snapshot_create",
        title="Create System Snapshot",
        description="为当前 Zemax 光学系统创建内存/临时快照，以便后续随时使用 zemax_undo 或 zemax_rollback 回退。",
    )
    def zemax_snapshot_create(label: str = "manual") -> Dict[str, Any]:
        return optical_service.safe_call(
            lambda: {"status": "ok", "snapshot": zm.run_locked(lambda oss: sm.create(oss, label=label))},
            include_trace=False,
        )

    @mcp.tool(
        name="zemax_undo",
        title="Undo Last System Change",
        description="弹出最近一次快照并恢复 OpticStudio 系统状态（支持撤回 build / load / optimize / 手动快照）。",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def zemax_undo() -> Dict[str, Any]:
        def _undo():
            entry = zm.run_locked(sm.undo)
            state = zm.run_locked(optical_service.extract_system_state)
            return {"status": "ok", "restored_snapshot": entry, "data": state}

        return optical_service.safe_call(_undo, include_trace=False)

    @mcp.tool(
        name="zemax_rollback",
        title="Rollback to Specific Snapshot",
        description="回滚到指定 snapshot_id 的历史快照状态。",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def zemax_rollback(snapshot_id: str) -> Dict[str, Any]:
        def _rb():
            entry = zm.run_locked(lambda oss: sm.rollback_to(oss, snapshot_id))
            state = zm.run_locked(optical_service.extract_system_state)
            return {"status": "ok", "restored_snapshot": entry, "data": state}

        return optical_service.safe_call(_rb, include_trace=False)

    @mcp.tool(
        name="zemax_list_snapshots",
        title="List Available Snapshots",
        description="列出当前会话可用的历史快照列表（按最新到最旧排序）。",
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def zemax_list_snapshots() -> Dict[str, Any]:
        return {"status": "ok", "snapshots": sm.list_snapshots()}

    @mcp.tool(
        name="zemax_get_aberrations",
        title="Get Seidel Aberrations & Attribution",
        description=(
            "提取系统级与逐面赛德尔像差 (SPHA/COMA/ASTI/FCUR/DIST/PETZ) 及色差 (AXCL/LACL)，"
            "自动计算表面贡献占比、判别三阶/高级像差主导区，并给出下一步归因建议。"
        ),
        annotations=ToolAnnotations(readOnlyHint=True, idempotentHint=True),
    )
    def zemax_get_aberrations() -> Dict[str, Any]:
        return optical_service.safe_call(
            lambda: zm.run_locked(optical_service.get_aberrations)
        )

    @mcp.tool(
        name="zemax_hammer_optimize",
        title="Timed Hammer Global Escape Optimization",
        description=(
            "运行限时 Hammer 全局优化以跳出 DLS 局部极小值（支持在表面开启 material_substitute 自动换玻璃）。"
            "执行前自动创建快照并支持 zemax_undo 回滚，target_run_time_minutes 上限 3.0 分钟。"
        ),
    )
    def zemax_hammer_optimize(
        target_run_time_minutes: float = 0.25,
        automatic_optimization: bool = True,
        use_wavefront: bool = False,
        target_effl: Optional[float] = None,
        effl_weight: float = 10.0,
        max_totr: Optional[float] = None,
        min_bfl: Optional[float] = None,
        min_edge_thickness: Optional[float] = None,
        minimize_axial_color: bool = False,
        use_trac_spherical_mf: bool = False,
        custom_operands: List[CustomOperandItem] = [],
    ) -> Dict[str, Any]:
        req = HammerOptimizeRequest(
            target_run_time_minutes=target_run_time_minutes,
            automatic_optimization=automatic_optimization,
            use_wavefront=use_wavefront,
            target_effl=target_effl,
            effl_weight=effl_weight,
            max_totr=max_totr,
            min_bfl=min_bfl,
            min_edge_thickness=min_edge_thickness,
            minimize_axial_color=minimize_axial_color,
            use_trac_spherical_mf=use_trac_spherical_mf,
            custom_operands=custom_operands,
        )
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: optical_service.run_hammer_optimization(oss, req, snapshot_manager=sm)
            )
        )

    @mcp.tool(
        name="zemax_synthesize_initial_design",
        title="Synthesize Initial Optical Design (P-W / First-Order)",
        description=(
            "基于 Coddington / P-W 初阶薄透镜理论自动综合无 TIR 的初始结构："
            "支持 'singlet_min_spha'、'achromatic_doublet'、'cooke_triplet'，"
            "默认立即构建到 OpticStudio（前会自动压入快照）。"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def zemax_synthesize_initial_design(
        topology: str,
        target_effl: float,
        epd: float,
        fovs: List[float] = [0.0],
        glasses: Optional[List[str]] = None,
        edge_thickness: float = 1.5,
        wavelength_preset: Optional[str] = None,
        apply_to_system: bool = True,
    ) -> Dict[str, Any]:
        req = SynthesizeDesignRequest(
            topology=topology,
            target_effl=target_effl,
            epd=epd,
            fovs=fovs,
            glasses=glasses,
            edge_thickness=edge_thickness,
            wavelength_preset=wavelength_preset,
            apply_to_system=apply_to_system,
        )
        if apply_to_system:
            return optical_service.safe_call(
                lambda: zm.run_locked(
                    lambda oss: synthesis_service.synthesize_initial_design(
                        req, oss=oss, snapshot_manager=sm
                    )
                )
            )
        return optical_service.safe_call(
            lambda: synthesis_service.synthesize_initial_design(req, oss=None)
        )

    @mcp.tool(
        name="zemax_scale_system",
        title="Scale Optical System",
        description="按缩放倍率 scale_factor 或目标焦距 target_effl 等比缩放当前镜头（常用于官方 Sample 快速适配指标），自动保存快照。",
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def zemax_scale_system(
        scale_factor: Optional[float] = None,
        target_effl: Optional[float] = None,
    ) -> Dict[str, Any]:
        req = ScaleSystemRequest(scale_factor=scale_factor, target_effl=target_effl)
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: synthesis_service.scale_system(oss, req, snapshot_manager=sm)
            )
        )

    @mcp.tool(
        name="zemax_split_lens",
        title="Split Lens Element (Topology Evolution)",
        description=(
            "原子级安全透镜分裂：保持总光焦度将单片强弯曲透镜拆为两片同材料透镜以降低高级球差。"
            "分裂前自动压入快照，若分裂后光线追迹失败则自动回滚。"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def zemax_split_lens(
        surface_index: int,
        split_ratio: float = 0.5,
        air_gap: float = 1.0,
        min_edge_thickness: float = 1.2,
    ) -> Dict[str, Any]:
        req = SplitLensRequest(
            surface_index=surface_index,
            split_ratio=split_ratio,
            air_gap=air_gap,
            min_edge_thickness=min_edge_thickness,
        )
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: topology_service.split_lens(oss, req, snapshot_manager=sm)
            )
        )

    @mcp.tool(
        name="zemax_promote_asphere",
        title="Progressively Promote Surface to Even Asphere",
        description=(
            "自动分阶段非球面升级：安全快照 → 转 EvenAsphere → 先优化 Conic → 再释放高次项 (4/6/8阶) 并施加 MNEG 边缘厚度保护。"
        ),
        annotations=ToolAnnotations(destructiveHint=True),
    )
    def zemax_promote_asphere(
        surface_index: int,
        max_order: int = 6,
        target_effl: Optional[float] = None,
        min_edge_thickness: float = 1.2,
        use_wavefront: bool = True,
    ) -> Dict[str, Any]:
        req = PromoteAsphereRequest(
            surface_index=surface_index,
            max_order=max_order,
            target_effl=target_effl,
            min_edge_thickness=min_edge_thickness,
            use_wavefront=use_wavefront,
        )
        return optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: topology_service.promote_asphere(oss, req, snapshot_manager=sm)
            )
        )

    @mcp.tool(
        name="zemax_export_layout",
        title="Export Optical Layout Image",
        description=(
            "导出当前光学系统的 2D 光路截面图 (cross_section)、3D 视图 (viewer_3d) 或实体模型 (shaded_model) PNG 图像，"
            "并以多模态 ImageContent 直接返回给大模型查看光路与切光情况。"
        ),
        annotations=ToolAnnotations(readOnlyHint=True),
    )
    def zemax_export_layout(
        viewer_type: str = "cross_section",
        output_path: Optional[str] = None,
    ) -> Any:
        res = optical_service.safe_call(
            lambda: zm.run_locked(
                lambda oss: export_service.export_layout_image(
                    oss, viewer_type=viewer_type, output_path=output_path  # type: ignore[arg-type]
                )
            )
        )
        if res.get("status") != "ok":
            return res
        raw_bytes = base64.b64decode(res["data_base64"])
        meta = {
            "status": "ok",
            "viewer_type": res["viewer_type"],
            "image_path": res["image_path"],
            "size_bytes": res["size_bytes"],
        }
        return [Image(data=raw_bytes, format="png"), json.dumps(meta, ensure_ascii=False)]

    # ==================== MCP Resources ====================

    @mcp.resource(
        "zemax://system/snapshots",
        name="system_snapshots",
        title="System Snapshot Stack",
        description="当前可用的系统快照列表。",
        mime_type="application/json",
    )
    def resource_snapshots() -> str:
        return json.dumps({"snapshots": sm.list_snapshots()}, ensure_ascii=False, indent=2)

    @mcp.resource(
        "zemax://system/state",
        name="system_state",
        title="Current Optical System State",
        description="当前 Zemax 光学系统的实时表面参数与光路健康状态快照。",
        mime_type="application/json",
    )
    def resource_system_state() -> str:
        res = optical_service.safe_call(lambda: zm.run_locked(optical_service.get_status))
        return json.dumps(res, ensure_ascii=False, indent=2)

    @mcp.resource(
        "zemax://samples/list",
        name="samples_list",
        title="Available ZMX Samples",
        description="列出本机 Zemax 官方 Objectives 样例及本地 outputs/ 目录下的 .zmx 文件。",
        mime_type="application/json",
    )
    def resource_samples_list() -> str:
        candidates: List[str] = []
        search_dirs = [
            Path.home() / "Documents" / "Zemax" / "Samples" / "Sequential" / "Objectives",
            Path(os.getcwd()) / "outputs",
        ]
        for directory in search_dirs:
            if directory.exists():
                for zmx_file in sorted(directory.glob("*.zmx"))[:50]:
                    candidates.append(str(zmx_file))
        return json.dumps({"samples": candidates}, ensure_ascii=False, indent=2)

    # ==================== MCP Prompts ====================

    @mcp.prompt(
        name="design_singlet_workflow",
        title="Singlet Lens Design Workflow",
        description="单透镜标准设计闭环工作流：高斯初算 → 平缓弯曲构建 → EFFL优化 → Maréchal验收。",
    )
    def prompt_design_singlet(
        epd_mm: float = 25.0,
        target_effl_mm: float = 100.0,
        material: str = "N-BK7",
    ) -> str:
        return (
            f"请设计一个入瞳直径 {epd_mm} mm、目标焦距 {target_effl_mm} mm、材料为 {material} 的单透镜：\n"
            f"1. 使用薄透镜公式计算初始曲率半径，且满足 |R| > 1.5 * EPD = {1.5 * epd_mm} mm。\n"
            f"2. 调用 `zemax_build_system` 构建系统（平面 radius=0.0，semi_diameter 留空自动计算）。\n"
            f"3. 调用 `zemax_optimize(target_effl={target_effl_mm})` 进行点列图基底优化。\n"
            f"4. 调用 `zemax_evaluate` 验收：焦距误差 < 1%，若波前 RMS > 0.15 waves 再以 `use_wavefront=True, minimize_axial_color=True` 精修。\n"
            f"5. 达标后调用 `zemax_save_file` 保存结果。"
        )

    @mcp.prompt(
        name="design_afocal_telescope_workflow",
        title="Afocal Telescope Design Workflow",
        description="无焦望远系统（开普勒/伽利略）分步构建与拼接规范，严禁初始即全局盲目优化。",
    )
    def prompt_design_afocal() -> str:
        return (
            "无焦望远系统设计必须严格拆分：\n"
            "1. 先在有焦模式（afocal_mode=False）下独立构建并优化物镜，记录焦距 f'_obj。\n"
            "2. 再在有焦模式下独立构建并优化目镜，记录焦距 f'_eye。\n"
            "3. 计算焦点重合间隔 d = f'_obj + f'_eye，用 `zemax_build_system(afocal_mode=True)` 拼接。\n"
            "4. 最后通过 `zemax_optimize` 配合自定义操作数 PMAG（角放大率）或 EXPP（出瞳距）微调。"
        )

    @mcp.prompt(
        name="diagnose_tir_9e9",
        title="Diagnose 9E9 Ray Trace Failure",
        description="当遇到 9E9 光线追迹失败或全反射（TIR）时的标准恢复流程。",
    )
    def prompt_diagnose_tir() -> str:
        return (
            "遇到 Merit > 1e6 或 ray_trace_valid=False 时：\n"
            "1. 严禁原地重复调用 `zemax_optimize`。\n"
            "2. 调用 `zemax_get_status` 检查各面曲率半径与半口径。\n"
            "3. 使用 `zemax_patch_surface` 放大弯曲过度的表面 radius，或使用 `zemax_tune_parameter` 扫描安全区间，直到 `ray_trace_valid=True`。"
        )

    @mcp.prompt(
        name="design_asphere_workflow",
        title="Even Asphere Lens Design Workflow",
        description="偶次非球面（EvenAsphere + Conic）渐进式设计规范：防过弯、保边缘厚度、分阶释放高次项。",
    )
    def prompt_design_asphere(epd_mm: float = 35.0, target_effl_mm: float = 70.0) -> str:
        return (
            f"设计入瞳 {epd_mm} mm、焦距 {target_effl_mm} mm 的偶次非球面透镜建议优先调用 `zemax_promote_asphere`：\n"
            f"1. 先构建 Standard 球面基线，保持 |R| > 1.2 * EPD = {1.2 * epd_mm} mm，先锁定有效焦距。\n"
            f"2. 调用 `zemax_get_aberrations` 定位贡献球差最大的表面。\n"
            f"3. 调用 `zemax_promote_asphere(surface_index=..., max_order=6, target_effl={target_effl_mm}, min_edge_thickness=1.2)` 执行安全渐进升级。\n"
            f"4. 优化后调用 `zemax_export_layout` 查看面形与光线边缘是否平滑。"
        )

    @mcp.prompt(
        name="physics_driven_design_loop",
        title="Physics-Driven Optical Design Loop",
        description="物理大脑闭环工作流：P-W 综合/Sample 缩放 → DLS 优化 → 像差归因 → 非球面/分裂/Hammer 全局逃逸。",
    )
    def prompt_physics_loop() -> str:
        return (
            "严禁无脑随机加变量或原地重试优化，请按以下物理闭环执行：\n"
            "1. **初始结构**：1-3 片优先调用 `zemax_synthesize_initial_design`（singlet_min_spha / achromatic_doublet / cooke_triplet）；复杂系统从 `zemax://samples/list` 加载并用 `zemax_scale_system` 缩放到目标焦距。\n"
            "2. **局部优化**：调用 `zemax_optimize`（有焦必带 `target_effl`，玻璃元件建议带 `min_edge_thickness=1.2`）。\n"
            "3. **像差归因**：调用 `zemax_get_aberrations` 查看 `dominant_regime`、`attribution_hints` 与各面 `spha_share_pct` / `coma_share_pct`：\n"
            "   - 若为 `third_order` 且单面球差占比高 → 对该面调用 `zemax_promote_asphere` 或改弯曲；\n"
            "   - 若为 `high_order` 或单面光焦度过大 → 调用 `zemax_split_lens` 分裂透镜；\n"
            "   - 若停在局部极小值或色差难消 → 开启 `material_substitute=True` 并运行 `zemax_hammer_optimize(target_run_time_minutes=0.25)` 逃逸。\n"
            "4. **验收**：调用 `zemax_evaluate` 与 `zemax_export_layout` 完成指标与视觉双验收。"
        )

    return mcp


def main() -> None:
    parser = argparse.ArgumentParser(description="Zemax OpticStudio MCP Server")
    parser.add_argument(
        "--transport",
        choices=["streamable-http", "stdio"],
        default="streamable-http",
        help="MCP transport mode (default: streamable-http)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    if args.transport == "stdio":
        server = create_mcp_server()
        server.run(transport="stdio")
    else:
        import uvicorn
        from zemax_mcp.api.http_app import create_app

        app = create_app()
        uvicorn.run(app, host=args.host, port=args.port, workers=1, loop="asyncio")


if __name__ == "__main__":
    main()
