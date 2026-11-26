# zemax_cli.py
import sys
import json
import argparse
import requests

SERVER_URL = "http://127.0.0.1:8765"

def main():
    parser = argparse.ArgumentParser(description="Zemax AI Agent 命令行控制器")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # 1. status
    subparsers.add_parser("status", help="获取当前镜头表面参数与光路健康状态")

    # 2. load
    p_load = subparsers.add_parser("load", help="加载现有的 .zmx 镜头文件 (如官方 Sample)")
    p_load.add_argument("filepath", help="要加载的 .zmx 文件路径")

    # 3. build
    p_build = subparsers.add_parser("build", help="根据 JSON 配置重建光学系统")
    p_build.add_argument("json_input", help="JSON 文件路径或 JSON 字符串")

    # 4. patch
    p_patch = subparsers.add_parser("patch", help="修改单个表面参数")
    p_patch.add_argument("--index", type=int, required=True, help="表面序号(从1开始)")
    p_patch.add_argument("--surface-type", type=str, choices=["Standard", "EvenAsphere"], help="切换表面类型")
    p_patch.add_argument("--radius", type=float, help="曲率半径(0为无穷大)")
    p_patch.add_argument("--thickness", type=float, help="厚度")
    p_patch.add_argument("--material", type=str, help="玻璃材料")
    p_patch.add_argument("--conic", type=float, help="圆锥常数 (Conic k)")
    p_patch.add_argument("--var-r", type=int, choices=[0, 1], help="曲率半径是否设为变量(1是,0否)")
    p_patch.add_argument("--var-t", type=int, choices=[0, 1], help="厚度是否设为变量(1是,0否)")
    p_patch.add_argument("--var-c", type=int, choices=[0, 1], help="圆锥常数是否设为变量(1是,0否)")
    p_patch.add_argument("--material-sub", type=int, choices=[0, 1], help="材料是否设为 Hammer 替换变量(1是,0否)")
    p_patch.add_argument("--catalog", type=str, default="SCHOTT", help="玻璃替换库名 (默认 SCHOTT)")
    p_patch.add_argument("--asphere-terms", type=str, help="偶次非球面高次项 JSON，如 '{\"4\": -1e-6, \"6\": 2e-9}'")
    p_patch.add_argument("--asphere-vars", type=str, help="偶次非球面变量 JSON，如 '{\"4\": true, \"6\": true}'")
    p_patch.add_argument("--marginal-ray-height", action="store_true", help="将该面厚度设置为边缘光线高度求解 (MarginalRayHeight, Height=0.0, PupilZone=0.0)")
    p_aperture = p_patch.add_mutually_exclusive_group()
    p_aperture.add_argument("--semi-diameter", type=float, help="固定半口径(mm)，必须为正数，通常优先使用自动半口径")
    p_aperture.add_argument("--semi-diameter-auto", action="store_true", help="恢复半口径 Automatic 求解")
    p_patch.add_argument("--stop", action="store_true", help="将光阑移动到该面")

    # 5. optimize
    p_opt = subparsers.add_parser("optimize", help="带宏观物理约束的自动评价函数构建与 DLS 优化")
    p_opt.add_argument("--cycles", type=int, default=0, help="0为自动循环，其他为固定10次")
    p_opt.add_argument("--wavefront", action="store_true", help="使用波前差(Wavefront)替代点列图作为优化基底")
    p_opt.add_argument("--effl", type=float, help="严格锁定的目标有效焦距 EFFL (mm)")
    p_opt.add_argument("--effl-weight", type=float, default=10.0, help="焦距锁定权重 (默认 10.0)")
    p_opt.add_argument("--max-totr", type=float, help="限制系统最大总长 TOTR <= 该值 (mm)")
    p_opt.add_argument("--min-bfl", type=float, help="限制最小后工作距/后截距 >= 该值 (mm)")
    p_opt.add_argument("--min-edge", type=float, help="限制玻璃最小边缘厚度 MNEG >= 该值 (mm)")
    p_opt.add_argument("--achromatic", action="store_true", help="加入 AXCL 强制消除轴向色差")
    p_opt.add_argument("--trac-mf", action="store_true", help="使用讲义专用的 6 孔径带 TRAC + MNCA + MXCA + EFFL 评价函数")
    p_opt.add_argument("--custom-op", type=str, help="额外自定义操作数 JSON 数组字符串")

    # 6. evaluate
    p_eval = subparsers.add_parser("evaluate", help="读取波前 RMS (λ)、斯特列尔比与 MTF 曲线数据")
    p_eval.add_argument("--max-freq", type=float, default=50.0, help="最大空间频率 (默认 50 lp/mm)")
    p_eval.add_argument("--points", type=int, default=5, help="频率采样点数 (默认 5 个点)")
    p_eval.add_argument("--mtf-sampling", type=int, choices=[1, 2, 3, 4], default=3, help="MTF 采样级别 (默认 3，Grid=0 稀疏积分；逐级比较以检查收敛)")

    # 7. save
    p_save = subparsers.add_parser("save", help="保存镜头文件")
    p_save.add_argument("filepath", help="保存的 .zmx 路径")

    # 8. quick_focus
    subparsers.add_parser("quick_focus", help="调用 QuickFocus 工具寻找最佳像面位置")

    # 8b. aberrations
    subparsers.add_parser("aberrations", help="读取初级赛德尔像差系数 (SPHA/COMA/ASTI/FCUR/DIST) 与轴向色差")

    # 9. tune
    p_tune = subparsers.add_parser("tune", help="在指定范围内对某一表面参数进行快速自动扫描调参并反馈")
    p_tune.add_argument("--index", type=int, required=True, help="表面序号 (从1开始)")
    p_tune.add_argument("--param", type=str, required=True, choices=["radius", "thickness", "semi_diameter", "conic"], help="调参变量类型")
    p_tune.add_argument("--min", type=float, required=True, dest="min_val", help="扫描起始/最小值")
    p_tune.add_argument("--max", type=float, required=True, dest="max_val", help="扫描终止/最大值")
    p_tune.add_argument("--steps", type=int, default=10, help="采样点数 (默认 10，2~100)")
    p_tune.add_argument("--criterion", type=str, default="geo_spot", choices=["geo_spot", "rms_spot", "merit_function", "effl_diff"], help="寻优目标 (默认 geo_spot)")
    p_tune.add_argument("--target-effl", type=float, default=None, help="若 criterion 为 effl_diff 时的目标焦距 (mm)")
    p_tune.add_argument("--quick-focus", action="store_true", help="每个采样点后自动执行快速对焦")
    p_tune.add_argument("--apply-best", action="store_true", help="自动将最优参数应用到当前系统中")

    # 10. hammer
    p_hammer = subparsers.add_parser("hammer", help="运行限时 Hammer 全局优化逃逸局部极小值")
    p_hammer.add_argument("--time", type=float, default=0.25, help="运行时间(分钟，上限3.0)")
    p_hammer.add_argument("--effl", type=float, help="目标有效焦距 EFFL (mm)")
    p_hammer.add_argument("--min-edge", type=float, help="最小玻璃边缘厚度 MNEG (mm)")
    p_hammer.add_argument("--wavefront", action="store_true", help="波前差基底")
    p_hammer.add_argument("--achromatic", action="store_true", help="加入 AXCL 消除轴向色差")

    # 11. synthesize
    p_synth = subparsers.add_parser("synthesize", help="基于 P-W/初阶理论综合初始光学结构")
    p_synth.add_argument("--topology", required=True, choices=["singlet_min_spha", "achromatic_doublet", "cooke_triplet"])
    p_synth.add_argument("--effl", type=float, required=True, help="目标焦距 (mm)")
    p_synth.add_argument("--epd", type=float, required=True, help="入瞳直径 (mm)")
    p_synth.add_argument("--fovs", type=float, nargs="+", default=[0.0], help="Y方向视场角列表")
    p_synth.add_argument("--glasses", type=str, nargs="+", default=None, help="指定玻璃牌号列表")
    p_synth.add_argument("--no-apply", action="store_true", help="仅生成配置不立即构建")

    # 12. scale
    p_scale = subparsers.add_parser("scale", help="按倍率或目标焦距整体缩放当前光学系统")
    p_scale.add_argument("--factor", type=float, help="直接缩放倍率")
    p_scale.add_argument("--effl", type=float, help="目标有效焦距 (mm)")

    # 13. split
    p_split = subparsers.add_parser("split", help="安全透镜分裂（保持总光焦度拆分为两片同材料透镜）")
    p_split.add_argument("--index", type=int, required=True, help="透镜前表面序号")
    p_split.add_argument("--ratio", type=float, default=0.5, help="光焦度分配比 (0.1~0.9)")
    p_split.add_argument("--air-gap", type=float, default=1.0, help="分裂后空气间隔 (mm)")

    # 14. promote-asphere
    p_promo = subparsers.add_parser("promote-asphere", help="渐进式非球面升级（Conic -> 高次项 + MNEG）")
    p_promo.add_argument("--index", type=int, required=True, help="目标表面序号")
    p_promo.add_argument("--max-order", type=int, default=6, choices=[4, 6, 8], help="最高阶次")
    p_promo.add_argument("--effl", type=float, help="目标有效焦距 (mm)")
    p_promo.add_argument("--min-edge", type=float, default=1.2, help="最小边缘厚度 (mm)")

    args = parser.parse_args()

    try:
        if args.command == "status":
            resp = requests.get(f"{SERVER_URL}/status")
        elif args.command == "load":
            resp = requests.post(f"{SERVER_URL}/load_file", json={"filepath": args.filepath})
        elif args.command == "build":
            if args.json_input.endswith(".json"):
                with open(args.json_input, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            else:
                payload = json.loads(args.json_input)
            resp = requests.post(f"{SERVER_URL}/build", json=payload)
        elif args.command == "patch":
            payload = {"surface_index": args.index}
            if args.surface_type is not None: payload["surface_type"] = args.surface_type
            if args.radius is not None: payload["radius"] = args.radius
            if args.thickness is not None: payload["thickness"] = args.thickness
            if args.material is not None: payload["material"] = args.material
            if args.conic is not None: payload["conic"] = args.conic
            if args.var_r is not None: payload["radius_variable"] = bool(args.var_r)
            if args.var_t is not None: payload["thickness_variable"] = bool(args.var_t)
            if args.var_c is not None: payload["conic_variable"] = bool(args.var_c)
            if args.material_sub is not None:
                payload["material_substitute"] = bool(args.material_sub)
                payload["substitute_catalog"] = args.catalog
            if args.asphere_terms: payload["asphere_terms"] = {int(k): float(v) for k, v in json.loads(args.asphere_terms).items()}
            if args.asphere_vars: payload["asphere_terms_variable"] = {int(k): bool(v) for k, v in json.loads(args.asphere_vars).items()}
            if args.marginal_ray_height: payload["marginal_ray_height_solve"] = True
            if args.semi_diameter is not None: payload["semi_diameter"] = args.semi_diameter
            if args.semi_diameter_auto: payload["semi_diameter_auto"] = True
            if args.stop: payload["is_stop"] = True
            resp = requests.post(f"{SERVER_URL}/patch_surface", json=payload)
        elif args.command == "optimize":
            payload = {
                "cycles": args.cycles,
                "use_wavefront": args.wavefront,
                "effl_weight": args.effl_weight,
                "minimize_axial_color": args.achromatic,
                "use_trac_spherical_mf": args.trac_mf
            }
            if args.effl is not None: payload["target_effl"] = args.effl
            if args.max_totr is not None: payload["max_totr"] = args.max_totr
            if args.min_bfl is not None: payload["min_bfl"] = args.min_bfl
            if args.min_edge is not None: payload["min_edge_thickness"] = args.min_edge
            if args.custom_op: payload["custom_operands"] = json.loads(args.custom_op)
            resp = requests.post(f"{SERVER_URL}/optimize", json=payload)
        elif args.command == "evaluate":
            resp = requests.post(f"{SERVER_URL}/evaluate", json={
                "max_frequency": args.max_freq, "num_freq_points": args.points,
                "mtf_sampling": args.mtf_sampling
            })
        elif args.command == "save":
            resp = requests.post(f"{SERVER_URL}/save", json={"filepath": args.filepath})
        elif args.command == "quick_focus":
            resp = requests.post(f"{SERVER_URL}/quick_focus")
        elif args.command == "aberrations":
            resp = requests.get(f"{SERVER_URL}/aberrations")
        elif args.command == "tune":
            payload = {
                "surface_index": args.index,
                "parameter": args.param,
                "min_value": args.min_val,
                "max_value": args.max_val,
                "steps": args.steps,
                "criterion": args.criterion,
                "quick_focus": args.quick_focus,
                "apply_best": args.apply_best
            }
            if args.target_effl is not None:
                payload["target_effl"] = args.target_effl
            resp = requests.post(f"{SERVER_URL}/tune", json=payload)
        elif args.command == "hammer":
            payload = {
                "target_run_time_minutes": args.time,
                "use_wavefront": args.wavefront,
                "minimize_axial_color": args.achromatic,
            }
            if args.effl is not None: payload["target_effl"] = args.effl
            if args.min_edge is not None: payload["min_edge_thickness"] = args.min_edge
            resp = requests.post(f"{SERVER_URL}/hammer_optimize", json=payload)
        elif args.command == "synthesize":
            payload = {
                "topology": args.topology,
                "target_effl": args.effl,
                "epd": args.epd,
                "fovs": args.fovs,
                "apply_to_system": not args.no_apply,
            }
            if args.glasses is not None: payload["glasses"] = args.glasses
            resp = requests.post(f"{SERVER_URL}/synthesize", json=payload)
        elif args.command == "scale":
            payload = {}
            if args.factor is not None: payload["scale_factor"] = args.factor
            if args.effl is not None: payload["target_effl"] = args.effl
            resp = requests.post(f"{SERVER_URL}/scale", json=payload)
        elif args.command == "split":
            resp = requests.post(
                f"{SERVER_URL}/split_lens",
                json={"surface_index": args.index, "split_ratio": args.ratio, "air_gap": args.air_gap},
            )
        elif args.command == "promote-asphere":
            payload = {
                "surface_index": args.index,
                "max_order": args.max_order,
                "min_edge_thickness": args.min_edge,
            }
            if args.effl is not None: payload["target_effl"] = args.effl
            resp = requests.post(f"{SERVER_URL}/promote_asphere", json=payload)

        data = resp.json()
        print(json.dumps(data, indent=2, ensure_ascii=False))
        if (isinstance(resp.status_code, int) and resp.status_code >= 400) or (
            isinstance(data, dict) and data.get("status") not in (None, "ok")
        ):
            sys.exit(1)
    except requests.exceptions.ConnectionError:
        print(json.dumps({
            "status": "fatal",
            "message": "无法连接到 zemax_server.py！请确认已在独立终端启动后台守护进程。"
        }, ensure_ascii=False))
        sys.exit(1)

if __name__ == "__main__":
    main()
