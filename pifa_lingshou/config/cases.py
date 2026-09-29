"""Defaults for replayable cases. All prices are yuan/MWh; energy is MWh."""
from datetime import date, timedelta


def defaults():
    target=date.today()+timedelta(days=30)
    return dict(name='批零测试',customer_count=3,target_date=target.isoformat(),seed=20260928,
        recommendation_days=30,risk_lambda=.25,scenario_count=16,
        market=dict(annual_price=405.,monthly_price=416.,ten_day_price=424.,
            annual_reference_price=405.,monthly_reference_price=416.,
            reference_weights=[.7,.2,.1],procurement_weights=[.7,.2,.1],
            operating_cost_yuan_per_mwh=2.,price_uncertainty_ratio=.15),
        recommendation_policy=dict(minimum_total_profit_yuan=0.,
            maximum_profit_loss_cvar_yuan=None,minimum_customer_saving_yuan=0.),
        storage=dict(minimum_soc_mwh=2.,maximum_soc_mwh=20.,initial_soc_mwh=10.,terminal_soc_mwh=10.,
            maximum_charge_mwh=1.25,maximum_discharge_mwh=1.25,efficiency=.92,degradation_yuan_per_mwh=2.,
            optimization_scenario_count=8,assessment_basis='grid_net_load',hard_deviation=False,time_limit_seconds=5.))
