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


def _analyze_client_trend(
    results: List[Dict[str, Any]],
    best_candidate: Optional[Dict[str, Any]],
    min_val: float,
    max_val: float,
) -> Dict[str, Any]:
    """客户端降级模式下的灵敏度与极值趋势诊断"""
    valid_points = [r for r in results if r.get("ray_trace_valid") and r.get("metric_value") is not None]
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
            "recommended_range": [round(best_val - step_delta, 4), round(best_val + step_delta, 4)],
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
            "recommended_range": [round(min(rec_min, rec_max), 4), round(max(rec_min, rec_max), 4)],
        }
    elif best_idx == n - 1:
        rec_min = max_val - step_size
        rec_max = max_val + step_size * 2.0
        return {
            "direction": "monotonic_boundary_max",
            "summary": f"像质在终止边界 {best_val} 处最优 (指标: {best_metric})，呈现向外侧单调改善趋势。建议向该方向扩展扫描范围。",
            "recommended_range": [round(min(rec_min, rec_max), 4), round(max(rec_min, rec_max), 4)],
        }
    else:
        prev_val = results[best_idx - 1]["value"]
        next_val = results[best_idx + 1]["value"]
        return {
            "direction": "interior_minimum",
            "summary": f"在参数值 {best_val} 处发现明确的像质最优极值点 (指标: {best_metric})，两侧像质均退化。此区域具备良好的局部收敛特征。",
            "recommended_range": [round(min(prev_val, next_val), 4), round(max(prev_val, next_val), 4)],
        }


def client_side_tune(
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
    """
    客户端无侵入回退方案：完全仅依赖原有的 /status 和 /patch_surface 接口，
    即使服务端未重启或未添加 /tune 端点也能直接运行，且全程保证沙箱自动回滚。
    """
    resp_init = requests.get(f"{server_url}/status", timeout=10)
    init_json = resp_init.json()
    if init_json.get("status") != "ok":
        return {"status": "error", "message": "无法获取当前系统状态", "detail": init_json}

    surfaces = init_json.get("data", {}).get("surfaces", [])
    target_surf = None
    for s in surfaces:
        if s.get("index") == surface_index:
            target_surf = s
            break
    if not target_surf:
        return {"status": "error", "message": f"未找到表面序号 {surface_index}"}

    param_key = parameter.lower().strip()
    orig_val = target_surf.get(param_key)
    if orig_val is None:
        return {"status": "error", "message": f"表面不存在属性: {parameter}"}

    last_surf_idx = len(surfaces) - 2
    orig_last_thickness = None
    for s in surfaces:
        if s.get("index") == last_surf_idx:
            orig_last_thickness = s.get("thickness")
            break

    if steps <= 1:
        test_values = [min_value]
    else:
        step_size = (max_value - min_value) / (steps - 1)
        test_values = [min_value + i * step_size for i in range(steps)]

    results = []
    best_candidate = None
    best_metric = float("inf")
    best_last_thickness = None
    applied = False

    try:
        for val in test_values:
            patch_payload = {"surface_index": surface_index, param_key: float(val)}
            requests.post(f"{server_url}/patch_surface", json=patch_payload, timeout=10)

            refocused_bfl = None
            if quick_focus:
                try:
                    qf_resp = requests.post(f"{server_url}/quick_focus", timeout=10).json()
                    if qf_resp.get("status") == "ok":
                        qf_surfs = qf_resp.get("data", {}).get("surfaces", [])
                        for s in qf_surfs:
                            if s.get("index") == last_surf_idx:
                                refocused_bfl = s.get("thickness")
                                break
                except Exception:
                    pass

            st_resp = requests.get(f"{server_url}/status", timeout=10).json()
            st_data = st_resp.get("data", {})
            ray_ok = bool(st_data.get("ray_trace_valid", False))

            metric_val = None
            if ray_ok:
                crit = criterion.lower().strip()
                if crit in ("geo_spot", "geo_spot_diameter_um"):
                    metric_val = st_data.get("geo_spot_diameter_um")
                elif crit in ("rms_spot", "rms_spot_radius"):
                    rms_mat = st_data.get("rms_spot_radius")
                    if rms_mat and len(rms_mat) > 0 and len(rms_mat[0]) > 0:
                        metric_val = round(rms_mat[0][0] * 1000.0, 4)
                elif crit in ("merit_function", "merit", "mf"):
                    mf = st_data.get("merit_function")
                    if mf is not None and mf < 1e6:
                        metric_val = round(mf, 6)

                if metric_val is None:
                    metric_val = st_data.get("geo_spot_diameter_um") or st_data.get("merit_function")

            item = {
                "value": round(float(val), 5),
                "ray_trace_valid": ray_ok,
                "metric_value": metric_val,
                "geo_spot_diameter_um": st_data.get("geo_spot_diameter_um"),
                "merit_function": st_data.get("merit_function"),
            }
            if refocused_bfl is not None:
                item["refocused_bfl_mm"] = refocused_bfl

            results.append(item)

            if ray_ok and metric_val is not None and metric_val < best_metric:
                best_metric = metric_val
                best_candidate = item
                best_last_thickness = refocused_bfl

        if apply_best and best_candidate is not None:
            best_val = best_candidate["value"]
            requests.post(f"{server_url}/patch_surface", json={"surface_index": surface_index, param_key: float(best_val)}, timeout=10)
            if quick_focus and best_last_thickness is not None and last_surf_idx >= 1:
                requests.post(f"{server_url}/patch_surface", json={"surface_index": last_surf_idx, "thickness": float(best_last_thickness)}, timeout=10)
            applied = True
        else:
            requests.post(f"{server_url}/patch_surface", json={"surface_index": surface_index, param_key: float(orig_val)}, timeout=10)
            if quick_focus and orig_last_thickness is not None and last_surf_idx >= 1:
                requests.post(f"{server_url}/patch_surface", json={"surface_index": last_surf_idx, "thickness": float(orig_last_thickness)}, timeout=10)
            applied = False

    except Exception as e:
        if not applied:
            try:
                requests.post(f"{server_url}/patch_surface", json={"surface_index": surface_index, param_key: float(orig_val)}, timeout=5)
                if quick_focus and orig_last_thickness is not None and last_surf_idx >= 1:
                    requests.post(f"{server_url}/patch_surface", json={"surface_index": last_surf_idx, "thickness": float(orig_last_thickness)}, timeout=5)
            except Exception:
                pass
        return {"status": "error", "message": f"客户端无侵入调参执行异常: {str(e)}"}

    trend_info = _analyze_client_trend(results, best_candidate, min_value, max_value)
    valid_candidates = [r for r in results if r["ray_trace_valid"] and r["metric_value"] is not None]
    sorted_candidates = sorted(valid_candidates, key=lambda x: x["metric_value"])
    top_candidates = [
        {
            "rank": idx + 1,
            "value": cand["value"],
            "metric_value": cand["metric_value"],
            "geo_spot_diameter_um": cand.get("geo_spot_diameter_um"),
        }
        for idx, cand in enumerate(sorted_candidates[:3])
    ]

    best_cand_out = None
    if best_candidate is not None:
        best_cand_out = dict(best_candidate)
        best_cand_out["applied"] = applied

    return {
        "status": "ok",
        "surface_index": surface_index,
        "parameter": parameter,
        "original_value": orig_val,
        "scan_range": {"min": min_value, "max": max_value, "steps": steps},
        "criterion": criterion,
        "quick_focus": quick_focus,
        "applied": applied,
        "valid_count": len(valid_candidates),
        "invalid_count": len(results) - len(valid_candidates),
        "best_candidate": best_cand_out,
        "trend_analysis": trend_info,
        "top_candidates": top_candidates,
        "all_results": results,
        "execution_mode": "client_side_fallback"
    }


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
    """优先调用服务端的 /tune 端点；若服务端未升级或返回 404，则自动无缝降级为无侵入客户端扫描"""
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

    try:
        resp = requests.post(f"{server_url}/tune", json=payload, timeout=60)
        if resp.status_code == 200:
            return resp.json()
        elif resp.status_code == 404:
            # 服务端未更新该端点，自动无缝切换为客户端降级调参
            return client_side_tune(
                surface_index=surface_index,
                parameter=parameter,
                min_value=min_value,
                max_value=max_value,
                steps=steps,
                criterion=criterion,
                target_effl=target_effl,
                quick_focus=quick_focus,
                apply_best=apply_best,
                server_url=server_url,
            )
        else:
            return resp.json()
    except requests.exceptions.RequestException:
        raise


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
