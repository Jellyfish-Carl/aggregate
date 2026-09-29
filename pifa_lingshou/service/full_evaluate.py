"""Evaluate fixed customer packages through independent L1/L2/L3-A/L3-B layers."""
from __future__ import annotations
from dataclasses import asdict
import json
from pathlib import Path
from ..inputs.full_model import FullModelInput
from ..accounting.full_accounting import account
from .layer_runner import run_layer, config_from, write_result
from .storage_validation import compare_storage_layer
from ..problem_solver.storage_milp import DEFAULTS


def declaration_for(result):
    raw=result['raw_solution']
    original=result.get('declaration') or {}
    return dict(**{k:v for k,v in original.items() if k!='rows'},rows=[dict(
        **({k:v for k,v in original['rows'][j].items() if k not in ('period','time','declared_mwh','p10_mwh','p50_mwh','p90_mwh')} if original else {}),
        period=j+1,time=f'{j//4:02d}:{j%4*15:02d}',declared_mwh=raw['declaration_mwh'][j],
        **{k:row[k] for k in ('p10_mwh','p50_mwh','p90_mwh')}
    ) for j,row in enumerate(raw['load_snapshot']['rows'])])


def report_defaults(config, inputs=None):
    inputs=inputs or {}
    settings={**DEFAULTS,**inputs.get('storage',{})}
    return dict(**{**asdict(config),**inputs,
        'event':'REAL_TIME' if inputs.get('fixed_until',0) else 'D-1',
        'physical_peak_mw':inputs.get('physical_peak_mw',20),
        'time_grid':{'spot_periods':96,'contract_periods':48,'interval_minutes':15},
        'cvar_alpha':.95,'coverage':{'annual_minimum':settings['annual_coverage'],
            'overall_minimum':settings['overall_lower_ratio'],'overall_maximum':settings['overall_upper_ratio']},
        'storage':settings,'transaction_friction_yuan_per_mwh':settings['friction_yuan_per_mwh'],
        'declaration_slack_penalty_yuan_per_mwh':10000,
        'deviation':{'ratio_limit':settings['deviation_ratio'],'rule':'软考核计价；可启用hard_deviation对全部场景未来点施加硬约束'},
        'source':'项目内预测模型或用户输入；储能代表场景优化后按全场景概率评估；演示数据默认为MOCK'})


def _declaration_view(layer_result):
    raw=layer_result['raw_solution']; declaration=declaration_for(layer_result)
    fills=layer_result['fills']; n=96
    settings={**DEFAULTS,**raw.get('storage_settings',{})}
    ratio=settings['deviation_ratio']; annual_min=settings['annual_coverage']
    overall_min=settings['overall_lower_ratio']; overall_max=settings['overall_upper_ratio']
    products=('ANNUAL','MONTHLY','TEN_DAY','D-3','D-2')
    curves={p:[0.]*n for p in products}
    for fill in fills:
        product=fill['product_class']
        if product not in curves: continue
        days=float(fill['equivalent_delivery_days'])
        for h in range(48):
            q=float(fill['delivery_curve'].get('P%02d'%(h+1),0.))/days
            curves[product][2*h]+=q/2; curves[product][2*h+1]+=q/2
    s=next((x for x in raw['scenarios'] if x['scenario_id']=='L50_DA50_SP50'),raw['scenarios'][0])
    load_rows=raw['load_snapshot']['rows']; rows=[]
    for j in range(n):
        d=declaration['rows'][j]; q=s['load_mwh'][j]
        annual=sum(curves['ANNUAL'][k] for k in (j//2*2,j//2*2+1))
        overall=sum(sum(curves[p][k] for p in products) for k in (j//2*2,j//2*2+1))
        meter=sum(s['load_mwh'][k] for k in (j//2*2,j//2*2+1))
        if raw.get('storage_settings',{}).get('assessment_basis')=='grid_net_load':
            meter+=sum(raw['charge_mwh'][k]-raw['discharge_mwh'][k] for k in (j//2*2,j//2*2+1))
        ar=annual/max(meter,1e-9); rr=overall/max(meter,1e-9)
        rb=s['rt_buy_mwh'][j]; rs=s['rt_sell_mwh'][j]
        charge=raw['charge_mwh'][j]; discharge=raw['discharge_mwh'][j]
        long=sum(curves[p][j] for p in products)
        daynet=raw['day_ahead_buy_mwh'][j]-raw['day_ahead_sell_mwh'][j]
        rows.append(dict(period=j+1,time=d.get('time',f'{j//4:02d}:{j%4*15:02d}'),
            annual_mwh=curves['ANNUAL'][j],monthly_mwh=curves['MONTHLY'][j],
            ten_day_mwh=curves['TEN_DAY'][j],d3_mwh=curves['D-3'][j],d2_mwh=curves['D-2'][j],
            long_term_mwh=long,day_ahead_buy_mwh=raw['day_ahead_buy_mwh'][j],
            day_ahead_sell_mwh=raw['day_ahead_sell_mwh'][j],day_ahead_spot_mwh=daynet,
            declaration_mwh=raw['declaration_mwh'][j],real_time_buy_mwh=rb,real_time_sell_mwh=rs,
            real_time_spot_mwh=rb-rs,spot_exposure_mwh=q-long,
            spot_exposure_buy_mwh=max(q-long,0),spot_exposure_sell_mwh=max(long-q,0),
            spot_deviation_abs_mwh=abs(rb-rs),spot_deviation_limit_mwh=ratio*q,
            spot_deviation_ratio=abs(rb-rs)/max(q,1e-9),spot_deviation_compliant=abs(rb-rs)<=ratio*q+1e-7,
            p10_mwh=d['p10_mwh'],forecast_load_mwh=d['p50_mwh'],p90_mwh=d['p90_mwh'],
            band_slack_mwh=d.get('band_slack_lower_mwh',0)+d.get('band_slack_upper_mwh',0),
            actual_load_mwh=q,net_grid_load_after_storage_mwh=q+charge-discharge,
            real_time_after_storage_mwh=q+charge-discharge-raw['declaration_mwh'][j],
            reconciliation_mwh=long+daynet+rb-rs,
            annual_assessment_ratio=ar,annual_assessment_compliant=ar>=annual_min-1e-6,
            overall_assessment_ratio=rr,overall_assessment_compliant=overall_min-1e-6<=rr<=overall_max+1e-6))
    totals={k:sum(row[k] for row in rows) for k in (
        'long_term_mwh','day_ahead_buy_mwh','day_ahead_sell_mwh','day_ahead_spot_mwh',
        'declaration_mwh','real_time_buy_mwh','real_time_sell_mwh','real_time_spot_mwh',
        'spot_exposure_mwh','spot_exposure_buy_mwh','spot_exposure_sell_mwh',
        'forecast_load_mwh','actual_load_mwh','net_grid_load_after_storage_mwh')}
    totals['spot_deviation_breach_periods']=sum(not r['spot_deviation_compliant'] for r in rows)
    totals['max_spot_deviation_ratio']=max(r['spot_deviation_ratio'] for r in rows)
    assessment=dict(status='PASS' if all(r['annual_assessment_compliant'] and r['overall_assessment_compliant'] for r in rows) else 'BLOCKED',
        annual_min_ratio=min(r['annual_assessment_ratio'] for r in rows),
        overall_min_ratio=min(r['overall_assessment_ratio'] for r in rows),
        overall_max_ratio=max(r['overall_assessment_ratio'] for r in rows),
        long_term_assessment_products=list(products),spot_deviation_limit_ratio=ratio)
    return dict(rows=rows,totals=totals,assessment_guard=assessment,
        status=declaration.get('status','CONDITIONAL_PLAN'),
        l3_objective={key:layer_result['solver'].get(key) for key in ('milp_executed','objective_yuan','mip_gap','status')})


def _adapt_layer_result(layer_result):
    raw=layer_result['raw_solution']; solver=layer_result['solver']
    declaration=declaration_for(layer_result); actions=layer_result['actions']
    lineage=layer_result.get('upstream_runs',[])
    nodes={x['node'] for x in lineage}|{layer_result['node']}
    l1=next((item['solver'] for item in lineage if item.get('node')=='TEN_DAY'),{})
    l2=next((item['solver'] for item in lineage if item.get('node')=='D-2'),{})
    l3a=next((item['solver'] for item in lineage if item.get('node')=='L3-A'),{})
    return dict(wholesale_raw_solution=raw,l3_objective=solver,
        portfolio={'optimized':{'fills':layer_result['fills'],'actions':actions}},
        declaration=declaration,declaration_breakdown=_declaration_view(layer_result),
        price_forecast={'rows':raw['price_snapshot']['rows']},
        meta={'selected_event':'REAL_TIME' if layer_result['node']=='STORAGE-RT' else 'D-1','source_type':'MOCK','engine':solver['backend'],
            'model_structure':'INDEPENDENT_L1_L2_L3A_STORAGE_MILP',
            'milp':{'executed':{'TEN_DAY','D-2','L3-A','STORAGE-DA'}<=nodes,'l1_executed':'TEN_DAY' in nodes,'l2_rule_executed':'D-2' in nodes,
                'l3_executed':'L3-A' in nodes,'l1_backend':l1.get('backend'),
                'l1_statuses':[x['solver'].get('status') for x in lineage if x.get('node') in ('ANNUAL','MONTHLY','TEN_DAY')],
                'l1_max_mip_gap':max([x['solver'].get('mip_gap') or 0 for x in lineage if x.get('node') in ('ANNUAL','MONTHLY','TEN_DAY')]+[0]),
                'l2_solver':'RULE_ENGINE','l2_status':l2.get('status'),
                'l3a_backend':l3a.get('backend'),'l3a_status':l3a.get('status'),
                'l3_backend':solver.get('backend'),'l3_status':solver.get('status'),
                'l3_mip_gap':solver.get('mip_gap'),'l3_solve_seconds':solver.get('solve_seconds'),
                'diagnostic_mode':False}})


def account_layer(chain):
    inputs=chain['inputs']
    result=account(config_from(inputs),chain['package'],chain['risk_lambda'],_adapt_layer_result(chain))
    solvers=chain.get('upstream_runs',[])+[dict(node=chain['node'],solver=chain['solver'])]
    result['layer_solvers']=solvers
    result['chain_status']='LIMIT_REACHED' if any(x['solver'].get('status')=='LIMIT_REACHED' or x['solver'].get('valuation_solver',{}).get('status')=='LIMIT_REACHED' for x in solvers) else 'OPTIMAL'
    return result


def evaluate_full(config=None, packages=('F','L','S'), progress=None, layer_inputs=None, output_dir=None):
    config=config or FullModelInput()
    config.validate()
    if config.event!='D-1':
        raise ValueError('默认报告是事前D-1评估；日内请运行layer_runner STORAGE-RT并提供上游及实绩。原pifa其他事件入口保持可用。')
    if not packages or any(package not in ('F','L','S') for package in packages):
        raise ValueError('套餐必须为非空F/L/S集合')
    results=[]
    catalog=[]
    for package in packages:
        for risk in config.risk_lambdas:
            if progress: progress(f'求解 {package} / λ={risk:g}：独立 L1 → L2 → L3-A 日前申报 → L3-B 储能 MILP')
            inputs={**asdict(config),**(layer_inputs or {})}
            inputs.update(package=package,risk_lambda=float(risk))
            chain=None
            from .layer_runner import NODES
            for node in NODES[:7]:
                chain=run_layer(node,inputs,chain,lambda pct,msg: progress(f'{package}/λ={risk:g} {pct:.0f}% {msg}') if progress else None)
                if output_dir:
                    path=write_result(chain,Path(output_dir)/f'{package}_{risk:g}'/(node+'.json'))
                    rel=path.relative_to(Path(output_dir).resolve().parent).as_posix()
                    catalog.append(dict(id=rel,path=rel,node=node,package=package,risk_lambda=risk,status=chain['status']))
            evaluated=account_layer(chain)
            evaluated['storage_validation']=compare_storage_layer(chain)
            results.append(evaluated)
    if output_dir:
        (Path(output_dir)/'index.json').write_text(json.dumps(catalog,ensure_ascii=False,indent=2),encoding='utf-8')
    return dict(model='independent pifa L1/L2/L3-A declaration/L3-B storage chain + retail risk objective',
        results=results,packages=list(packages),risk_lambdas=list(config.risk_lambdas),cvar_alpha=.95,
        input_defaults=report_defaults(config,layer_inputs),
        allocation_rule='每场景每时段：买电均价=批发净成本/客户总负荷；卖电均价=零售收入/客户总负荷；客户成本/收入=其分时电量×对应均价。',
        remaining_issues=[
            'L1、L2、L3-A和L3-B可分别运行并保存结果；套餐在L1之前锁定，独立运行时从上一层结果继承。',
            'L2的逐笔撮合是价格阈值规则，边际价值使用L3现货MILP计算，因此L2本身不报告MILP求解状态。',
            '日前申报与储能已分开求解；储能在日前锁定申报后优化日内套利、实时偏差罚款、中长期覆盖罚款和退化成本。',
            '长期考核计量口径尚未在当前业务文档中明确。当前默认按储能后的并网净负荷计罚，也支持 customer_load 口径；需按结算规则确认。',
            '中长期考核费用是目标日按48点折算的预测费用，复用pifa有利价差回收公式；尚未提供月累计分时实绩，不能当作正式月结罚款。',
            '20MW沿用原pifa作为申报上限；全场景并网潮流上限须另按并网协议提供storage.grid_import_limit_mwh和grid_export_limit_mwh，默认未设置。',
            '储能默认用8条代表场景及聚合概率快速优化，报告在全部原始场景上评估同一策略；该gap只证明代表场景MILP精度。将storage.optimization_scenario_count设为64可全量优化。',
            'L1仍使用五档负荷及补救成本代理；分层顺序决策的λ曲线不构成全时域联合最优前沿，也不保证期望收益单调。',
            '64条Mock场景P10-P90尚未用历史覆盖率校准；当前客户分时电量由聚合负荷比例拆分，不是客户真实计量。',
        ])
