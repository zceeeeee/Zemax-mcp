# zemax_cli.py
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
    p_patch.add_argument("--radius", type=float, help="曲率半径(0为无穷大)")
    p_patch.add_argument("--thickness", type=float, help="厚度")
    p_patch.add_argument("--material", type=str, help="玻璃材料")
    p_patch.add_argument("--var-r", type=int, choices=[0, 1], help="曲率半径是否设为变量(1是,0否)")
    p_patch.add_argument("--var-t", type=int, choices=[0, 1], help="厚度是否设为变量(1是,0否)")
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
    p_opt.add_argument("--achromatic", action="store_true", help="加入 AXCL 强制消除轴向色差")
    p_opt.add_argument("--custom-op", type=str, help="额外自定义操作数 JSON 数组字符串")

    # 6. evaluate
    p_eval = subparsers.add_parser("evaluate", help="读取波前 RMS (λ)、斯特列尔比与 MTF 曲线数据")
    p_eval.add_argument("--max-freq", type=float, default=50.0, help="最大空间频率 (默认 50 lp/mm)")
    p_eval.add_argument("--points", type=int, default=5, help="频率采样点数 (默认 5 个点)")
    p_eval.add_argument("--mtf-sampling", type=int, choices=[1, 2, 3, 4], default=3, help="MTF 采样级别 (默认 3，Grid=0 稀疏积分；逐级比较以检查收敛)")

    # 7. save
    p_save = subparsers.add_parser("save", help="保存镜头文件")
    p_save.add_argument("filepath", help="保存的 .zmx 路径")

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
            if args.radius is not None: payload["radius"] = args.radius
            if args.thickness is not None: payload["thickness"] = args.thickness
            if args.material is not None: payload["material"] = args.material
            if args.var_r is not None: payload["radius_variable"] = bool(args.var_r)
            if args.var_t is not None: payload["thickness_variable"] = bool(args.var_t)
            if args.semi_diameter is not None: payload["semi_diameter"] = args.semi_diameter
            if args.semi_diameter_auto: payload["semi_diameter_auto"] = True
            if args.stop: payload["is_stop"] = True
            resp = requests.post(f"{SERVER_URL}/patch_surface", json=payload)
        elif args.command == "optimize":
            payload = {
                "cycles": args.cycles,
                "use_wavefront": args.wavefront,
                "effl_weight": args.effl_weight,
                "minimize_axial_color": args.achromatic
            }
            if args.effl is not None: payload["target_effl"] = args.effl
            if args.max_totr is not None: payload["max_totr"] = args.max_totr
            if args.min_bfl is not None: payload["min_bfl"] = args.min_bfl
            if args.custom_op: payload["custom_operands"] = json.loads(args.custom_op)
            resp = requests.post(f"{SERVER_URL}/optimize", json=payload)
        elif args.command == "evaluate":
            resp = requests.post(f"{SERVER_URL}/evaluate", json={
                "max_frequency": args.max_freq, "num_freq_points": args.points,
                "mtf_sampling": args.mtf_sampling
            })
        elif args.command == "save":
            resp = requests.post(f"{SERVER_URL}/save", json={"filepath": args.filepath})

        print(json.dumps(resp.json(), indent=2, ensure_ascii=False))
    except requests.exceptions.ConnectionError:
        print(json.dumps({
            "status": "fatal",
            "message": "无法连接到 zemax_server.py！请确认已在独立终端启动后台守护进程。"
        }, ensure_ascii=False))

if __name__ == "__main__":
    main()
