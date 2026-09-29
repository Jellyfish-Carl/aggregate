"""Signing-period package estimates; no procurement orders or realized data."""
from statistics import mean
from ..inputs.full_model import FullModelInput,retail_prices
from ..inputs.case_data import layer_input
from ..data_objects.model import Customer
from ..accounting.retail import summarize_distribution
from ..service.layer_runner import run_layer


# The signing screen is an ex-ante comparison, before a procurement MILP has
# been run.  These fixed points make the recommendation view comparable to
# the later risk replay without implying that a package is automatically
# selected by lambda.
RECOMMENDATION_LAMBDAS = (0.0, 0.25, 0.5, 0.75, 1.0)


def _risk_adjusted(mean_value, p10_value, risk_lambda):
    """Move the displayed expected value toward its lower forecast quantile."""

    return float(mean_value) - float(risk_lambda) * (
        float(mean_value) - float(p10_value)
    )


def _lambda_curve(stats):
    return [
        dict(
            lambda_value=risk_lambda,
            expected_saving_yuan=_risk_adjusted(
                stats["saving"]["mean"], stats["saving"]["p10"], risk_lambda
            ),
            expected_profit_yuan=_risk_adjusted(
                stats["profit"]["mean"], stats["profit"]["p10"], risk_lambda
            ),
            expected_spread_yuan_per_mwh=_risk_adjusted(
                stats["spread"]["mean"], stats["spread"]["p10"], risk_lambda
            ),
            profit_p10_yuan=float(stats["profit"]["p10"]),
            profit_p90_yuan=float(stats["profit"]["p90"]),
            profit_interval_low_yuan=float(stats["profit"]["p10"]),
            profit_interval_high_yuan=float(stats["profit"]["mean"])
            + (1.0 - risk_lambda) * (float(stats["profit"]["p90"]) - float(stats["profit"]["mean"])),
            saving_interval_low_yuan=float(stats["saving"]["p10"]),
            saving_interval_high_yuan=float(stats["saving"]["mean"])
            + (1.0 - risk_lambda) * (float(stats["saving"]["p90"]) - float(stats["saving"]["mean"])),
            spread_p10=float(stats["spread"]["p10"]),
            spread_p90=float(stats["spread"]["p90"]),
            spread_interval_low_yuan_per_mwh=float(stats["spread"]["p10"]),
            spread_interval_high_yuan_per_mwh=float(stats["spread"]["mean"])
            + (1.0 - risk_lambda) * (float(stats["spread"]["p90"]) - float(stats["spread"]["mean"])),
        )
        for risk_lambda in RECOMMENDATION_LAMBDAS
    ]


def _distribution(samples, probabilities):
    return {
        key: summarize_distribution([sample[key] for sample in samples], probabilities)
        for key in samples[0]
    }


def recommend(data):
    market=data['market'];days=data['signing_forecast']['days'];policy=data['recommendation_policy']
    levels=((-1.28,.1),(-.5,.2),(0.,.4),(.5,.2),(1.28,.1))
    probs=[lp*pp for _,lp in levels for _,pp in levels]
    config=FullModelInput(customers=[Customer(**c['terms']) for c in data['customers']],
        annual_reference_price=market['annual_reference_price'],monthly_reference_price=market['monthly_reference_price'],
        reference_weights=market['reference_weights'])
    price_paths={}
    for pi,(pz,_) in enumerate(levels):
        for di,day in enumerate(days):
            rt=[p*day['price_factor']+abs(p*day['price_factor'])*market['price_uncertainty_ratio']*pz/1.28 for p in market['real_time_price']]
            price_paths[pi,di]=(rt,{p:retail_prices(config,p,rt) for p in ('F','L','S')})
    customers=[]
    customer_choices = {}
    customer_candidates = {}
    for c in data['customers']:
        cid=c['terms']['customer_id'];profile=c['load_profile_mwh']
        uncertainty=c['forecast_uncertainty']
        candidates=[]
        for package in ('F','L','S'):
            samples=[]
            for lz,_ in levels:
                for pi,(pz,_) in enumerate(levels):
                    energy=bill=cost=baseline=0.
                    for di,day in enumerate(days):
                        rt,prices=price_paths[pi,di];weights=market['procurement_weights']
                        for j,q0 in enumerate(profile):
                            q=q0*day['load_factors'][cid]*max(0,1+uncertainty*lz/1.28)
                            energy+=q;bill+=q*prices[package][cid][j];baseline+=q*c['tou_price'][j]
                            purchase=weights[0]*market['annual_price']+weights[1]*market['monthly_price']+weights[2]*rt[j]
                            cost+=q*(purchase+market['operating_cost_yuan_per_mwh'])
                    samples.append(dict(energy=energy,bill=bill,cost=cost,baseline=baseline,profit=bill-cost,saving=baseline-bill,
                        spread=(bill-cost)/energy if energy else 0.))
            stats=_distribution(samples,probs)
            eligible=stats['profit']['mean']>=policy['minimum_total_profit_yuan'] and stats['saving']['mean']>=policy['minimum_customer_saving_yuan']
            candidates.append(dict(package=package,eligible=eligible,
                expected_cost_yuan=stats['cost']['mean'],expected_revenue_yuan=stats['bill']['mean'],
                expected_profit_yuan=stats['profit']['mean'],profit_p10_yuan=stats['profit']['p10'],profit_p90_yuan=stats['profit']['p90'],
                expected_customer_saving_yuan=stats['saving']['mean'],expected_tou_bill_yuan=stats['baseline']['mean'],
                saving_p10_yuan=stats['saving']['p10'],saving_p90_yuan=stats['saving']['p90'],
                expected_spread_yuan_per_mwh=stats['spread']['mean'],spread_p10=stats['spread']['p10'],spread_p90=stats['spread']['p90'],profit_cvar_yuan=stats['profit']['cvar'],
                lambda_curve=_lambda_curve(stats),
                _scenario_values=samples))
        eligible=[r for r in candidates if r['eligible']]
        choice=max(eligible,key=lambda r:(r['expected_profit_yuan'],r['expected_customer_saving_yuan'])) if eligible else None
        # Keep a deterministic reference mix for the company curve even when
        # the economic floor makes every candidate ineligible.  This is a
        # display basis only; the user still confirms every package below.
        company_choice=choice or max(candidates,key=lambda r:(r['expected_profit_yuan'],r['expected_customer_saving_yuan']))
        customer_choices[cid]=company_choice['package']
        customer_candidates[cid]=candidates
        customers.append(dict(customer_id=cid,daily_energy_cv=cv,forecast_uncertainty=uncertainty,
            instability_score=instability,stability_preference=preferred,
            recommended_package=choice['package'] if choice else None,
            reason=('满足客户最低节省和单客户期望利润底线' if choice else '无套餐同时满足客户最低节省和单客户期望利润，请调整报价/条款'),
            candidates=candidates))
    # Aggregate the same 25 scenario positions across customers.  This gives
    # the company interval a real portfolio meaning instead of summing
    # independently rounded customer quantiles.
    company_samples=[]
    selected_candidates=[next(item for item in customer_candidates[cid] if item['package']==package)
                         for cid,package in customer_choices.items()]
    for index in range(len(probs)):
        company_samples.append({
            key:sum(float(candidate['_scenario_values'][index][key])
                    for candidate in selected_candidates)
            for key in ('energy','bill','cost','baseline','profit','saving')
        })
        company_samples[-1]['spread']=(
            company_samples[-1]['profit']/company_samples[-1]['energy']
            if company_samples[-1]['energy'] else 0.
        )
    company_stats=_distribution(company_samples,probs)
    company_curve=dict(
        basis='按程序推荐套餐组合（仅用于签约前比较，最终以人工确认套餐为准）',
        packages=customer_choices,
        expected_cost_yuan=company_stats['cost']['mean'],
        expected_revenue_yuan=company_stats['bill']['mean'],
        expected_profit_yuan=company_stats['profit']['mean'],
        profit_p10_yuan=company_stats['profit']['p10'],
        profit_p90_yuan=company_stats['profit']['p90'],
        expected_customer_saving_yuan=company_stats['saving']['mean'],
        saving_p10_yuan=company_stats['saving']['p10'],
        saving_p90_yuan=company_stats['saving']['p90'],
        expected_spread_yuan_per_mwh=company_stats['spread']['mean'],
        spread_p10=company_stats['spread']['p10'],
        spread_p90=company_stats['spread']['p90'],
        lambda_curve=_lambda_curve(company_stats),
    )
    for candidates in customer_candidates.values():
        for candidate in candidates:
            candidate.pop('_scenario_values',None)
    return dict(period={k:data['signing_forecast'][k] for k in ('issued_at','start_date','end_date')},
        day_count=len(days),customers=customers,method='稳定性优先 + 客户节省及公司期望利润底线',
        formula='不稳定度=0.5×逐日预测电量变异系数+0.5×预测相对半区间；低→固定F，中→分成S，高→联动L',
        cost_basis='签约前采购成本代理：年度/月度/现货预测按可编辑权重组合，加运营费；不是成交策略或结算成本',
        risk_note='25条负荷/价格组合情景作收益区间估计；λ曲线将均值向P10下行分位数平滑，区间为底层情景P10–P90；推荐不自动签约',
        lambda_curve_definition='展示值=(1-λ)×情景均值+λ×P10；风险带下界=P10，上界=均值+(1-λ)×(P90-均值)。它是签约前风险调整观察值，不替代后续批发侧MILP期望收益。',
        lambda_values=list(RECOMMENDATION_LAMBDAS),company_curve=company_curve,policy=policy)
