"""One period-aware profit/savings contract for signing and fixed packages."""
from math import isfinite
from types import SimpleNamespace
from ..accounting.retail import summarize_distribution
from .full_model import customer_load, retail_prices

DEFAULT_POLICY = dict(minimum_total_profit_yuan=0., minimum_customer_saving_yuan=0.,
    minimum_total_customer_saving_yuan=0., threshold_basis='per_delivery_day')


def period_policy(inputs, days=1.):
    policy = {**DEFAULT_POLICY, **(inputs.get('portfolio_policy') or {})}
    basis = policy['threshold_basis']
    if basis not in ('per_delivery_day','current_horizon'):
        raise ValueError('threshold_basis须为per_delivery_day或current_horizon')
    factor = days if basis == 'per_delivery_day' else 1.
    for k in ('minimum_total_profit_yuan','minimum_customer_saving_yuan','minimum_total_customer_saving_yuan'):
        if not isinstance(policy[k],(int,float)) or not isfinite(policy[k]) or policy[k]<0:
            raise ValueError(k+'须为非负有限数')
        policy[k] *= factor
    policy.update(threshold_basis='current_horizon', scope_days=days)
    return policy


def fixed_package_options(inputs, config, scenarios, policy):
    probs = [s.probability for s in scenarios]
    baseline = inputs['customer_baseline_price']
    ids = {c.customer_id for c in config.customers}
    if set(baseline)!=ids: raise ValueError('分时基准电价必须逐客户提供')
    if any(len(v)!=96 or any(not isfinite(p) for p in v) for v in baseline.values()):
        raise ValueError('分时基准电价必须为96点有限数值')
    prices = [retail_prices(config,inputs['package'],s.real_time_price) for s in scenarios]
    options = {}
    for i,c in enumerate(config.customers):
        cid=c.customer_id
        loads=[customer_load(s,config,i) for s in scenarios]
        bills=[sum(q*p for q,p in zip(v,prices[si][cid])) for si,v in enumerate(loads)]
        bases=[sum(q*p for q,p in zip(v,baseline[cid])) for v in loads]
        savings=[b-r for b,r in zip(bases,bills)]
        p=config.customer_packages.get(cid,inputs['package'])
        options[cid]={p:dict(bills=bills,baseline=bases,savings=savings,
            energies=[sum(v) for v in loads],
            eligible=summarize_distribution(savings,probs)['p10']+1e-6>=policy['minimum_customer_saving_yuan'])}
    return options


def fixed_savings_audit(options, probs, policy):
    customers = {cid:summarize_distribution(next(iter(v.values()))['savings'],probs)
                 for cid,v in options.items()}
    total = [sum(next(iter(v.values()))['savings'][si] for v in options.values()) for si in range(len(probs))]
    stats = summarize_distribution(total,probs)
    satisfied = (all(v['p10']+1e-6>=policy['minimum_customer_saving_yuan'] for v in customers.values())
                 and stats['mean']+1e-6>=policy['minimum_total_customer_saving_yuan'])
    return dict(customers=customers,total_savings=stats,savings_satisfied=satisfied,
        policy=policy,individual_statistic='P10',total_statistic='mean')
