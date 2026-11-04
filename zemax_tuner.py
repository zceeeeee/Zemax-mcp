# zemax_tuner.py
"""
Zemax 参数自动快速调参器 (Parameter Tuner for AI Agent)

功能：
1. 允许 Agent/用户针对当前系统中的指定表面参数 (radius, thickness, semi_diameter) 在指定区间内快速自动调参。
2. 避免大模型单步手动反复试错，将多轮对话压缩为单次高效扫描。
3. 支持多种优化择优目标 (geo_spot, rms_spot, merit_function, effl_diff)。
4. 支持快速对焦 (quick_focus) 消除离焦干扰。
5. 支持两阶段自适应粗调到精调 (two-stage / auto-zoom)。
6. 支持终端 ASCII 可视化曲线、格式化表格输出以及机器读取的纯 JSON 模式。
"""

import sys
import json
import argparse
import requests
from typing import Dict, Any, Optional, List

SERVER_URL = "http://127.0.0.1:8765"


def call_tune_api(
    surface_index: int,
    parameter: str,
    min_value: float,
    max_value: float,
    steps: int = 10,
    criterion: str = "geo_spot",
    target_effl: Optional[float] = None,
    quick_focus: bool = False,
    apply_best: bool = False,
    server_url: str = SERVER_URL,
) -> Dict[str, Any]:
    """调用服务端的 /tune 端点执行一次单阶段参数扫描"""
    payload = {
        "surface_index": surface_index,
        "parameter": parameter,
        "min_value": min_value,
        "max_value": max_value,
        "steps": steps,
        "criterion": criterion,
        "quick_focus": quick_focus,
        "apply_best": apply_best,
    }
    if target_effl is not None:
        payload["target_effl"] = target_effl

    resp = requests.post(f"{server_url}/tune", json=payload, timeout=60)
    return resp.json()


def two_stage_tune(
    surface_index: int,
    parameter: str,
    min_value: float,
    max_value: float,
    stage1_steps: int = 7,
    stage2_steps: int = 7,
    criterion: str = "geo_spot",
    target_effl: Optional[float] = None,
    quick_focus: bool = False,
    apply_best: bool = True,
    server_url: str = SERVER_URL,
) -> Dict[str, Any]:
    """
    两阶段粗调 + 局部精调 (Coarse-to-Fine Tuning)
    1. 第一阶段粗扫：在 [min_value, max_value] 找出最优凹陷点或边界趋势。
    2. 第二阶段精扫：自动根据推荐区间做更精细采样，并应用最终最优值。
    """
    # 阶段 1：粗调 (暂不应用，保留原状态或探索最优)
    res_s1 = call_tune_api(
        surface_index=surface_index,
        parameter=parameter,
        min_value=min_value,
        max_value=max_value,
        steps=stage1_steps,
        criterion=criterion,
        target_effl=target_effl,
        quick_focus=quick_focus,
        apply_best=False,
        server_url=server_url,
    )

    if res_s1.get("status") != "ok":
        return {"status": "error", "stage": 1, "detail": res_s1}

    trend = res_s1.get("trend_analysis", {})
    rec_range = trend.get("recommended_range")

    # 如果无法形成有效推荐范围（如全部追迹失败）
    if not rec_range or len(rec_range) != 2:
        return {
            "status": "warning",
            "message": "第一阶段扫描未产生有效的精修推荐区间",
            "stage1": res_s1,
            "final_best": res_s1.get("best_candidate"),
        }

    s2_min, s2_max = rec_range[0], rec_range[1]

    # 阶段 2：精调 (按用户需求是否最终应用)
    res_s2 = call_tune_api(
        surface_index=surface_index,
        parameter=parameter,
        min_value=s2_min,
        max_value=s2_max,
        steps=stage2_steps,
        criterion=criterion,
        target_effl=target_effl,
        quick_focus=quick_focus,
        apply_best=apply_best,
        server_url=server_url,
    )

    return {
        "status": "ok",
        "tuning_mode": "two_stage_zoom",
        "surface_index": surface_index,
        "parameter": parameter,
        "stage1_coarse": {
            "range": [min_value, max_value],
            "best": res_s1.get("best_candidate"),
            "trend": trend.get("summary"),
        },
        "stage2_fine": {
            "range": [s2_min, s2_max],
            "best": res_s2.get("best_candidate"),
            "trend": res_s2.get("trend_analysis", {}).get("summary"),
        },
        "final_best": res_s2.get("best_candidate") or res_s1.get("best_candidate"),
        "stage2_full_result": res_s2,
    }


def render_ascii_report(data: Dict[str, Any]) -> str:
    """生成给 Agent 与工程师直观阅读的 ASCII 报表与性能柱状分布"""
    lines = []
    lines.append("=" * 70)
    lines.append(f"  Zemax 快速调参反馈报告 (Surface {data.get('surface_index')}, {data.get('parameter')})")
    lines.append("=" * 70)

    orig_val = data.get("original_value")
    applied = data.get("applied")
    criterion = data.get("criterion")
    scan_range = data.get("scan_range", {})
    best = data.get("best_candidate")
    trend = data.get("trend_analysis", {})

    lines.append(f"• 原始参数值: {orig_val}")
    lines.append(f"• 扫描区间:   [{scan_range.get('min')} ~ {scan_range.get('max')}] 共 {scan_range.get('steps')} 个采样点")
    lines.append(f"• 寻优指标:   {criterion}")
    lines.append(f"• 追迹有效率: {data.get('valid_count')} / {data.get('valid_count', 0) + data.get('invalid_count', 0)}")

    all_res = data.get("all_results", [])
    valid_metrics = [r["metric_value"] for r in all_res if r.get("metric_value") is not None]

    if valid_metrics:
        min_m, max_m = min(valid_metrics), max(valid_metrics)
        m_range = max_m - min_m if max_m > min_m else 1.0

        lines.append("\n[采样点明细与指标趋势分布]")
        lines.append(f"{'No.':<4} {'测试值':<10} {'追迹':<6} {'指标值':<12} {'焦距(mm)':<10} {'趋势分布 (柱短者优)'}")
        lines.append("-" * 70)

        for idx, r in enumerate(all_res, 1):
            val_str = f"{r['value']:.4f}"
            valid_str = "OK" if r.get("ray_trace_valid") else "FAIL"
            m_val = r.get("metric_value")
            effl_str = f"{r['effl_mm']:.2f}" if r.get("effl_mm") is not None else "-"

            if m_val is not None:
                m_str = f"{m_val:.4f}"
                # 归一化柱状图长度 (1~20 字符)
                bar_len = int(round(((m_val - min_m) / m_range) * 16)) + 2
                bar = "█" * bar_len
                is_best = " ★ BEST" if best and abs(r["value"] - best["value"]) < 1e-5 else ""
                lines.append(f"{idx:<4} {val_str:<10} {valid_str:<6} {m_str:<12} {effl_str:<10} {bar}{is_best}")
            else:
                lines.append(f"{idx:<4} {val_str:<10} {valid_str:<6} {'TIR/切光':<12} {effl_str:<10} (全反射/无法成像)")

    if best:
        lines.append("\n[最优候选解]")
        lines.append(f"  • 最优值:     {best.get('value')}")
        lines.append(f"  • 指标值:     {best.get('metric_value')}")
        if best.get("geo_spot_diameter_um") is not None:
            lines.append(f"  • 几何弥散斑: {best.get('geo_spot_diameter_um')} μm")
        if best.get("effl_mm") is not None:
            lines.append(f"  • 对应焦距:   {best.get('effl_mm')} mm")
        lines.append(f"  • 是否已应用: {'已应用至当前系统' if applied else '未应用 (已安全回滚至初始状态)'}")

    if trend:
        lines.append("\n[灵敏度趋势诊断与建议]")
        lines.append(f"  • 诊断结论: {trend.get('summary')}")
        if trend.get("recommended_range"):
            lines.append(f"  • 下一步推荐微调区间: {trend.get('recommended_range')}")

    lines.append("=" * 70)
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Zemax AI Agent 自动快速调参器")
    parser.add_argument("--index", type=int, required=True, help="要调节的表面序号 (从1开始)")
    parser.add_argument("--param", type=str, required=True, choices=["radius", "thickness", "semi_diameter"], help="调参变量名称")
    parser.add_argument("--min", type=float, required=True, dest="min_val", help="扫描起始/最小值")
    parser.add_argument("--max", type=float, required=True, dest="max_val", help="扫描终止/最大值")
    parser.add_argument("--steps", type=int, default=10, help="采样点数 (默认 10)")
    parser.add_argument("--criterion", type=str, default="geo_spot", choices=["geo_spot", "rms_spot", "merit_function", "effl_diff"], help="寻优目标 (默认 geo_spot)")
    parser.add_argument("--target-effl", type=float, default=None, help="目标焦距 (当 criterion=effl_diff 时必填)")
    parser.add_argument("--quick-focus", action="store_true", help="每个采样点后自动执行快速对焦消除离焦")
    parser.add_argument("--apply-best", action="store_true", help="自动将最优参数应用到当前系统")
    parser.add_argument("--two-stage", action="store_true", help="开启两阶段自适应精修 (粗扫定位极小值后自动放大精扫)")
    parser.add_argument("--json", action="store_true", help="纯 JSON 输出模式 (便于 Agent 代码解析)")
    parser.add_argument("--server", type=str, default=SERVER_URL, help="Zemax Server 地址")

    args = parser.parse_args()

    try:
        if args.two_stage:
            res = two_stage_tune(
                surface_index=args.index,
                parameter=args.param,
                min_value=args.min_val,
                max_value=args.max_val,
                stage1_steps=args.steps,
                stage2_steps=args.steps,
                criterion=args.criterion,
                target_effl=args.target_effl,
                quick_focus=args.quick_focus,
                apply_best=args.apply_best,
                server_url=args.server,
            )
            if args.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                s2_data = res.get("stage2_full_result", {})
                if s2_data.get("status") == "ok":
                    print(render_ascii_report(s2_data))
                else:
                    print(json.dumps(res, indent=2, ensure_ascii=False))
        else:
            res = call_tune_api(
                surface_index=args.index,
                parameter=args.param,
                min_value=args.min_val,
                max_value=args.max_val,
                steps=args.steps,
                criterion=args.criterion,
                target_effl=args.target_effl,
                quick_focus=args.quick_focus,
                apply_best=args.apply_best,
                server_url=args.server,
            )
            if args.json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                if res.get("status") == "ok":
                    print(render_ascii_report(res))
                else:
                    print(json.dumps(res, indent=2, ensure_ascii=False))

    except requests.exceptions.ConnectionError:
        err = {"status": "fatal", "message": f"无法连接到 Zemax 服务端 ({args.server})，请确认后台 zemax_server.py 正在运行。"}
        if args.json:
            print(json.dumps(err, ensure_ascii=False))
        else:
            print(f"❌ 错误: {err['message']}")
        sys.exit(1)


if __name__ == "__main__":
    main()
