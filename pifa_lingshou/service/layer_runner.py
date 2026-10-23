"""Independent layer and event execution. No calls to the whole simulate chain."""
from __future__ import annotations
import argparse
import json
from copy import deepcopy
from dataclasses import asdict
from math import isfinite
from pathlib import Path
from time import perf_counter
from types import SimpleNamespace
from dataclasses import replace
from .full_model import FullModelInput, revenue_builder, customer_load, retail_prices
from ..data_objects.customer_storage import default_customer_storage, battery_settings
from .economic_policy import DEFAULT_POLICY, period_policy, fixed_package_options, fixed_savings_audit
from ..data_objects.model import Customer
from .wholesale import engine
engine()
from pifa_lingshou.data_objects.domain import ContractFill
from pifa_lingshou.service.load_forecast import (IndustrialParkConfig, build_forecast_snapshots,
    snapshot_index, aggregate_spot_snapshot, product_equivalent_days)
from pifa_lingshou.service.wholesale_mockdata import build_price_forecasts, build_rolling_order_book
from pifa_lingshou.data_objects.scenario import reduced_joint_trajectories
from pifa_lingshou.service.trading import TraderConfig, default_quotes, _fills_from_action
from pifa_lingshou.problem_solver.l1_contract_milp import solve_l1_contract_milp
from pifa_lingshou.problem_solver.l2_rolling_threshold import apply_l2_rolling_threshold
from pifa_lingshou.problem_solver.l3_milp import solve_l3_milp
from pifa_lingshou.service.simulator import _daily_locked_contract_curve, _daily_contract_fixed_cost
from ..problem_solver.storage_milp import dispatch, vector

NODES=('ANNUAL','MONTHLY','TEN_DAY','D-3','D-2','L3-A','STORAGE-DA','STORAGE-RT')
DEFAULT_INPUT=dict(package='F',risk_lambda=.25,annual_price=405.,monthly_price=416.,
    ten_day_price=424.,scenario_count=64,physical_peak_mw=20.,fixed_until=0,
    storage={'hard_deviation':False,'assessment_basis':'grid_net_load'},retail_linked=True)


def config_from(inputs):
    keys=('annual_price','monthly_price','ten_day_price','reference_weights','customer_shares','customer_packages','annual_reference_price','monthly_reference_price')
    kw={k:inputs[k] for k in keys if k in inputs}
    if 'customers' in inputs: kw['customers']=tuple(Customer(**c) for c in inputs['customers'])
    config=FullModelInput(risk_lambdas=(inputs['risk_lambda'],),**kw)
    config.validate()
    return config


def _inputs(inputs, upstream):
    supplied=asdict(inputs) if isinstance(inputs,FullModelInput) else dict(inputs or {})
    if 'node' in supplied or 'inputs' in supplied:
        raise ValueError('inputs 只接收模型参数；完整 request 请使用 solve_request，或传入 request["inputs"]')
    if isinstance(inputs,FullModelInput) and inputs.risk_lambdas:
        supplied.setdefault('risk_lambda',float(inputs.risk_lambdas[0]))
    inherited=(upstream or {}).get('inputs',{})
    # Transactional/manual upstream data is not a configuration default. A
    # subsequent layer must inherit the resulting fills, not the old input.
    inherited={k:v for k,v in inherited.items() if k not in ('fills','declaration_mwh','executed_storage','order_book')}
    out={**deepcopy(DEFAULT_INPUT),**deepcopy(inherited),**deepcopy(supplied)}
    out['storage']={**DEFAULT_INPUT['storage'],**deepcopy(inherited.get('storage',{})),**deepcopy(supplied.get('storage',{}))}
    if out['package'] not in ('F','L','S'): raise ValueError('套餐必须为F/L/S')
    def locked_terms(value):
        record=asdict(config_from(value))
        record.pop('customer_shares',None)  # Meter/forecast shares change over time.
        for key in ('annual_price','monthly_price','ten_day_price'): record.pop(key,None)
        return json.dumps(record,sort_keys=True)
    if upstream and (out['package']!=upstream.get('package', out['package']) or locked_terms(out)!=locked_terms({**out,**inherited})):
        raise ValueError('已签套餐及零售条款已锁定；修改套餐须重新从L1开始')
    if not isinstance(out['scenario_count'],int) or not 1<=out['scenario_count']<=500:
        raise ValueError('scenario_count必须在1到500之间')
    if 'assessment_basis' in out:
        out['storage']['assessment_basis']=out.pop('assessment_basis')
    if not isfinite(out['physical_peak_mw']) or out['physical_peak_mw']<=0:
        raise ValueError('physical_peak_mw必须为正有限数值')
    config=config_from(out)
    ids=[c.customer_id for c in config.customers]
    if 'customer_storage' not in out:
        out['customer_storage']={cid:default_customer_storage(i) for i,cid in enumerate(sorted(ids))}
        out['input_migrations']=list(out.get('input_migrations',[]))+['legacy request: per-customer battery defaults v2']
    if set(out['customer_storage'])!=set(ids): raise ValueError('逐客户储能ID必须与客户集合一致')
    for v in out['customer_storage'].values(): battery_settings(v)
    if upstream and inherited.get('customer_storage') and out['customer_storage']!=inherited['customer_storage']:
        raise ValueError('逐客户储能设备参数已锁定，不能在后续阶段改写')
    out['portfolio_policy']={**DEFAULT_POLICY,**out.get('portfolio_policy',{})}
    period_policy(out)
    out['model_version']='customer-storage-request-marginals-v2'
    if 'customer_load_forecast_mwh' not in out:
        # Explicit legacy migration: preserve provided aggregate marginals;
        # shares are the only client forecast information in legacy requests.
        if 'load_forecast_mwh' not in out:
            _,legacy_load,_=_data(out)
            for key,col in (('load_forecast_mwh','p50_mwh'),('load_p10_mwh','p10_mwh'),('load_p90_mwh','p90_mwh')):
                out[key]=[r[col] for r in legacy_load['rows']]
        for target,source in (('customer_load_forecast_mwh','load_forecast_mwh'),('customer_load_p10_mwh','load_p10_mwh'),('customer_load_p90_mwh','load_p90_mwh')):
            base=out.get(source,out['load_forecast_mwh'])
            out[target]={c.customer_id:[q*config.customer_shares[i][j] for j,q in enumerate(base)] for i,c in enumerate(config.customers)}
        out.setdefault('input_migrations',[]).append('legacy customer forecasts allocated by supplied shares; correlated customer innovations v2')
    out.setdefault('customer_baseline_price',{cid:[310. if j<28 else 730. if 36<=j<44 or 68<=j<84 else 510. for j in range(96)] for cid in ids})
    for field,value in (('billing_basis','native_load'),('storage_cost_bearer','retailer'),('baseline_storage',False)):
        out.setdefault(field,value)
        if out[field]!=value: raise ValueError(field+'当前只支持'+str(value))
    out['operating_cost_yuan_per_mwh']=0.
    return out


def _data(inputs,event='D-1'):
    park=IndustrialParkConfig(target_date=inputs.get('target_date','2026-09-15'),
        physical_peak_mw=inputs['physical_peak_mw'],delivery_days_override=inputs.get('delivery_days',{}))
    load=deepcopy(snapshot_index(build_forecast_snapshots(park,'high'))[event])
    price=deepcopy(build_price_forecasts().get(event,build_price_forecasts()['D-1']))
    mapping={'load_forecast_mwh':'p50_mwh','load_p10_mwh':'p10_mwh','load_p90_mwh':'p90_mwh'}
    if 'load_forecast_mwh' in inputs:
        values=vector(inputs['load_forecast_mwh'],'load_forecast_mwh',nonnegative=True)
        for row,v in zip(load['rows'],values):
            old=row['p50_mwh']; ratio=v/old if old else 1
            for key in ('p10_mwh','p50_mwh','p75_mwh','p90_mwh'):
                if key in row: row[key]*=ratio
            row['p50_mwh']=v
    for key,col in mapping.items():
        if key in inputs:
            for row,v in zip(load['rows'],vector(inputs[key],key,nonnegative=True)): row[col]=v
    for row in load['rows']:
        if not 0<=row['p10_mwh']<=row['p50_mwh']<=row['p90_mwh']: raise ValueError('负荷须满足P10≤P50≤P90')
        row['p75_mwh']=(row['p50_mwh']+row['p90_mwh'])/2
    for key,prefix in (('day_ahead_price_forecast','day_ahead'),('rt_price_forecast','real_time')):
        if key in inputs:
            for row,v in zip(price['rows'],vector(inputs[key],key)):
                delta=v-row[prefix+'_p50']
                for q in ('p10','p50','p90'): row[prefix+'_'+q]+=delta
    for prefix,key in (('day_ahead','day_ahead_price'),('real_time','rt_price')):
        for quantile in ('p10','p90'):
            if key+'_'+quantile in inputs:
                for row,v in zip(price['rows'],vector(inputs[key+'_'+quantile],key+'_'+quantile)):
                    row[prefix+'_'+quantile]=v
        if any(not r[prefix+'_p10']<=r[prefix+'_p50']<=r[prefix+'_p90'] for r in price['rows']):
            raise ValueError('价格须满足P10≤P50≤P90')
    load['published_at']=inputs.get('as_of',load.get('published_at'))
    price['published_at']=inputs.get('as_of',price.get('published_at'))
    if inputs.get('day_ahead_price_known'):
        known=vector(inputs.get('known_day_ahead_price',inputs.get('day_ahead_price_forecast',[])),'已公布日前价格')
        for row,value in zip(price['rows'],known):
            for quantile in ('p10','p50','p90'):row['day_ahead_'+quantile]=value
        price['information_state']='OFFICIAL_DAY_AHEAD_REVEALED'
    for row in price['rows']:
        for key in ('spread_p10','spread_p50','spread_p90'): row.pop(key,None)
    price['scenario_version']='request-marginal-da-rt-v2'
    price['scenario_seed']=inputs.get('scenario_seed',2026091503)
    if 'customer_load_forecast_mwh' in inputs:
        ids={c.customer_id for c in config_from(inputs).customers}
        fields=('customer_load_forecast_mwh','customer_load_p10_mwh','customer_load_p90_mwh')
        if any(set(inputs.get(k,{}))!=ids for k in fields): raise ValueError('逐客户预测ID不一致或缺失分位数')
        load['customer_rows']={}
        for cid in ids:
            mid,low,high=[vector(inputs[k][cid],k+cid,nonnegative=True) for k in fields]
            if any(not l<=m<=h for l,m,h in zip(low,mid,high)): raise ValueError('逐客户负荷分位数顺序错误')
            load['customer_rows'][cid]=[dict(load_p10=l,load_p50=m,load_p90=h) for l,m,h in zip(low,mid,high)]
        if any(abs(sum(inputs[fields[0]][cid][j] for cid in ids)-load['rows'][j]['p50_mwh'])>1e-6 for j in range(96)):
            raise ValueError('逐客户预测合计与聚合预测不一致')
    return park,load,price


def _default_fills(inputs,node,load,park):
    # Explicit mock predecessor portfolio, never run predecessor optimizers.
    products={'ANNUAL':(), 'MONTHLY':('ANNUAL',), 'TEN_DAY':('ANNUAL','MONTHLY')}.get(node,('ANNUAL','MONTHLY','TEN_DAY'))
    ratios={'ANNUAL':.8,'MONTHLY':.15,'TEN_DAY':.03}
    prices={'ANNUAL':inputs['annual_price'],'MONTHLY':inputs['monthly_price'],'TEN_DAY':inputs['ten_day_price']}
    out=[]
    for product in products:
        days=product_equivalent_days(product,park)
        curve={f'P{h+1:02d}':sum(load['rows'][j]['p50_mwh'] for j in (2*h,2*h+1))*ratios[product]*days for h in range(48)}
        out.append(ContractFill('DEFAULT-'+product,product,'DEFAULT','BUY',sum(curve.values()),prices[product],0.,curve,True,equivalent_delivery_days=days))
    return out


def _fills(inputs,upstream,node,load,park):
    raw=(upstream or {}).get('fills') if upstream else inputs.get('fills')
    if upstream and 'fills' in inputs and inputs['fills']!=upstream['fills']:
        raise ValueError('上游成交已锁定，不允许覆盖fills；可独立运行时手动输入')
    if raw is None: return _default_fills(inputs,node,load,park),'DEFAULT'
    out=[]
    for f in raw:
        fill=ContractFill(**f)
        errors=fill.validate()
        if errors: raise ValueError('; '.join(errors))
        if fill.product_class not in NODES[:5]: raise ValueError('合同产品必须为年/月/旬/D-3/D-2')
        vector(list(fill.delivery_curve.values()),'合同曲线',48)
        if set(fill.delivery_curve)!=set(f'P{i+1:02d}' for i in range(48)):
            raise ValueError('合同delivery_curve键必须为P01至P48')
        if any(not isfinite(v) for v in (fill.signed_quantity,fill.price,fill.fee,fill.equivalent_delivery_days)):
            raise ValueError('合同量、价、费和天数必须有限')
        out.append(fill)
    ids=[f.fill_id for f in out]
    if len(ids)!=len(set(ids)): raise ValueError('重复合同fill_id')
    if node in NODES[:5] and any(NODES.index(f.product_class)>=NODES.index(node) for f in out):
        raise ValueError('上游合同包含当前或未来节点；请从前一节点结果重算，防止重复成交')
    return out,'MANUAL' if 'fills' in inputs else 'UPSTREAM'


def _spot(inputs,fills,load,price,config):
    scenarios=reduced_joint_trajectories(load,price,inputs['scenario_count'],
        candidate_count=max(100,inputs['scenario_count']))
    # locked=False here is pre-gate-closure. The original pifa all-scenario
    # hard band would be infeasible with zero storage and stochastic load.
    return solve_l3_milp(load,price,[r['p50_mwh'] for r in load['rows']],inputs['risk_lambda'],
        inputs['physical_peak_mw'],[r['day_ahead_p50'] for r in price['rows']],
        [r['real_time_p50'] for r in price['rows']],1,False,
        locked_contract_curve=_daily_locked_contract_curve(fills),locked_contract_cost_yuan=_daily_contract_fixed_cost(fills),
        storage_power_override_mw=0.,joint_scenarios=scenarios,retail_revenue_builder=revenue_builder(config,inputs['package']))


def _manual_da(inputs,fills,load,price,config):
    scenarios=reduced_joint_trajectories(load,price,inputs['scenario_count'],
        candidate_count=max(100,inputs['scenario_count']))
    revenues=revenue_builder(config,inputs['package'])(scenarios)
    a=vector(inputs.get('declaration_mwh',[r['p50_mwh'] for r in load['rows']]),'declaration_mwh',nonnegative=True)
    curve=_daily_locked_contract_curve(fills)
    policy=period_policy(inputs)
    options=fixed_package_options(inputs,config,scenarios,policy)
    return dict(customer_storage_config=deepcopy(inputs['customer_storage']),portfolio_policy=policy,
        economic_audit=fixed_savings_audit(options,[s.probability for s in scenarios],policy),
        declaration_mwh=a,contract_curve_mwh=curve,contract_cost_yuan=_daily_contract_fixed_cost(fills),
        day_ahead_buy_mwh=[max(x-y,0) for x,y in zip(a,curve)],day_ahead_sell_mwh=[max(y-x,0) for x,y in zip(a,curve)],
        declaration_penalty_yuan=0.,load_snapshot=load,price_snapshot=price,
        scenarios=[dict(scenario_id=s.scenario_id,probability=s.probability,load_mwh=list(s.load_mwh),
            customer_load_mwh={c.customer_id:list(customer_load(s,config,ci)) for ci,c in enumerate(config.customers)},
            day_ahead_price=list(s.day_ahead_price),real_time_price=list(s.real_time_price),retail_revenue_yuan=revenues[i]) for i,s in enumerate(scenarios)])


def l1_context(inp, node='ANNUAL', fills=()):
    """One model input adapter for package selection and fixed-package L1."""
    config=config_from(inp)
    park,load,price=_data(inp,node)
    cfg=TraderConfig(annual_price_yuan_per_mwh=inp['annual_price'],
        monthly_price_yuan_per_mwh=inp['monthly_price'],ten_day_price_yuan_per_mwh=inp['ten_day_price'],**inp.get('trader',{}))
    cfg.validate()
    scope=product_equivalent_days(node,park)
    snapshot=aggregate_spot_snapshot(load)
    snapshot['spot_price_forecast']=[sum(price['rows'][j]['real_time_p50'] for j in (2*h,2*h+1))/2 for h in range(48)]
    # Daily trajectories go into the contract model, which scales by scope.
    # The billing trajectories use the very same loads/prices, scaled once.
    daily=list(reduced_joint_trajectories(load,price,inp['scenario_count'],
        candidate_count=max(100,inp['scenario_count'])))
    billing=[SimpleNamespace(**{**vars(s),'load_mwh':[q*scope for q in s.load_mwh],
        'customer_load_mwh':{cid:[q*scope for q in v] for cid,v in s.customer_load_mwh.items()}}) for s in daily]
    # Revenue and scenario energy cover this node's delivery horizon, so
    # inherited contract cost must cover exactly the same horizon (including fees).
    notional=_daily_contract_fixed_cost(fills)*scope
    quote=default_quotes(cfg)[node]
    quote=replace(quote,maximum_quantity=inp.get('l1_quote_limits',{}).get(node,quote.maximum_quantity))
    args=(node,snapshot,sum(f.signed_quantity/f.equivalent_delivery_days for f in fills)*scope,notional,
        sum(f.signed_quantity for f in fills if f.product_class=='ANNUAL'),
        sum(f.signed_quantity for f in fills if f.assessment_base_eligible),cfg,park,quote,
        sum(r['real_time_p50'] for r in price['rows'])/96,inp['risk_lambda'],fills)
    operating=[0. for s in billing]
    return dict(args=args,daily=daily,billing=billing,operating=operating,config=config,scope_days=scope)


def solve_l1_input(inp, node='ANNUAL', fills=(), retail_options=None, fixed_buy_curve=None):
    context=l1_context(inp,node,fills)
    options=inp.get('l1_solver',{})
    policy=period_policy(inp,context['scope_days'])
    if retail_options is None and inp.get('retail_linked',True):
        retail_options=fixed_package_options(inp,context['config'],context['billing'],policy)
    return solve_l1_contract_milp(*context['args'],joint_scenarios=context['daily'],
        scenario_revenue_yuan=revenue_builder(context['config'],inp['package'])(context['billing'])
            if retail_options is None and inp.get('retail_linked',True) else (),
        retail_options=retail_options,portfolio_policy=policy,fixed_buy_curve=fixed_buy_curve,
        scenario_operating_cost_yuan=context['operating'],
        time_limit_seconds=min(options.get('time_limit_seconds',60.),60.),mip_relative_gap=options.get('mip_relative_gap',.03))


def _refresh_scenarios(raw,inputs,upstream,load,price,config,node):
    if node not in ('STORAGE-DA','STORAGE-RT'): return
    fixed=inputs['fixed_until'] if node=='STORAGE-RT' else 0
    if fixed < (upstream or {}).get('solver',{}).get('fixed_until',0): raise ValueError('不能回退已执行点数')
    actual_load=vector(inputs.get('actual_load_mwh',[]),'actual_load_mwh',fixed,True)
    actual_price=vector(inputs.get('actual_rt_price',[]),'actual_rt_price',fixed)
    prevfixed=(upstream or {}).get('solver',{}).get('fixed_until',0)
    if fixed > prevfixed and not inputs.get('executed_storage'):
        raise ValueError('新增已执行点必须提供 executed_storage 的充电、放电和SOC实绩，不能把日前计划当成实绩')
    for key,arr in (('actual_load_mwh',actual_load),('actual_rt_price',actual_price)):
        if prevfixed and arr[:prevfixed] != upstream['inputs'][key][:prevfixed]: raise ValueError('已锁定实绩不可改写: '+key)
    known=[r['day_ahead_p50'] for r in price['rows']]
    if node=='STORAGE-RT' and (upstream or {}).get('solver',{}).get('day_ahead_price_known'):
        if any(s['day_ahead_price']!=known for s in raw['scenarios']): raise ValueError('已公布日前价格在日内不可改写')
    if fixed:
        ids={c.customer_id for c in config.customers}
        actual_clients=inputs.get('actual_customer_load_mwh',{})
        if set(actual_clients)!=ids: raise ValueError('日内须提供逐客户 actual_customer_load_mwh，不能用预测份额分摊实绩')
        for cid,values in actual_clients.items(): vector(values,'actual_customer_load_mwh '+cid,fixed,True)
        if any(abs(sum(actual_clients[cid][j] for cid in ids)-actual_load[j])>1e-6 for j in range(fixed)):
            raise ValueError('逐客户负荷实绩合计不一致')
        for cid in ids:
            if prevfixed and actual_clients[cid][:prevfixed]!=upstream['inputs']['actual_customer_load_mwh'][cid][:prevfixed]:
                raise ValueError('逐客户已锁定负荷实绩不可改写')
        for j in range(fixed):
            load['rows'][j].update(p10_mwh=actual_load[j],p50_mwh=actual_load[j],p90_mwh=actual_load[j],status='ACTUAL')
            price['rows'][j].update(real_time_p10=actual_price[j],real_time_p50=actual_price[j],real_time_p90=actual_price[j],real_time_observed=True)
            for cid in ids:
                q=actual_clients[cid][j]
                load['customer_rows'][cid][j]=dict(load_p10=q,load_p50=q,load_p90=q)
    fresh=_manual_da(inputs,[],load,price,config)
    for key in ('scenarios','load_snapshot','price_snapshot','customer_storage_config','portfolio_policy','economic_audit'):
        raw[key]=fresh[key]
    revenues=revenue_builder(config,inputs['package'])([SimpleNamespace(**s) for s in raw['scenarios']])
    for s,r in zip(raw['scenarios'],revenues): s['retail_revenue_yuan']=r


def run_layer(node, inputs=None, upstream=None, progress=None):
    """progress(percent, message); inputs/upstream are plain JSON objects."""
    started=perf_counter()
    node={'L3':'L3-A','L3-B':'STORAGE-DA','STORAGE':'STORAGE-DA'}.get(node,node)
    inp=_inputs(inputs,upstream); config=config_from(inp)
    def emit(p,msg):
        if progress: progress(p,msg)
    emit(5,'读取已确定套餐、预测及锁定上游状态')
    if node in ('L1','L2','CHAIN'):
        sequence={'L1':NODES[:3],'L2':NODES[3:5],'CHAIN':NODES[:7]}[node]
        result=upstream; nodes=[]
        for i,event in enumerate(sequence):
            current=dict(inp)
            if i:
                for key in ('fills','order_book'): current.pop(key,None)
            result=run_layer(event,current,result,lambda p,msg:emit((i+p/100)/len(sequence)*100,msg))
            nodes.append({k:result[k] for k in ('node','solver','elapsed_seconds')})
        result['layer_runs']=nodes; result['elapsed_seconds']=perf_counter()-started
        return result
    if node not in NODES: raise ValueError('未知节点: '+str(node))
    if upstream and node in NODES[:6] and NODES.index(upstream['node'])>=NODES.index(node):
        raise ValueError('上游节点必须先于当前节点，不能回退已完成交易')
    event=node if node in NODES[:5] else 'D-1'
    park,load,price=_data(inp,event)
    fills,source=_fills(inp,upstream,node,load,park)
    actions=list((upstream or {}).get('actions',[]))
    result=dict(schema_version=1,node=node,package=inp['package'],risk_lambda=inp['risk_lambda'],inputs=inp,
        input_source=source,phase='INTRADAY' if node=='STORAGE-RT' else 'EX_ANTE',
        package_locked=True,actions=actions)
    if node in NODES[:5]:
        cfg=TraderConfig(annual_price_yuan_per_mwh=inp['annual_price'],monthly_price_yuan_per_mwh=inp['monthly_price'],ten_day_price_yuan_per_mwh=inp['ten_day_price'],**inp.get('trader',{}))
        cfg.validate(); quote=default_quotes(cfg)[node]
        scope=product_equivalent_days(node,park); daily=sum(f.signed_quantity/f.equivalent_delivery_days for f in fills)
        notional=_daily_contract_fixed_cost(fills)*scope
        snapshot=aggregate_spot_snapshot(load)
        if 'rt_price_forecast' in inp:
            snapshot['spot_price_forecast']=[sum(price['rows'][j]['real_time_p50'] for j in (2*h,2*h+1))/2 for h in range(48)]
        args=(node,snapshot,daily*scope,notional,
            sum(f.signed_quantity for f in fills if f.product_class=='ANNUAL'),
            sum(f.signed_quantity for f in fills if f.assessment_base_eligible),cfg,park,quote,
            sum(r['real_time_p50'] for r in price['rows'])/96,inp['risk_lambda'])
        if node in NODES[:3]:
            emit(30,node+'：48点合同MILP、五档负荷、考核约束和套餐净损失CVaR')
            action=solve_l1_input(inp,node,fills)
            solver=dict(action.solver_details,milp_executed=True)
            solver['retail_linked']=bool(inp.get('retail_linked', True))
        else:
            emit(20,node+'：计算锁定合同下的日前MILP边际价值')
            valuation=_spot(inp,fills,load,price,config)['optimization']
            emit(75,node+'：逐笔订单价格阈值、仓位约束和部分成交')
            orders=inp.get('order_book') or build_rolling_order_book(node,price)
            sell_curve=[sum(max(-f.delivery_curve.get(f'P{h+1:02d}',0.),0.) for f in fills if f.product_class in ('D-3','D-2')) for h in range(48)]
            action=apply_l2_rolling_threshold(*args,sum(sell_curve),sell_curve,(),fills,orders,valuation)
            solver=dict(backend='RULE_ENGINE_WITH_MILP_VALUATION',status='EXECUTED',mip_gap=None,solve_seconds=valuation['solve_seconds'],
                variable_count=0,binary_count=0,constraint_count=0,milp_executed=False,valuation_solver=valuation)
        fills.extend(_fills_from_action(action,aggregate_spot_snapshot(load)))
        actions.append(action.to_dict()); result['solver']=solver
    elif node=='L3-A':
        emit(25,'L3：日前现货价格尚未公布；按价格区间场景联合优化申报曲线和储能计划')
        raw=_manual_da(inp,fills,load,price,config)
        raw['annual_contract_curve_mwh']=_daily_locked_contract_curve(fills,('ANNUAL',))
        declaration_settings={**inp.get('storage',{}),
            'annual_price_yuan_per_mwh':inp['annual_price'],
            'monthly_price_yuan_per_mwh':inp['monthly_price']}
        solved=dispatch(raw,inp['risk_lambda'],declaration_settings,0,optimize_declaration=True,physical_peak_mw=inp['physical_peak_mw'],day_ahead_price_known=False,progress=emit)
        result.update(solved)
        result['solver']=result.pop('optimization')
        result['declaration']=dict(status='CONDITIONAL_PLAN',rows=[])
    else:
        if upstream and upstream['node'] not in ('L3-A','STORAGE-DA','STORAGE-RT'):
            raise ValueError('储能上游必须为日前申报或上一储能输出')
        if node=='STORAGE-DA' and upstream and upstream['solver'].get('fixed_until',0):
            raise ValueError('进入日内后不可回退至日前重写执行历史')
        raw=deepcopy(upstream['raw_solution']) if upstream else _manual_da(inp,fills,load,price,config)
        raw['annual_contract_curve_mwh'] = _daily_locked_contract_curve(
            [f for f in fills if f.product_class == 'ANNUAL']
        )
        if upstream and 'declaration_mwh' in (inputs or {}): raise ValueError('申报已锁定，储能阶段不可修改')
        _refresh_scenarios(raw,inp,upstream,load,price,config,node)
        fixed_until=inp['fixed_until'] if node=='STORAGE-RT' else 0
        prior=upstream['raw_solution'] if upstream else None
        if fixed_until and inp.get('executed_storage'):
            prior={**(prior or {}),**inp['executed_storage']}
            for key in ('charge_mwh','discharge_mwh','soc_mwh'):
                values=inp['executed_storage'].get(key,[])
                if len(values) not in (fixed_until,96): raise ValueError(key+'须为已执行点数或96点')
                vector(values,key,len(values),True)
                oldfixed=(upstream or {}).get('solver',{}).get('fixed_until',0)
                if oldfixed and any(abs(values[j]-upstream['raw_solution'][key][j])>1e-8 for j in range(oldfixed)):
                    raise ValueError('已锁定储能实绩不可改写: '+key)
        storage_settings={**inp.get('storage',{}),'annual_price_yuan_per_mwh':inp['annual_price'],
            'monthly_price_yuan_per_mwh':inp['monthly_price']}
        solved=dispatch(raw,inp['risk_lambda'],storage_settings,fixed_until,
            prior=prior,progress=emit,physical_peak_mw=inp.get('physical_peak_mw'),
            day_ahead_price_known=bool(inp.get('day_ahead_price_known')))
        result.update(solved)
        result['solver']=result.pop('optimization')
        if upstream: result['declaration']=upstream.get('declaration')
    if 'raw_solution' in result:
        if result.get('declaration') is not None:
            result['declaration']=dict(status='CONDITIONAL_PLAN',rows=[dict(period=j+1,time=f'{j//4:02d}:{j%4*15:02d}',
                declared_mwh=result['raw_solution']['declaration_mwh'][j],
                **{k:r[k] for k in ('p10_mwh','p50_mwh','p90_mwh')}) for j,r in enumerate(result['raw_solution']['load_snapshot']['rows'])])
        result['raw_solution']['annual_contract_curve_mwh']=_daily_locked_contract_curve(fills,('ANNUAL',))
        # Persist a forecast-only view; mock truth is not information available
        # before delivery and must never appear as actual input to a plan.
        result['raw_solution']['load_snapshot']={'rows':[
            {k:r[k] for k in ('p10_mwh','p50_mwh','p90_mwh')} for r in result['raw_solution']['load_snapshot']['rows']]}
    result.update(fills=[asdict(f) for f in fills],contract_curve_mwh=_daily_locked_contract_curve(fills),
        contract_cost_yuan=_daily_contract_fixed_cost(fills),elapsed_seconds=perf_counter()-started)
    result['solver']['solver_backend']=result['solver']['backend']
    result['status']=result['solver']['status']
    # Keep only a small audit trail; raw scenario arrays are in this result.
    result['upstream_runs']=(upstream or {}).get('upstream_runs',[])+([dict(node=upstream['node'],solver=upstream['solver'])] if upstream else [])
    emit(100,node+'：完成；'+result['status'])
    return result


def run_l1(inputs=None,upstream=None,progress=None): return run_layer('L1',inputs,upstream,progress)
def run_l2(inputs=None,upstream=None,progress=None): return run_layer('L2',inputs,upstream,progress)
def run_l3_day_ahead(inputs=None,upstream=None,progress=None): return run_layer('L3-A',inputs,upstream,progress)
def run_storage_day_ahead(inputs=None,upstream=None,progress=None): return run_layer('STORAGE-DA',inputs,upstream,progress)
def run_storage_realtime(inputs=None,upstream=None,progress=None): return run_layer('STORAGE-RT',inputs,upstream,progress)


def write_result(result,path):
    path=Path(path).resolve(); path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(json.dumps(result,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')
    return path


def main():
    parser=argparse.ArgumentParser(description='独立运行L1/L2/L3-A/储能及每个交易节点')
    parser.add_argument('node',choices=NODES+('L1','L2','CHAIN'))
    parser.add_argument('--inputs',type=Path,help='手动输入JSON（省略字段使用默认值）')
    parser.add_argument('--upstream',type=Path,help='上游节点结果JSON')
    parser.add_argument('--output',type=Path)
    args=parser.parse_args()
    inp=json.loads(args.inputs.read_text()) if args.inputs else {}
    upstream=json.loads(args.upstream.read_text()) if args.upstream else None
    from .node_requests import RequestStore, FOLDERS
    store=RequestStore()
    sequence={'L1':NODES[:3],'L2':NODES[3:5],'CHAIN':NODES[:7]}.get(args.node,(args.node,))
    for i,node in enumerate(sequence):
        current=dict(inp)
        if i:
            for key in ('fills','order_book'):current.pop(key,None)
        record=store.create(dict(node=node,inputs=current),dict(source='INDEPENDENT_CLI'))
        saved=store.execute(FOLDERS[node],record['id'],upstream=upstream,allow_standalone=True,
            progress=lambda p,m:print(f'{p:5.1f}% {m}',flush=True))
        result=saved['result'];upstream=result
    path=Path(saved['record']['path'])/'response.json'
    if args.output:write_result(result,args.output)
    print('结果:',path.as_uri())
    print(json.dumps(result['solver'],ensure_ascii=False,indent=2))

if __name__=='__main__': main()
