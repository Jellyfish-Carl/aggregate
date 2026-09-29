from __future__ import annotations
import argparse
import gzip
import json
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4
ROOT=Path(__file__).resolve().parents[2]
from pifa_lingshou.inputs.full_model import FullModelInput
from pifa_lingshou.service.full_evaluate import evaluate_full
from pifa_lingshou.utils.visualize import render_report, empty_workbench


def export_results(payload,output):
    output=Path(output).resolve(); output.mkdir(parents=True,exist_ok=True)
    with gzip.open(output/'demo_full_results.json.gz','wt',encoding='utf-8') as stream:
        json.dump(payload,stream,ensure_ascii=False,separators=(',',':'),allow_nan=False)
    summary={k:v for k,v in payload.items() if k!='results'}
    summary['results']=[{k:v for k,v in r.items() if k not in {'wholesale_engine','scenario_results','customer_results','schedule'}} for r in payload['results']]
    for source,target in zip(payload['results'],summary['results']):
        target['customer_results']={cid:{k:v for k,v in c.items() if k!='scenarios'} for cid,c in source['customer_results'].items()}
    (output/'demo_results.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    page=render_report(payload)
    for name in ('visualize.html','demo_report.html'): (output/name).write_text(page,encoding='utf-8')
    checks=[]
    for r in payload['results']:
        raw=r['wholesale_engine']['wholesale_raw_solution']
        checks.append(dict(package=r['package'],risk_lambda=r['risk_lambda'],chain_status=r.get('chain_status'),
            storage_status=r['status'],charge_mwh=sum(raw['charge_mwh']),discharge_mwh=sum(raw['discharge_mwh']),
            rt_roundtrip_count=sum(min(b,s)>1e-8 for sc in raw['scenarios'] for b,s in zip(sc['rt_buy_mwh'],sc['rt_sell_mwh'])),
            storage_roundtrip_count=sum(min(c,d)>1e-8 for c,d in zip(raw['charge_mwh'],raw['discharge_mwh'])),
            accounting_checks=r['accounting_checks'],storage_comparison=r['storage_validation'],
            solver=r['wholesale_engine']['l3_objective']))
    (output/'validation_summary.json').write_text(json.dumps(checks,ensure_ascii=False,indent=2),encoding='utf-8')


def start_server(output,port=0):
    output=Path(output).resolve(); ready=output/('server-'+uuid4().hex[:8]+'.json')
    log=output/'server.log'
    with log.open('a') as stream:
        proc=subprocess.Popen([sys.executable,'-m','pifa_lingshou.web.server','--port',str(port),'--ready-file',str(ready)],
            cwd=str(ROOT),stdin=subprocess.DEVNULL,stdout=stream,stderr=stream,start_new_session=True)
    for _ in range(60):
        if ready.exists(): return json.loads(ready.read_text())['url']
        if proc.poll() is not None: raise RuntimeError('HTTP服务启动失败，查看 '+str(log))
        time.sleep(.1)
    proc.terminate()
    raise RuntimeError('HTTP服务启动超过6秒，查看 '+str(log))


def main():
    parser=argparse.ArgumentParser(description='独立L1→L2→L3-A→储能MILP；已确定套餐下的事前成本收益评估')
    parser.add_argument('--lambdas',default='0,0.25,0.5,0.75,1')
    parser.add_argument('--packages',default='F,L,S')
    parser.add_argument('--inputs',type=Path,help='可覆盖客户、负荷预测、价格、储能等默认入参的JSON')
    parser.add_argument('--event',default='D-1',choices=['D-1'],help='本入口为事前评估；日内使用layer_runner STORAGE-RT')
    parser.add_argument('--no-server',action='store_true')
    parser.add_argument('--port',type=int,default=0,help='0自动选择可用端口')
    args=parser.parse_args()
    inp=json.loads(args.inputs.read_text(encoding='utf-8')) if args.inputs else {}
    config=FullModelInput(risk_lambdas=tuple(float(x) for x in args.lambdas.split(',')))
    output=Path(__file__).resolve().parents[1]/'resource'/'output'
    payload=evaluate_full(config,packages=tuple(args.packages.split(',')),layer_inputs=inp,output_dir=output/'layers',
        progress=lambda message: print(message,flush=True))
    payload['storage_validation']=payload['results'][0]['storage_validation']
    export_results(payload,output)
    print('套餐 / λ / 批发成本 / 零售收入 / 净收益 / 充电MWh / 放电MWh')
    for r in payload['results']:
        s=r['storage_summary']
        print(f"{r['package']} / {r['risk_lambda']:.2f} / {r['expected_wholesale_cost_yuan']:.2f} / {r['expected_retail_revenue_yuan']:.2f} / {r['expected_profit_yuan']:.2f} / {s['total_charge_mwh']:.4f} / {s['total_discharge_mwh']:.4f}")
    print('HTML文件地址：',(output/'visualize.html').resolve().as_uri(),flush=True)
    print('每个独立节点JSON：',output/'layers')
    if not args.no_server:
        try: print('可视化与分层计算：',start_server(output,args.port),flush=True)
        except (OSError,RuntimeError) as exc: print('报告已生成；本地HTTP服务未能启动：',exc,flush=True)

if __name__=='__main__': main()
