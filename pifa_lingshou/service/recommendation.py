"""Choose one customer package portfolio using the annual L1 MILP."""
from copy import deepcopy
from math import isfinite
from statistics import mean
from time import perf_counter

from ..accounting.retail import summarize_distribution
from .case_data import layer_input
from .full_model import retail_prices
from ..problem_solver.milp import MAX_SOLVER_WALL_TIME_SECONDS, MilpSolveError
from .layer_runner import l1_context, solve_l1_input
from .economic_policy import period_policy
from .full_model import customer_load


def signing_input(data):
    """Use only signing-period information; later node forecasts are not truth."""
    inp=layer_input(data,'ANNUAL',{})
    days=data['signing_forecast']['days']
    ids=[c['terms']['customer_id'] for c in data['customers']]
    loads={c['terms']['customer_id']:[q*mean(day['load_factors'][c['terms']['customer_id']] for day in days)
        for q in c['load_profile_mwh']] for c in data['customers']}
    total=[sum(loads[cid][j] for cid in ids) for j in range(96)]
    inp.update(load_forecast_mwh=total,
        customer_load_forecast_mwh=loads,
        customer_load_p10_mwh={c['terms']['customer_id']:[q*(1-c['forecast_uncertainty']) for q in loads[c['terms']['customer_id']]] for c in data['customers']},
        customer_load_p90_mwh={c['terms']['customer_id']:[q*(1+c['forecast_uncertainty']) for q in loads[c['terms']['customer_id']]] for c in data['customers']},
        load_p10_mwh=[sum(loads[c['terms']['customer_id']][j]*(1-c['forecast_uncertainty']) for c in data['customers']) for j in range(96)],
        load_p90_mwh=[sum(loads[c['terms']['customer_id']][j]*(1+c['forecast_uncertainty']) for c in data['customers']) for j in range(96)],
        customer_shares=[[loads[cid][j]/total[j] if total[j] else 1/len(ids) for j in range(96)] for cid in ids],
        as_of=data['signing_forecast']['issued_at'],
        physical_peak_mw=max(20.,max(total)*4*1.3),
        portfolio_policy=data['recommendation_policy'])
    # The legacy L1 trader cap is a per-node adjustment cap. A signing
    # portfolio must cover the full annual aggregate, including 100 clients.
    cap=max(8000.,sum(inp['load_p90_mwh'])*inp['delivery_days']['ANNUAL']*1.25)
    inp['trader']={'maximum_node_adjustment_mwh':cap}
    inp['l1_quote_limits']={'ANNUAL':cap}
    market=data['market']
    for key in ('annual_price','monthly_price','ten_day_price'):
        inp[key]=market[key]
    factor=mean(day['price_factor'] for day in days)
    for key,source in (('day_ahead_price','day_ahead_price'),('rt_price','real_time_price')):
        values=[p*factor for p in market[source]]
        inp[key+'_forecast']=values
        inp[key+'_p10']=[p-abs(p)*market['price_uncertainty_ratio'] for p in values]
        inp[key+'_p90']=[p+abs(p)*market['price_uncertainty_ratio'] for p in values]
    policy=data['recommendation_policy']
    inp['l1_solver']=dict(time_limit_seconds=min(policy.get('time_limit_seconds',60.),MAX_SOLVER_WALL_TIME_SECONDS),
        mip_relative_gap=policy.get('mip_relative_gap',.03))
    return inp


def package_options(data, inp):
    context=l1_context(inp)
    scenarios=context['billing']; config=context['config']
    probs=[s.probability for s in scenarios]
    prices={p:[retail_prices(config,p,s.real_time_price) for s in scenarios] for p in ('F','L','S')}
    options={}; customers=[]
    for ci,c in enumerate(data['customers']):
        cid=c['terms']['customer_id']
        loads=[customer_load(s,config,ci) for s in scenarios]
        energies=[sum(v) for v in loads]
        baseline=[sum(q*c['tou_price'][j] for j,q in enumerate(v)) for v in loads]
        candidates=[]; options[cid]={}
        for p in ('F','L','S'):
            bills=[sum(q*prices[p][si][cid][j] for j,q in enumerate(v)) for si,v in enumerate(loads)]
            savings=[b-r for b,r in zip(baseline,bills)]
            stats=summarize_distribution(savings,probs)
            eligible=stats['p10']+1e-6>=period_policy(inp,context['scope_days'])['minimum_customer_saving_yuan']
            options[cid][p]=dict(eligible=eligible,bills=bills,savings=savings,baseline=baseline,energies=energies)
            candidates.append(dict(package=p,eligible=eligible,expected_revenue_yuan=sum(v*w for v,w in zip(bills,probs)),
                expected_customer_saving_yuan=stats['mean'],saving_p10_yuan=stats['p10'],saving_p90_yuan=stats['p90']))
        customers.append(dict(customer_id=cid,recommended_package=None,candidates=candidates))
    return context,options,customers


def quality(solver,target):
    gap=solver.get('mip_gap')
    return (gap is not None and isfinite(gap) and gap<=target+1e-9
            and solver.get('objective_reconciliation_yuan',0.)<=1e-3)


def portfolio_summary(context, options, packages, net_losses):
    probs=[s.probability for s in context['billing']]
    revenue=[sum(options[cid][p]['bills'][i] for cid,p in packages.items()) for i in range(len(probs))]
    saving=[sum(options[cid][p]['savings'][i] for cid,p in packages.items()) for i in range(len(probs))]
    energy=[sum(options[cid][p]['energies'][i] for cid,p in packages.items()) for i in range(len(probs))]
    profit=[-v for v in net_losses]
    profits=summarize_distribution(profit,probs); savings=summarize_distribution(saving,probs)
    losses=summarize_distribution([max(0.,v) for v in net_losses],probs)
    costs=[l+r for l,r in zip(net_losses,revenue)]
    return dict(packages=packages,expected_cost_yuan=sum(v*w for v,w in zip(costs,probs)),
        expected_revenue_yuan=sum(v*w for v,w in zip(revenue,probs)),expected_profit_yuan=profits['mean'],
        profit_p10_yuan=profits['p10'],profit_p90_yuan=profits['p90'],profit_loss_cvar_yuan=losses['cvar'],
        expected_customer_saving_yuan=savings['mean'],saving_p10_yuan=savings['p10'],saving_p90_yuan=savings['p90'],
        expected_spread_yuan_per_mwh=sum(w*(p/e if e else 0.) for p,e,w in zip(profit,energy,probs)),
        scenarios=[dict(scenario_id=s.scenario_id,probability=w,revenue_yuan=r,procurement_cost_yuan=c,profit_yuan=p,saving_yuan=sv)
            for s,w,r,c,p,sv in zip(context['billing'],probs,revenue,costs,profit,saving)])


def recommend(data, progress=None):
    started=perf_counter(); emit=progress or (lambda *_:None)
    inp=signing_input(data); count=len(data['customers'])
    target=inp['l1_solver']['mip_relative_gap']; budget=inp['l1_solver']['time_limit_seconds']
    emit(10,f'为 {count} 位客户建立独立 F/L/S 决策，完整组合空间 3^{count}；年度 L1 共用模型')
    context,options,customers=package_options(data,inp)
    result=dict(period={k:data['signing_forecast'][k] for k in ('issued_at','start_date','end_date')},
        day_count=len(data['signing_forecast']['days']),annual_scope_days=context['scope_days'],
        annual_inputs=inp,customers=customers,policy=deepcopy(data['recommendation_policy']),
        risk_lambda=inp['risk_lambda'],company_curve=None,recommended_portfolio=None,candidate_portfolio=None,
        constraints_satisfied=False,quality_certified=False,
        method='逐客户套餐选择 + 年度 L1 联合 MILP',
        formula='每客户恰选一种套餐；在客户 P10 节省、组合期望利润及损失 CVaR 约束下，最小化期望净损失与 CVaR 的加权和',
        cost_basis='与年度 L1 共用采购、48点考核、补救成本及净损失 CVaR；固定套餐变量即得到已签套餐年度模型',
        risk_note='使用签约时可用预测的平均代表日，按年度交割天数缩放；不是逐日全年情景树。gap 只衡量当前模型的全局求解误差。',
        combination_space=dict(customer_count=count,package_count=3,theoretical_count=str(3**count),
            coverage='完整逐客户笛卡尔积；仅按明确的客户经济约束排除不可行套餐',
            search_method='联合 MILP 分支定界，不逐个枚举，不按客户数量切换启发式'),
        performance=dict(time_limit_seconds=budget,target_mip_gap=target,
            explanation='3^N 是理论套餐组合数；只增加 3N 个选择变量，不创建 3^N 份模型。耗时取决于分支定界、风险约束、可行性及最优界证明；时限是上限，达到 gap 可提前结束。'))
    try:
        emit(40,f'求解完整组合年度 L1：全局 gap 目标 {target:.1%}，最多 {budget:g} 秒；正在搜索可行组合并收紧最优界')
        action=solve_l1_input(inp,retail_options=options)
    except MilpSolveError as exc:
        status=str(exc).split(':',1)[0]
        if status not in ('INFEASIBLE','LIMIT_REACHED'): raise
        result['status']='INFEASIBLE' if status=='INFEASIBLE' else 'NO_FEASIBLE_SOLUTION'
        result['solver']=dict(status=status,message=str(exc),mip_gap=None)
        for c in customers:c['reason']='没有通过年度联合模型的可行组合；请调整报价或约束后重算'
    else:
        solver=action.solver_details; selected=solver['portfolio']
        summary=portfolio_summary(context,options,selected['packages'],selected['net_losses_yuan'])
        summary.update(risk_adjusted_net_loss_yuan=selected['objective_yuan'],constraints_satisfied=True)
        certified=quality(solver,target)
        result.update(solver=solver,candidate_portfolio=summary,company_curve=summary,
            constraints_satisfied=True,quality_certified=certified,status='CERTIFIED' if certified else 'GAP_NOT_MET',
            recommended_portfolio=summary if certified else None,annual_purchase_curve_mwh=selected['buy_curve_mwh'])
        for c in customers:
            c['recommended_package']=selected['packages'][c['customer_id']] if certified else None
            c['reason']='来自通过全局 gap 精度要求的综合组合' if certified else '已有可行候选，但尚未达到全局 3% 精度要求'
        result['performance'].update(solve_seconds=solver['solve_seconds'],node_count=solver['node_count'],
            incumbent_objective_yuan=selected['objective_yuan'],best_bound_yuan=solver['dual_bound'],
            achieved_mip_gap=solver['mip_gap'],variable_count=solver['variable_count'],
            binary_count=solver['binary_count'],constraint_count=solver['constraint_count'])
    result['performance']['end_to_end_seconds']=perf_counter()-started
    emit(100,'套餐组合计算完成：'+result['status'])
    return result


def validate_selection(data, recommendation, packages):
    inp=deepcopy(recommendation['annual_inputs'])
    context,options,_=package_options(data,inp)
    if any(cid not in options or p not in options[cid] or not options[cid][p]['eligible'] for cid,p in packages.items()):
        raise ValueError('所选套餐不满足客户 P10 最低节省，请调整选择')
    inp['customer_packages']=dict(packages)
    action=solve_l1_input(inp)
    if not quality(action.solver_details,inp['l1_solver']['mip_relative_gap']):
        raise ValueError('所选组合的年度采购模型尚未达到 3% 精度，请延长时限后重算')
    summary=portfolio_summary(context,options,packages,action.solver_details['net_losses_yuan'])
    return dict(packages=dict(packages),solver=action.solver_details,portfolio=summary,
        basis='人工选择；固定套餐的年度 L1 可行性与精度，不是重新证明套餐组合全局最优')
