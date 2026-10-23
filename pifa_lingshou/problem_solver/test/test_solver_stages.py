"""Direct stage entry reads the selected request without rebuilding it."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from pifa_lingshou.service.node_requests import RequestStore


def run_stage(request_file, script):
    path = Path(request_file).resolve()
    store = RequestStore(path.parents[3])
    node, request_id = path.parent.parent.name, path.parent.name
    print("运行脚本:", Path(script).resolve(), "\nPython:", sys.executable, flush=True)
    print("决策节点:", node, flush=True)
    saved = store.execute(node, request_id,
        progress=lambda percent, message: print(f"{percent:5.1f}% {message}", flush=True))
    result = saved["result"]
    print("Response:", Path(saved["record"]["path"]) / "response.json", flush=True)
    print("状态:", result["status"], flush=True)
    solver = result.get("solver", {})
    print("变量:", solver.get("variable_count"), "约束:", solver.get("constraint_count"),
          "求解时间:", solver.get("solve_seconds"), "gap:", solver.get("mip_gap"), flush=True)
    return result


class SolverStageBoundaryTests(unittest.TestCase):
    def test_stage_reads_disk_and_preserves_both_runs(self):
        with TemporaryDirectory() as directory:
            store=RequestStore(directory)
            records=store.generate(dict(customer_count=1,recommendation_days=7))
            retail=records['retail']
            def solve(request,upstream,progress):
                return dict(node=request['node'],status='CERTIFIED',quality_certified=True,
                            recommended_portfolio=dict(packages={'C1':'F'}))
            with patch('pifa_lingshou.service.node_requests.solve_request',side_effect=solve):
                run_stage(retail['path'],__file__)
                run_stage(retail['path'],__file__)
            self.assertEqual(len(store.history('retail',retail['id'])),2)
            record=records['annual'];request=record['request'];request['inputs']['annual_price']=477.
            store.save('annual',record['id'],request,record['revision'])
            def annual(request,upstream,progress):
                self.assertEqual(request['inputs']['annual_price'],477.)
                self.assertEqual(request['inputs']['customer_packages'],{'C1':'F'})
                return dict(node='ANNUAL',status='OPTIMAL',inputs=request['inputs'])
            with patch('pifa_lingshou.service.node_requests.solve_request',side_effect=annual):
                run_stage(record['path'],__file__)
            self.assertEqual(json.loads(Path(record['path']).read_text())['inputs']['annual_price'],477.)


if __name__=='__main__':unittest.main()
