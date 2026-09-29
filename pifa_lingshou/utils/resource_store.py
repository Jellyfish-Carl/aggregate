"""Atomic, revisioned per-case input/output files."""
import json
import re
from pathlib import Path
from uuid import uuid4

RESOURCE=Path(__file__).resolve().parents[1]/'resource'


def write_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    temporary=path.with_name(path.name+'.'+uuid4().hex+'.tmp')
    temporary.write_text(json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    temporary.replace(path)
    return path


class CaseStore:
    def __init__(self,root=None):self.root=Path(root or RESOURCE).resolve()

    def path(self,kind,case_id,*parts):
        if kind not in ('input','output') or not isinstance(case_id,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,80}',case_id):
            raise ValueError('非法案例ID')
        path=(self.root/kind/case_id).joinpath(*parts).resolve()
        if not path.is_relative_to(self.root/kind/case_id):raise ValueError('非法资源路径')
        return path

    def read(self,kind,case_id,*parts):
        return json.loads(self.path(kind,case_id,*parts).read_text(encoding='utf-8'))

    def write(self,kind,case_id,name,value):return write_json(self.path(kind,case_id,name),value)

    def state(self,case_id):return self.read('output',case_id,'state.json')

    def save_state(self,state):self.write('output',state['case_id'],'state.json',state)

    def data(self,state):return self.read('input',state['case_id'],f"revision_{state['revision']:04d}.json")

    def listing(self):
        result=[]
        for path in self.root.glob('output/*/state.json'):
            s=json.loads(path.read_text(encoding='utf-8'))
            result.append({k:s[k] for k in ('case_id','name','status','target_date','revision','created_at')})
        return sorted(result,key=lambda s:s['created_at'],reverse=True)
