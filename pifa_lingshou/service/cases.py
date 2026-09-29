"""Persistent case state machine; signing precedes dated procurement decisions."""
from copy import deepcopy
from datetime import datetime,timedelta,date
from random import Random
from threading import RLock
from uuid import uuid4
from ..inputs.case_data import mock_case,validate_case,layer_input,NODES,curve
from ..utils.resource_store import CaseStore
from .recommendation import recommend
from .layer_runner import run_layer

LOCK=RLock()


class CaseService:
    def __init__(self,root=None):self.store=CaseStore(root)

    def view(self,case_id):
        s=self.store.state(case_id)
        return dict(state=s,input=self.store.data(s))

    def create(self,settings=None):
        data=mock_case(settings);cid=datetime.now().strftime('%Y%m%d-%H%M%S-')+uuid4().hex[:8]
        state=dict(case_id=cid,name=data['name'],target_date=data['target_date'],created_at=datetime.now().astimezone().isoformat(),
            revision=1,status='DRAFT',stages=[],packages={},clock=data['signing_forecast']['issued_at'])
        self.store.write('input',cid,'revision_0001.json',data);self.store.save_state(state)
        return self.view(cid)

    def save(self,cid,data,revision):
        with LOCK:
            s=self.store.state(cid)
            if s['revision']!=revision:raise ValueError('案例已更新，请重新加载')
            if s['packages']:raise ValueError('已确认套餐；请用节点预测更新，或新建案例修改签约数据')
            validate_case(data);s.update(revision=revision+1,status='DRAFT',name=data['name'],target_date=data['target_date'])
            s.pop('recommendation',None)
            self.store.write('input',cid,f'revision_{s["revision"]:04d}.json',data);self.store.save_state(s)
        return self.view(cid)

    def recommend(self,cid,revision):
        with LOCK:
            s=self.store.state(cid)
            if revision!=s['revision'] or s['packages']:raise ValueError('签约状态或输入版本已改变')
            result=recommend(self.store.data(s));s.update(status='RECOMMENDED',recommendation=result)
            self.store.write('output',cid,f'recommendation_{revision:04d}.json',result);self.store.save_state(s)
        return self.view(cid)

    def confirm(self,cid,packages,revision):
        with LOCK:
            s=self.store.state(cid);data=self.store.data(s)
            if s['status']!='RECOMMENDED' or revision!=s['revision']:raise ValueError('请先确认当前数据并计算推荐')
            ids={c['terms']['customer_id'] for c in data['customers']}
            if set(packages)!=ids or any(p not in ('F','L','S') for p in packages.values()):raise ValueError('须逐客户确认F/L/S套餐')
            s.update(status='SIGNED',packages=packages)
            self.store.write('input',cid,'signed_packages.json',dict(revision=revision,packages=packages,terms=[c['terms'] for c in data['customers']]))
            self.store.save_state(s)
        return self.view(cid)

    def update_forecast(self,cid,node,forecast,revision):
        with LOCK:
            s=self.store.state(cid);data=self.store.data(s)
            if not s['packages'] or s['status']=='SETTLED' or revision!=s['revision']:raise ValueError('请加载当前已签案例')
            completed=[r['node'] for r in s['stages']]
            if node not in NODES or (node in completed and node!='STORAGE-RT'):raise ValueError('已成交节点不可改写')
            data['forecasts'][node]=forecast;validate_case(data)
            s['revision']+=1
            self.store.write('input',cid,f'revision_{s["revision"]:04d}.json',data);self.store.save_state(s)
        return self.view(cid)

    def run(self,cid,node,revision,execution=None,progress=None):
        with LOCK:
            s=self.store.state(cid);data=self.store.data(s)
            if revision!=s['revision'] or not s['packages'] or s['status']=='SETTLED':raise ValueError('先确认套餐，并使用当前版本输入')
            done=[r['node'] for r in s['stages'] if r['node']!='STORAGE-RT']
            expected=NODES[len(done)] if len(done)<7 else 'STORAGE-RT'
            if node!=expected:raise ValueError('下一节点应为 '+expected)
            inputs=layer_input(data,node,s['packages'])
            upstream=self.store.read('output',cid,s['stages'][-1]['result_file']) if s['stages'] else None
            if node=='STORAGE-RT':
                if not execution or execution.get('fixed_until',0)<=0:raise ValueError('日内滚动须输入真实已执行前缀')
                allowed={'fixed_until','actual_load_mwh','actual_rt_price','executed_storage'}
                if set(execution)-allowed:raise ValueError('日内实绩字段不合法')
                inputs.update(execution)
            if progress:progress(2,'案例 '+cid+'：读取已签条款与本节点预测')
            result=run_layer(node,inputs,upstream,progress)
            result['forecast_context']={k:data['forecasts'][node][k] for k in ('as_of','scope_start','scope_end')}
            result['forecast_context']['representation']='范围内日均96点代表曲线；合同按交割日历天数缩放，考核统一折算至本节点范围'
            result['case_id']=cid;result['input_revision']=revision
            number=len(s['stages'])+1;prefix=f'{number:02d}_{node}'
            self.store.write('input',cid,prefix+'_request.json',dict(node=node,inputs=inputs,input_revision=revision))
            self.store.write('output',cid,prefix+'.json',result)
            from ..utils.visualize import render_layer
            page=render_layer(result)
            self.store.path('output',cid,prefix+'.html').write_text(page,encoding='utf-8')
            stage=dict(node=node,result_file=prefix+'.json',report_file=prefix+'.html',status=result['status'],
                elapsed_seconds=result['elapsed_seconds'],forecast=result['forecast_context'],input_revision=revision)
            s['stages'].append(stage);s.update(status='TRADING',clock=inputs['as_of'])
            if node=='STORAGE-RT':s['fixed_until']=inputs['fixed_until']
            self.store.save_state(s)
        return self.view(cid)

    def compare_risk(self,cid,revision,progress=None,lambdas=(0.,.25,.5,.75,1.)):
        """Replay saved ex-ante inputs for comparison without posting any new trade."""
        from ..service.full_evaluate import account_layer,report_defaults
        from ..service.layer_runner import config_from
        from ..utils.visualize import render_report
        with LOCK:
            s=self.store.state(cid)
            if revision!=s['revision'] or not any(r['node']=='STORAGE-DA' for r in s['stages']):
                raise ValueError('请先完成日前策略，并使用当前版本')
            records=s['stages'][:7];results=[]
            for i,risk in enumerate(lambdas):
                upstream=None
                for index,row in enumerate(records):
                    saved=self.store.read('input',cid,row['result_file'].replace('.json','_request.json'))
                    inputs=deepcopy(saved['inputs']);inputs['risk_lambda']=risk
                    upstream=run_layer(row['node'],inputs,upstream)
                    if progress:progress(100*(i+(index+1)/7)/len(lambdas),f'比较 λ={risk:g} · {row["node"]}')
                self.store.write('output',cid,f'risk_{risk:g}_solution.json',upstream)
                results.append(account_layer(upstream))
            payload=dict(results=results,packages=['F'],risk_lambdas=list(lambdas),cvar_alpha=.95,
                target_date=self.store.data(s)['target_date'],input_defaults=report_defaults(config_from(inputs),inputs),
                remaining_issues=['本报告是已签套餐下按各历史节点预测的λ敏感性复算，不产生新成交。','P10–P90为模型情景区间，尚未用真实历史数据校准覆盖率。'])
            self.store.write('output',cid,'risk_comparison.json',payload)
            self.store.path('output',cid,'risk_comparison.html').write_text(render_report(payload),encoding='utf-8')
            s['risk_comparison']=dict(report_file='risk_comparison.html',result_file='risk_comparison.json',input_revision=revision)
            self.store.save_state(s)
        return self.view(cid)

    def actual_template(self,cid):
        s=self.store.state(cid);data=self.store.data(s)
        if not any(r['node']=='STORAGE-DA' for r in s['stages']):raise ValueError('请先完成日前储能计划')
        raw=self.store.read('output',cid,s['stages'][-1]['result_file'])['raw_solution']
        f=data['forecasts']['STORAGE-RT'];rng=Random(data['seed']+971)
        fixed=s.get('fixed_until',0)
        loads={key:[round(q*(1+rng.uniform(-.03,.03)),6) for q in values] for key,values in f['customer_load_mwh'].items()}
        rt=[p+rng.uniform(-15,15) for p in f['real_time_price']]
        if fixed:
            last=self.store.read('output',cid,s['stages'][-1]['result_file'])
            for j in range(fixed):
                total=sum(v[j] for v in loads.values())
                for v in loads.values():v[j]=v[j]/total*last['inputs']['actual_load_mwh'][j] if total else 0.
                rt[j]=last['inputs']['actual_rt_price'][j]
        return dict(source='MOCK_REPLAY',target_date=data['target_date'],
            settled_at=(date.fromisoformat(data['target_date'])+timedelta(days=1)).isoformat(),
            customer_load_mwh=loads,day_ahead_price=data['forecasts']['L3-A']['day_ahead_price'],real_time_price=rt,
            charge_mwh=raw['charge_mwh'],discharge_mwh=raw['discharge_mwh'],soc_mwh=raw['soc_mwh'],
            note='模拟回放：未来动作按最新计划执行生成待确认实绩，确认前可修改；不用于事前优化')

    def settle(self,cid,actual,revision,progress=None):
        with LOCK:
            s=self.store.state(cid);data=self.store.data(s)
            if revision!=s['revision'] or not any(r['node']=='STORAGE-DA' for r in s['stages']) or s['status']=='SETTLED':
                raise ValueError('请先完成日前调度，且勿重复结算')
            if date.fromisoformat(actual['settled_at'])<=date.fromisoformat(data['target_date']):raise ValueError('应在标的日结束后结算')
            from ..accounting.replay import settle
            last=self.store.read('output',cid,s['stages'][-1]['result_file'])
            if progress:progress(20,'核验实绩、锁定历史与SOC；按已成交合同回放现金流')
            result=settle(data,s,last,actual)
            self.store.write('input',cid,'settlement_actuals.json',actual)
            self.store.write('output',cid,'settlement.json',result)
            from ..utils.visualize import render_settlement
            self.store.path('output',cid,'settlement.html').write_text(render_settlement(result),encoding='utf-8')
            s.update(status='SETTLED',clock=actual['settled_at'],settlement=result)
            self.store.save_state(s)
            if progress:progress(100,'结算完成：客户分时电价节省、公司成本收益与价差')
        return self.view(cid)
