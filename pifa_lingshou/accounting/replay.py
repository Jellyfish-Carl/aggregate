"""Realized settlement with executed storage fixed (no hindsight optimization)."""
from copy import deepcopy
from ..inputs.case_data import curve
from ..inputs.full_model import retail_prices
from ..problem_solver.storage_milp import dispatch


def settle(data,state,last,actual):
    from ..service.layer_runner import config_from
    from ..service.full_evaluate import account_layer
    ids=[c['terms']['customer_id'] for c in data['customers']]
    if actual.get('target_date')!=data['target_date'] or actual.get('source') not in ('MOCK_REPLAY','METERED'):
        raise ValueError('须明确标的日和实绩来源MOCK_REPLAY/METERED')
    if set(actual['customer_load_mwh'])!=set(ids):raise ValueError('客户计量集合不一致')
    for cid in ids:curve(actual['customer_load_mwh'][cid],cid,True)
    for key in ('day_ahead_price','real_time_price','charge_mwh','discharge_mwh','soc_mwh'):
        curve(actual[key],key,key not in ('day_ahead_price','real_time_price'))
    aggregate=[sum(actual['customer_load_mwh'][cid][j] for cid in ids) for j in range(96)]
    for j in range(state.get('fixed_until',0)):
        if abs(aggregate[j]-last['inputs']['actual_load_mwh'][j])>1e-7 or abs(actual['real_time_price'][j]-last['inputs']['actual_rt_price'][j])>1e-7:
            raise ValueError('结算不能改写日内锁定的负荷/价格实绩')
        if any(abs(actual[key][j]-last['raw_solution'][key][j])>1e-7 for key in ('charge_mwh','discharge_mwh','soc_mwh')):
            raise ValueError('结算不能改写日内锁定的储能实绩')
    layer=deepcopy(last);raw=layer['raw_solution']
    layer['inputs']['customer_shares']=[[actual['customer_load_mwh'][cid][j]/aggregate[j] if aggregate[j] else 1/len(ids) for j in range(96)] for cid in ids]
    config=config_from(layer['inputs']);prices=retail_prices(config,'F',actual['real_time_price'])
    revenue=sum(actual['customer_load_mwh'][cid][j]*prices[cid][j] for cid in ids for j in range(96))
    raw['scenarios']=[dict(scenario_id='REALIZED',probability=1.,load_mwh=aggregate,
        day_ahead_price=actual['day_ahead_price'],real_time_price=actual['real_time_price'],retail_revenue_yuan=revenue)]
    result=dispatch(raw,0.,raw.get('storage_settings',{}),fixed_until=96,
        prior={key:actual[key] for key in ('charge_mwh','discharge_mwh','soc_mwh')})
    layer['raw_solution']=result['raw_solution'];layer['solver']=result['optimization']
    ledger=account_layer(layer);customers=[]
    for c in data['customers']:
        cid=c['terms']['customer_id'];q=actual['customer_load_mwh'][cid];entry=ledger['customer_results'][cid]
        baseline=sum(qj*p for qj,p in zip(q,c['tou_price']))
        bill=sum(qj*p for qj,p in zip(q,prices[cid]));energy=sum(q)
        customers.append(dict(customer_id=cid,package=state['packages'][cid],energy_mwh=energy,
            tou_bill_yuan=baseline,actual_bill_yuan=bill,saving_yuan=baseline-bill,
            saving_ratio=(baseline-bill)/baseline if baseline else None,
            allocated_cost_yuan=entry['expected_cost_yuan'],allocated_revenue_yuan=entry['expected_revenue_yuan'],
            company_profit_on_customer_yuan=bill-entry['expected_cost_yuan'],
            spread_yuan_per_mwh=(bill-entry['expected_cost_yuan'])/energy if energy else None,
            cost_breakdown=entry['cost_breakdown'],periods=[dict(period=j+1,load_mwh=q[j],tou_price=c['tou_price'][j],
                retail_price=prices[cid][j],bill_yuan=q[j]*prices[cid][j],tou_bill_yuan=q[j]*c['tou_price'][j],
                allocated_cost_yuan=entry['periods'][j]['cost_yuan']) for j in range(96)]))
    baseline=sum(c['tou_bill_yuan'] for c in customers);cost=ledger['expected_wholesale_cost_yuan'];energy=sum(aggregate)
    expected=account_layer(last)
    return dict(case_id=state['case_id'],target_date=data['target_date'],source=actual['source'],
        settlement_scope='目标日实际现金流；合同覆盖考核按目标日48点等效口径，非正式月结',
        procurement_cost_yuan=cost,retail_revenue_yuan=revenue,profit_yuan=revenue-cost,
        spread_yuan_per_mwh=(revenue-cost)/energy if energy else None,
        tou_baseline_bill_yuan=baseline,customer_saving_yuan=baseline-revenue,
        customers=customers,cost_breakdown=ledger['wholesale_cost_breakdown']['expected'],
        accounting_checks=ledger['accounting_checks'],
        unallocated_cost_yuan=cost-sum(c['allocated_cost_yuan'] for c in customers),
        forecast_comparison=dict(expected_cost_yuan=expected['expected_wholesale_cost_yuan'],
            expected_profit_yuan=expected['expected_profit_yuan'],cost_error_yuan=cost-expected['expected_wholesale_cost_yuan'],
            profit_error_yuan=revenue-cost-expected['expected_profit_yuan']),
        dispatch_reoptimized=False,physical_validation=result['optimization']['status'])
