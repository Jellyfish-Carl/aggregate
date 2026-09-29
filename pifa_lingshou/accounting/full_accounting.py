"""Exact quarter-hour ledgers for the pifa L1/L2/L3 retail extension."""
from __future__ import annotations

from ..inputs.full_model import retail_prices
from .retail import summarize_distribution, weighted_mean

COST_KEYS = ('annual_energy_cost_yuan', 'monthly_energy_cost_yuan', 'ten_day_energy_cost_yuan', 'd3_energy_cost_yuan', 'd2_energy_cost_yuan', 'day_ahead_cost_yuan', 'real_time_cost_yuan', 'deviation_penalty_yuan', 'storage_degradation_yuan')
PRODUCT_COST_KEYS = {'ANNUAL':'annual_energy_cost_yuan','MONTHLY':'monthly_energy_cost_yuan','TEN_DAY':'ten_day_energy_cost_yuan','D-3':'d3_energy_cost_yuan','D-2':'d2_energy_cost_yuan'}


def contract_period_costs(fills):
    costs = [0.0] * 96
    for fill in fills:
        days = fill['equivalent_delivery_days']
        total = sum(abs(q) for q in fill['delivery_curve'].values())
        for h in range(48):
            q = fill['delivery_curve'].get('P%02d' % (h + 1), 0.0)
            fee = fill['fee'] * abs(q) / total if total else fill['fee'] / 48
            for j in (2*h, 2*h+1):
                costs[j] += (q * fill['price'] + fee) / days / 2
    return costs


def contract_period_costs_by_product(fills):
    out = {key: [0.0] * 96 for key in PRODUCT_COST_KEYS.values()}
    for fill in fills:
        key = PRODUCT_COST_KEYS.get(str(fill['product_class']))
        if not key:
            continue
        days = float(fill['equivalent_delivery_days'])
        total = sum(abs(q) for q in fill['delivery_curve'].values())
        for h in range(48):
            q = float(fill['delivery_curve'].get('P%02d' % (h + 1), 0.0))
            fee = float(fill['fee']) * abs(q) / total if total else float(fill['fee']) / 48
            for j in (2*h, 2*h+1):
                out[key][j] += (q * float(fill['price']) + fee) / days / 2
    return out


def account(config, package, risk, wholesale):
    raw = wholesale['wholesale_raw_solution']
    opt = wholesale['l3_objective']
    settings=raw.get('storage_settings',{})
    friction=settings.get('friction_yuan_per_mwh',.01)
    efficiency=settings.get('efficiency',.92)
    ratio=settings.get('deviation_ratio',.1)
    degradation=settings.get('degradation_yuan_per_mwh',2.)
    fills = wholesale['portfolio']['optimized']['fills']
    contract_costs_by_product = contract_period_costs_by_product(fills)
    contract_costs = [sum(values[j] for values in contract_costs_by_product.values()) for j in range(96)]
    # ContractFill quantity is rounded in the legacy engine. Reconcile its
    # fixed fee/notional scalar to the original solver cost without dropping it.
    correction = raw['contract_cost_yuan'] - sum(contract_costs)
    contract_costs = [v + correction / 96 for v in contract_costs]
    if correction and contract_costs_by_product:
        target_key = 'annual_energy_cost_yuan' if 'annual_energy_cost_yuan' in contract_costs_by_product else next(iter(contract_costs_by_product))
        contract_costs_by_product[target_key] = [v + correction / 96 for v in contract_costs_by_product[target_key]]
    probs = [s['probability'] for s in raw['scenarios']]
    scenario_ledgers, breakdowns = [], []
    customers = {c.customer_id: [] for c in config.customers}
    max_balance = max_ledger = max_customer = 0.0
    for si, s in enumerate(raw['scenarios']):
        prices = retail_prices(config, package, s['real_time_price'])
        periods = []
        for j in range(96):
            load, da, rt = s['load_mwh'][j], s['day_ahead_price'][j], s['real_time_price'][j]
            db, ds = raw['day_ahead_buy_mwh'][j], raw['day_ahead_sell_mwh'][j]
            rb, rs = s['rt_buy_mwh'][j], s['rt_sell_mwh'][j]
            c, d = raw['charge_mwh'][j], raw['discharge_mwh'][j]
            cost_parts = {key: values[j] for key, values in contract_costs_by_product.items()}
            cost_parts.update({
                'day_ahead_cost_yuan': (da+friction)*db-(da-friction)*ds,
                'real_time_cost_yuan': (rt+friction)*rb-(rt-friction)*rs,
                'deviation_penalty_yuan': (1.05*max(da-rt,0)*max(rb-rs-ratio*load,0)
                    + 1.05*max(rt-da,0)*max(rs-rb-ratio*load,0)
                    + (s['period_costs'][j].get('contract_assessment_penalty_yuan',0.) if 'period_costs' in s else 0.)),
                'storage_degradation_yuan': degradation*(c+d),
            })
            cost = sum(cost_parts.values())
            revenue = sum(load * config.customer_shares[i][j] * prices[cust.customer_id][j] for i,cust in enumerate(config.customers))
            # A zero-load interval cannot supply a unit allocation price. Keep
            # any cash flow explicitly unallocated instead of losing it.
            buy = cost/load if load else None
            sell = revenue/load if load else None
            periods.append(dict(period=j+1, load_mwh=load, buy_price_yuan_per_mwh=buy,
                sell_price_yuan_per_mwh=sell, wholesale_cost_yuan=cost, retail_revenue_yuan=revenue,
                unallocated_cost_yuan=cost if not load else 0.0, **cost_parts))
            max_balance = max(max_balance, abs(raw['declaration_mwh'][j]+rb-rs-c+d-load))
        cost = sum(row['wholesale_cost_yuan'] for row in periods)
        revenue = sum(row['retail_revenue_yuan'] for row in periods)
        max_ledger = max(max_ledger, abs(cost - s['wholesale_cost_yuan']))
        breakdowns.append(dict(scenario_id=s['scenario_id'], probability=s['probability'],
            total_wholesale_cost_yuan=cost, contract_cost_yuan=sum(contract_costs), **{k:sum(row[k] for row in periods) for k in COST_KEYS}))
        scenario_ledgers.append(dict(scenario_id=s['scenario_id'], probability=s['probability'],
            wholesale_cost_yuan=cost, retail_revenue_yuan=revenue, profit_yuan=revenue-cost,
            loss_yuan=cost-revenue, periods=periods))
        for i, cust in enumerate(config.customers):
            rows = []
            for j, row in enumerate(periods):
                q = s['load_mwh'][j]*config.customer_shares[i][j]
                cc = q*(row['buy_price_yuan_per_mwh'] or 0)
                cr = q*(row['sell_price_yuan_per_mwh'] or 0)
                rows.append(dict(period=j+1, load_mwh=q, cost_yuan=cc, revenue_yuan=cr,
                    profit_yuan=cr-cc, actual_bill_yuan=q*prices[cust.customer_id][j],
                    buy_price_yuan_per_mwh=row['buy_price_yuan_per_mwh'],
                    sell_price_yuan_per_mwh=row['sell_price_yuan_per_mwh'],
                    **{k:row[k]*config.customer_shares[i][j] if row['load_mwh'] else 0. for k in COST_KEYS}))
            cc, cr, q = (sum(row[key] for row in rows) for key in ('cost_yuan','revenue_yuan','load_mwh'))
            customers[cust.customer_id].append(dict(scenario_id=s['scenario_id'], probability=s['probability'],
                cost_yuan=cc, revenue_yuan=cr, profit_yuan=cr-cc, energy_mwh=q,
                spread_yuan_per_mwh=(cr-cc)/q if q else 0,
                actual_bill_yuan=sum(row['actual_bill_yuan'] for row in rows), periods=rows))
        max_customer = max(max_customer, abs(sum(v[-1]['cost_yuan'] for v in customers.values()) + sum(p['unallocated_cost_yuan'] for p in periods)-cost))
    def stats(key):
        return summarize_distribution([s[key] for s in scenario_ledgers], probs)
    cost_stats, rev_stats, profit_stats = (stats(k) for k in ('wholesale_cost_yuan','retail_revenue_yuan','profit_yuan'))
    customer_results = {}
    for cid, scenarios in customers.items():
        metrics = {}
        for source, dest in [('cost_yuan','cost'),('revenue_yuan','revenue'),('profit_yuan','profit'),('spread_yuan_per_mwh','price_spread'),('actual_bill_yuan','bill')]:
            dist = summarize_distribution([s[source] for s in scenarios], probs)
            unit = 'yuan_per_mwh' if dest=='price_spread' else 'yuan'
            metrics.update({f'expected_{dest}_{unit}':dist['mean'], f'{dest}_p10_{unit}':dist['p10'], f'{dest}_p90_{unit}':dist['p90']})
        energy = weighted_mean([s['energy_mwh'] for s in scenarios],probs)
        rows=[]
        for j in range(96):
            row={key:weighted_mean([s['periods'][j][key] for s in scenarios],probs) for key in ('load_mwh','cost_yuan','revenue_yuan','profit_yuan','actual_bill_yuan')+COST_KEYS}
            row.update(period=j+1, buy_price_yuan_per_mwh=row['cost_yuan']/row['load_mwh'] if row['load_mwh'] else None,
                sell_price_yuan_per_mwh=row['revenue_yuan']/row['load_mwh'] if row['load_mwh'] else None)
            rows.append(row)
        customer_results[cid]=dict(package=config.customer_packages.get(cid,package), load_mean_mwh=energy,
            expected_buy_price_yuan_per_mwh=metrics['expected_cost_yuan']/energy if energy else None,
            expected_sell_price_yuan_per_mwh=metrics['expected_revenue_yuan']/energy if energy else None,
            periods=rows, scenarios=scenarios, cost_breakdown={k:sum(row[k] for row in rows) for k in COST_KEYS}, **metrics)
    schedule=[]
    for j in range(96):
        row={key:raw[key][j] for key in ('declaration_mwh','day_ahead_buy_mwh','day_ahead_sell_mwh','charge_mwh','discharge_mwh','soc_mwh')}
        row.update(period=j+1, contract_supply_mwh=raw['contract_curve_mwh'][j],
            p10_mwh=wholesale['declaration']['rows'][j]['p10_mwh'], p90_mwh=wholesale['declaration']['rows'][j]['p90_mwh'],
            aggregate_load_by_scenario_mwh=[s['load_mwh'][j] for s in raw['scenarios']],
            day_ahead_price_by_scenario=[s['day_ahead_price'][j] for s in raw['scenarios']],
            real_time_price_by_scenario=[s['real_time_price'][j] for s in raw['scenarios']],
            real_time_buy_by_scenario_mwh=[s['rt_buy_mwh'][j] for s in raw['scenarios']],
            real_time_sell_by_scenario_mwh=[s['rt_sell_mwh'][j] for s in raw['scenarios']],
            real_time_net_by_scenario_mwh=[s['rt_buy_mwh'][j]-s['rt_sell_mwh'][j] for s in raw['scenarios']])
        schedule.append(row)
    previous=raw['initial_soc_mwh']; soc_error=0
    for row in schedule:
        soc_error=max(soc_error,abs(row['soc_mwh']-previous-efficiency*row['charge_mwh']+row['discharge_mwh']/efficiency))
        previous=row['soc_mwh']
    charge=sum(raw['charge_mwh']); discharge=sum(raw['discharge_mwh'])
    rt_mean=[weighted_mean([s['real_time_price'][j] for s in raw['scenarios']],probs) for j in range(96)]
    result=dict(package=package, risk_lambda=risk, status=opt['status'], feasible=True,
        backend=opt['backend'], objective_yuan=raw['objective_yuan'], mip_gap=opt['mip_gap'],
        expected_wholesale_cost_yuan=cost_stats['mean'], wholesale_cost_cvar_yuan=cost_stats['cvar'],
        expected_retail_revenue_yuan=rev_stats['mean'], expected_profit_yuan=profit_stats['mean'],
        profit_p10_yuan=profit_stats['p10'], profit_p90_yuan=profit_stats['p90'],
        retail_revenue_p10_yuan=rev_stats['p10'],retail_revenue_p90_yuan=rev_stats['p90'],
        profit_loss_cvar_yuan=stats('loss_yuan')['cvar'],
        declaration_band_penalty_yuan=raw['declaration_penalty_yuan'],
        wholesale_cost_breakdown={'scenarios':breakdowns,'expected':{k:weighted_mean([s[k] for s in breakdowns],probs) for k in COST_KEYS+('contract_cost_yuan','total_wholesale_cost_yuan',)}},
        customer_results=customer_results, scenario_results=scenario_ledgers, schedule=schedule,
        storage_summary=dict(total_charge_mwh=charge,total_discharge_mwh=discharge,initial_soc_mwh=raw['initial_soc_mwh'],
            terminal_soc_mwh=previous,soc_lower_bound_mwh=settings.get('minimum_soc_mwh',2),soc_upper_bound_mwh=settings.get('maximum_soc_mwh',20),efficiency=efficiency,
            average_charge_price_yuan_per_mwh=sum(q*p for q,p in zip(raw['charge_mwh'],rt_mean))/charge if charge else None,
            average_discharge_price_yuan_per_mwh=sum(q*p for q,p in zip(raw['discharge_mwh'],rt_mean))/discharge if discharge else None),
        accounting_checks=dict(max_energy_balance_residual_mwh=max_balance,max_soc_recursion_residual_mwh=soc_error,
            terminal_soc_residual_mwh=previous-settings.get('terminal_soc_mwh',raw['initial_soc_mwh']),max_wholesale_ledger_residual_yuan=max_ledger,
            customer_cost_reconciliation_yuan=max_customer,
            customer_revenue_reconciliation_yuan=sum(c['expected_revenue_yuan'] for c in customer_results.values())-rev_stats['mean'],
            expected_profit_reconciliation_yuan=profit_stats['mean']-(rev_stats['mean']-cost_stats['mean'])),
        wholesale_engine=wholesale,
        contracts={'fills':fills,'contract_cost_yuan':raw['contract_cost_yuan'],
            'period_cost_yuan':contract_costs,'period_cost_by_product_yuan':contract_costs_by_product,'contract_rounding_reconciliation_yuan':correction},
    )
    return result
