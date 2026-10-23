"""生成单个 pifa_lingshou 节点 request；修改文件顶部参数后在 IDE 运行。"""
from datetime import datetime
import json
import re
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from pifa_lingshou.service.case_data import layer_input, mock_case
from pifa_lingshou.utils.resource_store import RESOURCE


# 【修改这里】选择交易节点；非 ANNUAL 节点必须填写上一个节点的 response 地址。
NODE = "ANNUAL"
CUSTOMER_COUNT = 10  # 本批生成配置，不是 run.py 的客户数限制；可修改为其他有效客户数。
SCENARIO_COUNT = 5  # 单个 request 的场景数。
SCENARIO_COUNTS = (5, 20, 50, 100, 200, 300, 500)  # 批量生成的默认场景档位。
SEED = 20261022
TIME_LIMIT_SECONDS = 60.0
TARGET_GAP = 0.03
# 非 ANNUAL 节点需要：填入上一个节点 response.json 的完整路径。
UPSTREAM_RESPONSE_FILE = ""

NODE_LABELS = {
    "ANNUAL": "annual",
    "MONTHLY": "monthly",
    "TEN_DAY": "ten_day",
    "D-3": "d3",
    "D-2": "d2",
    "L3-A": "day_ahead_declaration",
    "STORAGE-DA": "storage_day_ahead",
}
UPSTREAM_NODE = {
    "MONTHLY": "ANNUAL",
    "TEN_DAY": "MONTHLY",
    "D-3": "TEN_DAY",
    "D-2": "D-3",
    "L3-A": "D-2",
    "STORAGE-DA": "L3-A",
}


def _timestamp():
    return datetime.now(ZoneInfo("Asia/Shanghai")).strftime("%Y%m%d%H%M%S%f")


def build_request(node=NODE, customer_count=CUSTOMER_COUNT,
                  scenario_count=SCENARIO_COUNT, seed=SEED,
                  upstream_response_file=UPSTREAM_RESPONSE_FILE):
    if node not in NODE_LABELS:
        raise ValueError("不支持的求解节点: " + str(node))
    if type(customer_count) is not int or not 1 <= customer_count <= 100:
        raise ValueError("模拟输入的客户数量须为1至100的整数")
    if type(scenario_count) is not int or not 1 <= scenario_count <= 500:
        raise ValueError("模拟输入的场景数量须为1至500的整数")
    data = mock_case({
        "customer_count": customer_count,
        "scenario_count": scenario_count,
        "seed": seed,
        "target_date": "2026-09-15",
        "recommendation_days": 30,
    })
    inputs = layer_input(data, node, None)
    inputs["customer_packages"] = {
        customer["terms"]["customer_id"]: "F" for customer in data["customers"]
    }
    inputs["scenario_count"] = scenario_count
    inputs["l1_solver"] = {
        "time_limit_seconds": TIME_LIMIT_SECONDS,
        "mip_relative_gap": TARGET_GAP,
    }
    inputs["storage"].update(
        time_limit_seconds=TIME_LIMIT_SECONDS,
        mip_relative_gap=TARGET_GAP,
        optimization_scenario_count=scenario_count,
        full_scenario_optimization=True,
    )
    request = {
        "node": node,
        "inputs": inputs,
        "benchmark": {
            "customer_count": customer_count,
            "scenario_count": scenario_count,
            "time_limit_seconds": TIME_LIMIT_SECONDS,
            "target_mip_gap": TARGET_GAP,
            "seed": seed,
        },
    }
    if node == "STORAGE-DA":
        request["benchmark"]["price_information"] = "已公布日前现货价格"
    elif node == "L3-A":
        request["benchmark"]["price_information"] = "日前现货价格尚未公布，使用预测场景"
    if node in UPSTREAM_NODE:
        if not upstream_response_file:
            raise ValueError(f"{node} request 必须提供 {UPSTREAM_NODE[node]} 的 response 地址")
        upstream_path = Path(upstream_response_file).expanduser().resolve()
        if not upstream_path.is_file():
            raise FileNotFoundError(f"找不到上游 response: {upstream_path}")
        upstream = json.loads(upstream_path.read_text(encoding="utf-8"))
        if upstream.get("node") != UPSTREAM_NODE[node]:
            raise ValueError(f"{node} 需要 {UPSTREAM_NODE[node]} 的 response，实际为 {upstream.get('node')}")
        request["upstream_node"] = UPSTREAM_NODE[node]
        request["upstream_response_file"] = str(upstream_path)
    return request


def allocate_run_number():
    """为一批测试预留编号；编号与交易阶段无关。"""
    input_root = RESOURCE / "input" / "solver_test"
    input_root.mkdir(parents=True, exist_ok=True)
    numbers = [0]
    for root, prefix in ((input_root, "request"),
                         (RESOURCE / "output" / "solver_test", "response")):
        for path in root.glob(f"{prefix}_*"):
            match = re.fullmatch(rf"{prefix}_(\d+)", path.name)
            if match:
                numbers.append(int(match[1]))
    number = max(numbers) + 1
    while True:
        try:
            (input_root / f"request_{number:02d}").mkdir()
            return number
        except FileExistsError:
            number += 1


def save_request(request, run_number=None):
    if run_number is None:
        upstream = request.get("upstream_response_file")
        if upstream:
            upstream_path = Path(upstream).expanduser().resolve()
            output_root = (RESOURCE / "output" / "solver_test").resolve()
            if upstream_path.is_relative_to(output_root):
                group = upstream_path.relative_to(output_root).parts[0]
                match = re.fullmatch(r"response_(\d+)", group)
                if match:
                    run_number = int(match[1])
        if run_number is None:
            run_number = allocate_run_number()
    if not isinstance(run_number, int) or isinstance(run_number, bool) or run_number < 1:
        raise ValueError("测试批次编号必须是正整数")
    node_label = NODE_LABELS[request["node"]]
    folder = RESOURCE / "input" / "solver_test" / f"request_{run_number:02d}"
    folder.mkdir(parents=True, exist_ok=True)
    benchmark = request.get("benchmark", {})
    for previous_path in folder.glob("*.json"):
        previous = json.loads(previous_path.read_text(encoding="utf-8"))
        previous_benchmark = previous.get("benchmark", {})
        if any(previous_benchmark.get(key) != benchmark.get(key)
               for key in ("customer_count", "scenario_count", "seed")):
            raise ValueError("该测试批次的客户数、场景数或种子不同，请新建测试批次")
    path = folder / f"request_{run_number:02d}_{_timestamp()}_{node_label}.json"
    path.write_text(json.dumps(request, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")
    return path.resolve()


def main():
    request = build_request()
    path = save_request(request)
    print("已生成 request，未运行模型。", flush=True)
    print("阶段:", request["node"], flush=True)
    print("客户数:", request["benchmark"]["customer_count"], flush=True)
    print("优化场景数:", request["benchmark"]["scenario_count"], flush=True)
    print("Request:", path, flush=True)
    if request.get("upstream_response_file"):
        print("上游 Response:", request["upstream_response_file"], flush=True)
    print("下一步：将该地址填入 solver_test/run.py 的 REQUEST_FILE。", flush=True)


if __name__ == "__main__":
    main()
