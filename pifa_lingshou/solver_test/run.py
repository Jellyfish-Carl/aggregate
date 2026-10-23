"""运行单个 pifa_lingshou request，并保存 response 与节点可视化。"""
from datetime import datetime
from copy import deepcopy
import json
from pathlib import Path
import sys
from time import perf_counter
from zoneinfo import ZoneInfo
from html import escape
import argparse
from contextlib import redirect_stdout, redirect_stderr

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pifa_lingshou.service.request_runner import solve_request
from pifa_lingshou.utils.resource_store import RESOURCE


# 粘贴 request_generation.py 输出的完整地址；单次求解为默认模式。
REQUEST_FILE = "/Users/hanwu/PyCharmMiscProject/aggregate/pifa_lingshou/resource/input/solver_test/request_13/request_13_20261023140039001760_day_ahead_declaration.json"
# 留空时读取 request 自带的上游 response 路径；仅在需要覆盖时填写。
UPSTREAM_RESPONSE_FILE = ""


def _timestamp():
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("run_%Y%m%d%H%M%S%f")


def _log(message):
    timestamp = datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M:%S,%f")[:-3]
    print(f"{timestamp} INFO solver_test - {message}", flush=True)


def _format_request_metrics(node, customer_count, time_count, scenario_count,
                            variable_count, constraint_count, solve_seconds,
                            other_seconds, total_seconds, gap, time_limit):
    headers = ("阶段", "客户数", "时间数量", "场景数", "变量数", "约束数", "求解时间/s",
               "其他处理时间/s", "总耗时/s", "求解精度 Gap", "求解时间限制/s")
    values = (
        node, customer_count, time_count, "N/A" if scenario_count is None else scenario_count,
        "N/A" if variable_count is None else variable_count,
        "N/A" if constraint_count is None else constraint_count,
        f"{solve_seconds:.3f}", f"{other_seconds:.3f}", f"{total_seconds:.3f}",
        "N/A" if gap is None else f"{float(gap):.4%}",
        "N/A" if time_limit is None else f"{float(time_limit):.1f}",
    )
    print("| " + " | ".join(headers) + " |", flush=True)
    print("| " + " | ".join(":---:" for _ in headers) + " |", flush=True)
    print("| " + " | ".join(map(str, values)) + " |", flush=True)


def _customer_storage_rows(raw):
    return [(cid,b['settings']['capacity_mwh'],b['settings']['maximum_charge_mw'],
        sum(b['charge_mwh']),sum(b['discharge_mwh']),b['soc_mwh'][-1],
        b['switching_ledger']['startup_count'],b['switching_ledger']['shutdown_count'],
        b['switching_ledger']['switching_cost_yuan']) for cid,b in raw.get('customer_storage',{}).items()]


def _render_solver_test_report(response):
    """Render the saved solver-test response without invoking the full Demo pipeline."""
    node = escape(str(response.get("node", "UNKNOWN")))
    status = escape(str(response.get("status", "UNKNOWN")))
    solver = response.get("solver") or {}
    metrics = response.get("solver_test") or {}
    raw = response.get("raw_solution") or {}
    stage_order = ("ANNUAL", "MONTHLY", "TEN_DAY", "D-3", "D-2", "L3-A", "STORAGE-DA")
    stage_labels = {
        "ANNUAL": "年度", "MONTHLY": "月度", "TEN_DAY": "旬度", "D-3": "D-3",
        "D-2": "D-2", "L3-A": "日前申报", "STORAGE-DA": "日前储能",
    }
    flow = " → ".join(
        '<strong aria-current="step">%s</strong>' % escape(stage_labels[name]) if name == response.get("node")
        else escape(stage_labels[name]) for name in stage_order
    )
    upstream_path = escape(str(metrics.get("upstream_response_file") or "无（链路起点）"))
    curve = response.get("contract_curve_mwh") or raw.get("contract_curve_mwh") or []
    declaration = raw.get("declaration_mwh") or response.get("declaration", {}).get("rows", [])
    storage = response.get("storage") or {}
    if not isinstance(declaration, list):
        declaration = []
    values = curve or declaration
    peak = max([float(v) for v in values if isinstance(v, (int, float))] + [1.0])
    bars = "".join(
        '<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="#087f78"><title>%s: %.5f</title></rect>'
        % (45 + i * 9.2, 210 - float(v) / peak * 160, 7, float(v) / peak * 160, i + 1, float(v))
        for i, v in enumerate(values[:96]) if isinstance(v, (int, float))
    )
    rows = "".join(
        "<tr><td>%s</td><td>%s</td><td>%s</td></tr>" % (
            escape(str(i + 1)), escape(str(v)), escape(str(curve[i] if i < len(curve) else "-")))
        for i, v in enumerate(declaration[:96])
    )
    return """<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\"><title>solver-test · %s</title>
<style>body{font:14px system-ui,sans-serif;max-width:1100px;margin:32px auto;padding:0 20px;color:#1e2a2b}h1{margin-bottom:4px}.meta{color:#607274}.flow{padding:14px 0;border-bottom:1px solid #d8e1e1;overflow-wrap:anywhere}.flow strong{color:#087f78}.upstream{font-size:12px;color:#607274;overflow-wrap:anywhere}.kpis{display:grid;grid-template-columns:repeat(4,1fr);gap:10px;margin:20px 0}.kpi{border:1px solid #d8e1e1;padding:12px}.kpi small{display:block;color:#607274}.kpi strong{display:block;margin-top:6px;font-size:18px}svg{width:100%%;height:260px;border:1px solid #d8e1e1;background:#fbfdfd}table{border-collapse:collapse;width:100%%}th,td{border-bottom:1px solid #e4eaea;padding:7px;text-align:left}pre{overflow:auto;background:#f4f7f7;padding:14px}@media(max-width:700px){.kpis{grid-template-columns:repeat(2,minmax(0,1fr))}}</style></head><body>
<h1>solver-test · %s</h1><div class=\"meta\">状态：%s；该页面只展示本次 request 的 response，不运行其他节点。</div>
<div class=\"flow\"><b>完整决策链</b><br>%s<div class=\"upstream\">上游 response：%s</div></div>
<div class=\"kpis\"><div class=\"kpi\"><small>场景数</small><strong>%s</strong></div><div class=\"kpi\"><small>模型耗时 / 秒</small><strong>%s</strong></div><div class=\"kpi\"><small>变量数</small><strong>%s</strong></div><div class=\"kpi\"><small>约束数</small><strong>%s</strong></div></div>
<h2>合同/申报曲线</h2><svg viewBox=\"0 0 1010 255\" role=\"img\" aria-label=\"合同或申报曲线\"><text x=\"45\" y=\"25\">MWh / 15分钟</text>%s<line x1=\"45\" y1=\"210\" x2=\"960\" y2=\"210\" stroke=\"#789\"/><text x=\"45\" y=\"240\">00:00</text><text x=\"490\" y=\"240\">12:00</text><text x=\"925\" y=\"240\">24:00</text></svg>
<h2>点位数据</h2><table><thead><tr><th>点位</th><th>日前申报</th><th>合同曲线</th></tr></thead><tbody>%s</tbody></table>
<h2>求解器详情</h2><pre>%s</pre></body></html>""" % (
        node, node, status, flow, upstream_path, metrics.get("scenario_count", "-"),
        metrics.get("solver_seconds", solver.get("solve_seconds", "-")),
        solver.get("variable_count", "-"), solver.get("constraint_count", "-"), bars,
        rows, escape(json.dumps(solver, ensure_ascii=False, indent=2)))


def run_request(request_file, upstream_response_file=None):
    request_started = perf_counter()
    request_path = Path(request_file).expanduser().resolve()
    if not request_path.is_file():
        raise FileNotFoundError(f"请在 REQUEST_FILE 中填写有效 request 地址: {request_path}")
    request = json.loads(request_path.read_text(encoding="utf-8"))
    node = request["node"]
    inputs = request["inputs"]
    benchmark = request.get("benchmark", {})
    scenario_count = int(inputs.get("scenario_count", 0))
    customer_count = len(inputs.get("customers", []))
    time_limit = 60.0
    # Enforce the actual backend options, not just the displayed benchmark label.
    inputs.setdefault("l1_solver", {})["time_limit_seconds"] = time_limit
    storage_options = inputs.setdefault("storage", {})
    storage_options["time_limit_seconds"] = time_limit
    storage_options["optimization_scenario_count"] = scenario_count
    storage_options["full_scenario_optimization"] = True
    inputs.setdefault("recommendation_policy", {})["time_limit_seconds"] = time_limit
    upstream_path_value = upstream_response_file or request.get("upstream_response_file", "")
    upstream = None
    upstream_path = None
    if upstream_path_value:
        upstream_path = Path(upstream_path_value).expanduser().resolve()
    request_preparation_seconds = perf_counter() - request_started
    _log("Request 已读取，耗时 %.3f 秒；上游数据将在本阶段内读取" % request_preparation_seconds)
    request_name = request_path.stem
    response_name = request_name.replace("request_", "response_", 1)
    source_group = request_path.parent.name if request_path.parent.name.startswith("request_") else request_name
    response_group = source_group.replace("request_", "response_", 1)
    response_root = RESOURCE / "output" / "solver_test" / response_group / response_name / _timestamp()
    response_root.mkdir(parents=True, exist_ok=False)
    response_path = response_root / "response.json"
    response_path.write_text(json.dumps({
        "node": node,
        "status": "RUNNING",
        "solver_test": {
            "request_file": str(request_path),
            "node": node,
            "upstream_response_file": str(upstream_path) if upstream_path else None,
            "customer_count": customer_count,
            "scenario_count": scenario_count,
            "time_limit_seconds": time_limit,
            "status": "RUNNING",
        },
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    _log("本次 Response 暂存位置=%s（求解结束后更新）" % response_path.resolve())
    started = perf_counter()
    error = None
    result = None
    failure_solver_metrics = {}
    _log("开始运行节点 %s；Python=%s；Request=%s" % (node, sys.executable, request_path))
    if upstream_path_value:
        _log("上游申报 Response=%s" % upstream_path)
    _log("客户数=%d；场景数=%d；目标 Gap=3.00%%；HiGHS求解时限=%.1f 秒（进程清理宽限另计）" % (
        customer_count, scenario_count, time_limit))

    def progress(_percent, message):
        _log(message)

    try:
        # 客户/场景规模来自本次 request，不受生成脚本的默认测试规模限制。
        if benchmark.get("customer_count", customer_count) != customer_count or benchmark.get("scenario_count", scenario_count) != scenario_count:
            raise ValueError("benchmark 客户/场景数量与实际 inputs 不一致")
        if node == "STORAGE-DA" and not inputs.get("day_ahead_price_known"):
            raise ValueError("STORAGE-DA request 必须包含已公布的日前现货价格")
        if upstream_path:
            upstream = json.loads(upstream_path.read_text(encoding="utf-8"))
            expected_upstream = request.get("upstream_node")
            if expected_upstream and upstream.get("node") != expected_upstream:
                raise ValueError(f"{node} 的上游 response 应为 {expected_upstream}，实际为 {upstream.get('node')}")
            if node == "STORAGE-DA" and upstream.get("node") != "L3-A":
                raise ValueError("STORAGE-DA 的上游 response 必须来自 L3-A")
        elif node == "STORAGE-DA":
            raise ValueError("STORAGE-DA 必须提供上游 L3-A response")
        if upstream and upstream.get("status") in {"ERROR", "TIME_LIMIT", "UPSTREAM_FAILED"}:
            raise RuntimeError("上游节点求解失败，不能使用默认数据伪造完整链路")
        if upstream and "fills" not in upstream:
            raise ValueError("上游 response 缺少成交结果 fills，不能继续链路")
        if node == "STORAGE-DA" and (not upstream or "raw_solution" not in upstream):
            raise ValueError("L3-A response 缺少锁定申报及场景数据，不能运行储能日前阶段")
        # The isolated HiGHS worker streams its native log through the watchdog.
        result = solve_request(request, upstream=upstream, progress=progress)
        status = result.get("status", "UNKNOWN")
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        failure_solver_metrics = deepcopy(getattr(exc, "solver_metrics", {}) or {})
        status = "TIME_LIMIT" if ("TIME_LIMIT_REACHED" in error or "LIMIT_REACHED" in error or "Time limit reached" in error) else "ERROR"

    model_execution_seconds = perf_counter() - started
    _log("模型执行结束；状态=%s；耗时 %.3f 秒" % (status, model_execution_seconds))
    solver = (result or {}).get("solver", {})
    if not solver and failure_solver_metrics:
        solver = failure_solver_metrics
    valuation = solver.get("valuation_solver", {}) if isinstance(solver, dict) else {}
    solver_metrics = solver if solver.get("milp_executed") or not valuation else valuation
    optimization = solver_metrics.get("optimization", {}) if isinstance(solver_metrics.get("optimization"), dict) else {}
    solved_scenarios = (
        solver_metrics.get("optimization_scenario_count")
        or solver_metrics.get("scenario_count")
        or optimization.get("optimization_scenario_count")
        or (scenario_count if solver_metrics.get("variable_count", optimization.get("variable_count")) is not None else None)
    )
    milp_seconds = float(solver_metrics.get("solve_seconds") or optimization.get("solve_seconds") or 0.0)
    pricing_seconds = float(solver_metrics.get("pricing_solve_seconds") or optimization.get("pricing_solve_seconds") or 0.0)
    solve_seconds = float(solver_metrics.get("solver_seconds") or optimization.get("solver_seconds") or (milp_seconds + pricing_seconds))
    variable_count = solver_metrics.get("variable_count", optimization.get("variable_count"))
    constraint_count = solver_metrics.get("constraint_count", optimization.get("constraint_count"))
    gap = solver_metrics.get("mip_gap", optimization.get("mip_gap"))
    time_limit=solver_metrics.get('milp_time_limit_seconds', solver_metrics.get('time_limit_seconds',time_limit))
    time_count = solver_metrics.get("time_count", 48 if node in {"ANNUAL", "MONTHLY", "TEN_DAY"} else 96)
    response = deepcopy(result or {"node": node})
    # Package assignments remain persisted business state; terminal formatting
    # is allowed to omit them, but must not mutate the saved response.
    response["solver_test"] = {
        "request_file": str(request_path),
        "node": node,
        "upstream_response_file": str(upstream_path) if upstream_path else None,
        "customer_count": customer_count,
        "scenario_count": scenario_count,
        "solved_scenario_count": solved_scenarios,
        "time_limit_seconds": time_limit,
        "time_count": time_count,
        "target_mip_gap": 0.03,
        "elapsed_seconds": request_preparation_seconds + model_execution_seconds,
        "solver_seconds": solve_seconds,
        "other_processing_seconds": request_preparation_seconds + max(0.0, model_execution_seconds - solve_seconds),
        "request_preparation_seconds": request_preparation_seconds,
        "model_execution_seconds": model_execution_seconds,
        "milp_seconds": milp_seconds,
        "pricing_lp_seconds": pricing_seconds,
        "variable_count": variable_count,
        "constraint_count": constraint_count,
        "mip_gap": gap,
        "status": status,
        "gap_status": solver_metrics.get("gap_status"),
        "node_count": solver_metrics.get("node_count"),
        "binary_count": solver_metrics.get("binary_count"),
        "milp_attempted": variable_count is not None,
        "has_integer_solution": result is not None and variable_count is not None,
        "objective_yuan": solver_metrics.get("objective_yuan"),
        "dual_bound": solver_metrics.get("dual_bound"),
    }
    if error:
        response.update(status=status, error=error)

    write_started = perf_counter()
    response_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    response_write_seconds = perf_counter() - write_started
    response["solver_test"]["response_write_seconds"] = response_write_seconds
    response["solver_test"]["elapsed_seconds"] += response_write_seconds
    response["solver_test"]["other_processing_seconds"] += response_write_seconds
    response_path.write_text(
        json.dumps(response, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    # Rendering is optional; its failure must not suppress the metrics table.
    if not error and result:
        try:
            response_path.with_name('effective_inputs.json').write_text(json.dumps(result['inputs'],ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
            report_path = response_path.with_name("visualize.html")
            report_path.write_text(_render_solver_test_report(response), encoding="utf-8")
            _log("节点可视化已写入 %s" % report_path.resolve())
        except Exception as exc:
            response["artifact_error"] = f"{type(exc).__name__}: {exc}"
            _log("附属文件生成失败=%s" % response["artifact_error"])
    # Snapshot once: terminal and persisted response use identical timings.
    # The final metrics write and printing this summary are outside this snapshot.
    response["solver_test"]["elapsed_seconds"] = perf_counter() - request_started
    response["solver_test"]["other_processing_seconds"] = max(0., response["solver_test"]["elapsed_seconds"] - solve_seconds)
    response_path.write_text(json.dumps(response,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    _log("Response 已写入 %s；写入耗时 %.3f 秒" % (response_path.resolve(), response_write_seconds))
    _log("求解状态=%s；变量数=%s；约束数=%s" % (status, variable_count, constraint_count))
    _log("阶段耗时汇总")
    _log("Request读取: %.3fs" % request_preparation_seconds)
    _log("MILP求解: %.3fs" % milp_seconds)
    if pricing_seconds:
        _log("固定整数定价LP: %.3fs" % pricing_seconds)
    _log("模型其余处理: %.3fs" % max(0.0, model_execution_seconds - solve_seconds))
    _log("Response写入: %.3fs" % response_write_seconds)
    _log("模型执行: %.3fs；端到端总耗时: %.3fs" % (
        model_execution_seconds, response["solver_test"]["elapsed_seconds"]))
    if gap is None and solver_metrics.get("gap_status") == "INFINITE_NO_INCUMBENT":
        gap_display = "inf（未找到整数可行解）"
    else:
        gap_display = "N/A" if gap is None else "%.6f%%" % (float(gap) * 100)
    _log("Gap=%s" % gap_display)
    _log("本次 Request 阶段统计")
    _format_request_metrics(
        node, customer_count, time_count, solved_scenarios,
        variable_count, constraint_count, solve_seconds,
        response["solver_test"]["other_processing_seconds"],
        response["solver_test"]["elapsed_seconds"], gap, time_limit,
    )
    if solver_metrics:
        _log('整数变量=%s；分支节点=%s；原始求解目标=%s；最优界=%s；整数残差=%s' % (
            solver_metrics.get('binary_count'),solver_metrics.get('node_count'),solver_metrics.get('objective_yuan'),
            solver_metrics.get('dual_bound'),solver_metrics.get('max_binary_integrality_residual')))
    batteries=_customer_storage_rows(response.get('raw_solution',{}))
    if batteries:
        print('\n| 客户 | 容量MWh | 充电功率MW | 充电MWh | 放电MWh | 终值SOC MWh | 启动次数 | 停机次数 | 启停费用/元 |',flush=True)
        print('|'+ '|'.join([':---:']*9)+'|',flush=True)
        for row in batteries:
            print('| '+' | '.join(str(v) if isinstance(v,str) else f'{v:.4f}' for v in row)+' |',flush=True)
    _log("Response=%s" % response_path.resolve())
    if error:
        _log("运行错误=%s" % error)
    return response, response_path.resolve()


class _Tee:
    def __init__(self, terminal, logfile):
        self.terminal, self.logfile = terminal, logfile

    def write(self, value):
        self.terminal.write(value)
        self.logfile.write(value)
        return len(value)

    def flush(self):
        self.terminal.flush()
        self.logfile.flush()


def run_matrix(scenario_counts=None, output_dir=None, resume=False):
    """显式批量模式；普通 run_request 不会调用此函数。"""
    from pifa_lingshou.solver_test.request_generation import (
        allocate_run_number, build_request, save_request, NODE_LABELS, SCENARIO_COUNTS,
        CUSTOMER_COUNT,
        _timestamp as request_timestamp,
    )
    scenario_counts = tuple(SCENARIO_COUNTS if scenario_counts is None else scenario_counts)
    customer_count = CUSTOMER_COUNT
    root = Path(output_dir) if output_dir else RESOURCE / 'output' / 'solver_test' / (f'matrix_{customer_count}customers_' + request_timestamp())
    root.mkdir(parents=True, exist_ok=True)
    manifest = dict(customer_count=customer_count, scenario_counts=list(scenario_counts),
                    time_limit_seconds=60., full_scenario_optimization=True, runs=[])
    manifest_path = root / 'manifest.json'
    if resume:
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        if manifest['customer_count'] != customer_count or manifest['scenario_counts'] != list(scenario_counts):
            raise ValueError('续跑矩阵的客户/场景配置与 manifest 不一致')
    elif manifest_path.exists():
        raise FileExistsError('目录已有历史矩阵；请换目录或使用 --resume')
    completed = {(r['scenario_count'], r['node']): r for r in manifest['runs'] if r['verified']}
    with (root / 'terminal.log').open('a' if resume else 'w', encoding='utf-8', buffering=1) as logfile:
        with redirect_stdout(_Tee(sys.stdout, logfile)), redirect_stderr(_Tee(sys.stderr, logfile)):
            _log('显式批量验证：%s客户；场景数=%s；每个阶段单独求解，HiGHS MILP时限60秒' % (customer_count, list(scenario_counts)))
            for count in scenario_counts:
                upstream_path = ''
                run_number = None
                for node in NODE_LABELS:
                    previous = completed.get((count, node))
                    if previous:
                        upstream_path = previous['response_file']
                        _log('续跑：复用已验证 %s / %s场景，不重复求解' % (node, count))
                        continue
                    request = build_request(node=node, customer_count=customer_count, scenario_count=count,
                                            upstream_response_file=upstream_path)
                    if run_number is None:
                        # 新场景配置分配新批次；续跑时沿用上游所属批次。
                        if upstream_path:
                            request_path = save_request(request)
                            run_number = int(request_path.parent.name.removeprefix('request_'))
                        else:
                            run_number = allocate_run_number()
                            request_path = save_request(request, run_number=run_number)
                    else:
                        request_path = save_request(request, run_number=run_number)
                    response, response_path = run_request(request_path)
                    metrics = response['solver_test']
                    verified = (response.get('status') in {'OPTIMAL', 'LIMIT_REACHED', 'EXECUTED'}
                                and metrics['variable_count'] is not None
                                and metrics['customer_count'] == customer_count
                                and metrics['solved_scenario_count'] == count
                                and metrics['time_limit_seconds'] == 60.)
                    manifest['runs'] = [r for r in manifest['runs'] if (r['scenario_count'], r['node']) != (count, node)]
                    manifest['runs'].append(dict(node=node, scenario_count=count,
                        request_file=str(request_path), response_file=str(response_path),
                        status=response.get('status'), verified=verified, metrics=metrics,
                        error=response.get('error')))
                    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
                    upstream_path = str(response_path)
            _log('批量真实统计汇总；失败项的 N/A 不是零，也不是成功求解')
            for entry in manifest['runs']:
                m = entry['metrics']
                _log('%s / %s场景：%s' % (entry['node'], entry['scenario_count'], entry['status']))
                _format_request_metrics(entry['node'], m['customer_count'], m['time_count'],
                    m['solved_scenario_count'], m['variable_count'], m['constraint_count'],
                    m['solver_seconds'], m['other_processing_seconds'], m['elapsed_seconds'],
                    m['mip_gap'], m['time_limit_seconds'])
            _log('批量验证记录=%s' % manifest_path.resolve())
    return manifest, manifest_path


def main():
    parser = argparse.ArgumentParser(description="只求解一个 request；不自动运行任何上游阶段")
    parser.add_argument("request", nargs="?", default=REQUEST_FILE)
    parser.add_argument("--upstream", default=UPSTREAM_RESPONSE_FILE or None)
    parser.add_argument("--matrix", action="store_true", help="按 request_generation.py 的客户数和场景配置显式批量运行")
    parser.add_argument("--scenarios", type=int, nargs="+", help="覆盖批量生成的场景数；不限制已有 request 的场景数")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--resume", action="store_true", help="续跑 output-dir 中的矩阵")
    args = parser.parse_args()
    if args.matrix:
        if args.request or args.upstream:
            parser.error("--matrix 不能同时指定单个 request 或上游覆盖路径；请将顶部 REQUEST_FILE 和 UPSTREAM_RESPONSE_FILE 留空")
        if args.resume and not args.output_dir:
            parser.error("--resume 必须同时指定 --output-dir")
        manifest, _ = run_matrix(args.scenarios, args.output_dir, args.resume)
        if not all(entry['verified'] for entry in manifest['runs']):
            raise SystemExit(1)
        return
    if args.scenarios is not None or args.output_dir is not None or args.resume:
        parser.error("--scenarios、--output-dir、--resume 仅可与 --matrix 一起使用")
    if not args.request:
        from pifa_lingshou.solver_test.request_generation import build_request, save_request
        request = build_request(node="ANNUAL")
        args.request = save_request(request)
        _log("未指定 request，按生成配置创建单个 ANNUAL request（%s客户、%s场景）=%s" % (
            len(request['inputs']['customers']), request['inputs']['scenario_count'], args.request))
    response, _ = run_request(args.request, args.upstream)
    if response.get("status") in {"ERROR", "TIME_LIMIT", "UPSTREAM_FAILED"}:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
