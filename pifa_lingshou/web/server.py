"""Loopback-only model runner. One solver job at a time; persistent artifacts."""
from __future__ import annotations
import argparse
import json
import mimetypes
import os
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from time import time
from urllib.parse import unquote, urlparse
from uuid import uuid4
from ..service.layer_runner import run_layer, write_result, NODES
from ..utils.visualize import render_layer, empty_workbench
from ..service.cases import CaseService
from ..config.cases import defaults

ROOT=Path(__file__).resolve().parents[1]; OUT=ROOT/'resource'/'output'
CASES=CaseService()
JOBS={}; CATALOG={}; LOCK=RLock(); WORKERS=ThreadPoolExecutor(max_workers=1)


def start_case_job(payload):
    action=payload.get('action');cid=payload.get('case_id');revision=payload.get('revision')
    if action not in ('recommend','stage','settle','risk'):raise ValueError('未知案例操作')
    CASES.store.state(cid)
    with LOCK:
        if any(j.get('case_id')==cid and j['status'] in ('QUEUED','RUNNING') for j in JOBS.values()):
            raise ValueError('该案例正在计算，请等待完成')
        jid=uuid4().hex[:12]
        JOBS[jid]=dict(status='QUEUED',progress=0.,message='等待求解器',log=[],case_id=cid,error=None)
    def work():
        def progress(percent,message):
            with LOCK:
                JOBS[jid].update(status='RUNNING',progress=min(99.,percent),message=message)
                JOBS[jid]['log'].append(dict(percent=percent,message=message))
        try:
            progress(5,'读取本案例输入版本 '+str(revision))
            if action=='recommend':CASES.recommend(cid,revision)
            elif action=='stage':CASES.run(cid,payload['node'],revision,payload.get('execution'),progress)
            elif action=='risk':CASES.compare_risk(cid,revision,progress)
            else:CASES.settle(cid,payload['actual'],revision,progress)
            with LOCK:JOBS[jid].update(status='DONE',progress=100.,message='已计算并保存案例结果')
        except Exception as exc:
            with LOCK:JOBS[jid].update(status='ERROR',message='计算失败；已完成节点保留',error=str(exc))
    WORKERS.submit(work)
    return jid


def register(path,result,ident=None):
    path=path.resolve(); relative=path.relative_to(OUT.resolve()).as_posix()
    ident=ident or relative
    with LOCK:
        CATALOG[ident]=dict(id=ident,node=result['node'],package=result['package'],
            risk_lambda=result['risk_lambda'],status=result['status'],path=str(path),
            result_url='/outputs/'+relative,updated=time())
    return ident


def catalog():
    index=OUT/'layers'/'index.json'
    if index.exists():
        for item in json.loads(index.read_text()):
            ident=item['id']
            if ident not in CATALOG:
                path=(OUT/item['path']).resolve()
                if OUT.resolve() in path.parents:
                    CATALOG[ident]={**item,'path':str(path),'updated':0.,
                        'result_url':'/outputs/'+path.relative_to(OUT.resolve()).as_posix()}
    return [{k:v for k,v in item.items() if k!='path'} for item in sorted(CATALOG.values(),key=lambda x:x['updated'],reverse=True)]


def start_job(payload):
    node=payload.get('node','L3-A')
    if node not in NODES+('L1','L2','CHAIN'): raise ValueError('未知节点')
    if not isinstance(payload.get('inputs',{}),dict): raise ValueError('inputs必须为对象')
    with LOCK:
        if sum(j['status'] in ('QUEUED','RUNNING') for j in JOBS.values())>=8:
            raise ValueError('已有8项计算排队，请等待完成')
        jid=uuid4().hex[:12]
        JOBS[jid]=dict(status='QUEUED',progress=0.,message='等待求解器',log=[],error=None)
    def work():
        def progress(percent,message):
            with LOCK:
                j=JOBS[jid]; j.update(status='RUNNING',progress=max(j['progress'],min(99.,float(percent))),message=message)
                if not j['log'] or j['log'][-1]['message']!=message:
                    j['log'].append(dict(percent=float(percent),message=message))
        try:
            upstream=payload.get('upstream')
            if payload.get('upstream_id'):
                with LOCK:
                    catalog(); entry=CATALOG.get(payload['upstream_id'])
                if not entry: raise ValueError('上游结果不存在，请重新选择')
                upstream=json.loads(Path(entry['path']).read_text())
            result=run_layer(node,payload.get('inputs') or {},upstream,progress)
            directory=OUT/'jobs'/jid; directory.mkdir(parents=True,exist_ok=True)
            path=write_result(result,directory/'result.json')
            ident=register(path,result,jid)
            progress(99,'核算时段成本并生成本节点图表')
            (directory/'visualize.html').write_text(render_layer(result),encoding='utf-8')
            with LOCK:
                JOBS[jid].update(status='DONE',progress=100.,message=f'{result["node"]} 完成 · {result["status"]} · {result["elapsed_seconds"]:.3f} 秒',
                    result_id=ident,result_url=f'/outputs/jobs/{jid}/result.json',report_url=f'/outputs/jobs/{jid}/visualize.html')
        except Exception as exc:
            with LOCK: JOBS[jid].update(status='ERROR',message='计算未完成',error=f'{type(exc).__name__}: {exc}')
    WORKERS.submit(work)
    return jid


class Handler(BaseHTTPRequestHandler):
    def log_message(self,*args): pass
    def send_body(self,body,content_type,status=200):
        self.send_response(status)
        self.send_header('Content-Type',content_type); self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store'); self.end_headers(); self.wfile.write(body)
    def send_json(self,obj,status=200):
        self.send_body(json.dumps(obj,ensure_ascii=False,allow_nan=False).encode(),'application/json; charset=utf-8',status)
    def do_POST(self):
        origin=self.headers.get('Origin')
        if origin and urlparse(origin).netloc!=self.headers.get('Host'):
            return self.send_json({'error':'仅允许本机同源计算请求'},403)
        try:
            length=int(self.headers.get('Content-Length','0'))
            if not 0<length<=32*1024*1024: raise ValueError('请求须为1字节至32MB')
            payload=json.loads(self.rfile.read(length))
            if not isinstance(payload,dict): raise ValueError('请求须为JSON对象')
            if self.path=='/api/run':return self.send_json({'job_id':start_job(payload)})
            if self.path=='/api/cases/create':return self.send_json(CASES.create(payload.get('settings')))
            if self.path=='/api/cases/save':return self.send_json(CASES.save(payload['case_id'],payload['input'],payload['revision']))
            if self.path=='/api/cases/confirm':return self.send_json(CASES.confirm(payload['case_id'],payload['packages'],payload['revision']))
            if self.path=='/api/cases/forecast':return self.send_json(CASES.update_forecast(payload['case_id'],payload['node'],payload['forecast'],payload['revision']))
            if self.path=='/api/cases/actual-template':return self.send_json(CASES.actual_template(payload['case_id']))
            if self.path=='/api/cases/run':return self.send_json({'job_id':start_case_job(payload)})
            return self.send_json({'error':'not found'},404)
        except (ValueError,TypeError,KeyError,FileNotFoundError) as exc: self.send_json({'error':str(exc)},400)
    def do_GET(self):
        path=unquote(urlparse(self.path).path)
        if path=='/api/health': return self.send_json({'status':'ok'})
        if path=='/workbench':return self.send_body(empty_workbench().encode('utf-8'),'text/html; charset=utf-8')
        if path=='/api/defaults':return self.send_json(defaults())
        if path=='/api/cases':return self.send_json({'cases':CASES.store.listing()})
        if path.startswith('/api/cases/'):
            try:return self.send_json(CASES.view(path.rsplit('/',1)[-1]))
            except (ValueError,FileNotFoundError):return self.send_json({'error':'案例不存在'},404)
        if path=='/api/catalog':
            with LOCK: data=catalog()
            return self.send_json({'results':data})
        if path.startswith('/api/job/'):
            with LOCK: data=dict(JOBS.get(path.rsplit('/',1)[-1],{}))
            return self.send_json(data or {'error':'job not found'},200 if data else 404)
        if path in ('/','/index.html'):
            return self.send_body((ROOT/'web'/'index.html').read_bytes(),'text/html; charset=utf-8')
        elif path in ('/case.css','/case.js'):
            file=ROOT/'web'/path[1:]
            return self.send_body(file.read_bytes(),'text/css; charset=utf-8' if path.endswith('.css') else 'text/javascript; charset=utf-8')
        elif path.startswith('/resource/'):
            file=(CASES.store.root/path[len('/resource/'):]).resolve()
            if not file.is_relative_to(CASES.store.root) or not file.is_file():return self.send_json({'error':'not found'},404)
            mime=mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
            return self.send_body(file.read_bytes(),mime)
        elif path.startswith('/outputs/'):
            file=(OUT/path[len('/outputs/'):]).resolve()
        elif path in ('/demo_report.html','/visualize.html'):
            file=OUT/path[1:]
        else: return self.send_json({'error':'not found'},404)
        if OUT.resolve() not in file.resolve().parents or not file.is_file():
            return self.send_json({'error':'not found'},404)
        mime=mimetypes.guess_type(str(file))[0] or 'application/octet-stream'
        self.send_body(file.read_bytes(),mime+'; charset=utf-8' if mime.startswith('text/') else mime)


def make_server(port=8765): return ThreadingHTTPServer(('127.0.0.1',port),Handler)


def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--port',type=int,default=8765)
    parser.add_argument('--resource-dir',type=Path,help='案例输入输出目录；默认使用项目内 resource')
    parser.add_argument('--ready-file',type=Path)
    args=parser.parse_args()
    global CASES,OUT
    if args.resource_dir:
        CASES=CaseService(args.resource_dir)
        OUT=args.resource_dir.resolve()/'output'
    server=make_server(args.port)
    url=f'http://127.0.0.1:{server.server_port}/'
    if args.ready_file:
        args.ready_file.write_text(json.dumps(dict(url=url,pid=os.getpid())),encoding='utf-8')
    print(url,flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close(); WORKERS.shutdown(wait=False)

if __name__=='__main__': main()
