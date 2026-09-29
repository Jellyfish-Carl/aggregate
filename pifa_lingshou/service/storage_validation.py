"""Compare storage on/off with identical contracts, scenarios and lambda."""
from __future__ import annotations

from .wholesale import engine


def compare_storage(result):
    engine()
    from pifa_lingshou.data_objects.scenario import JointTrajectoryScenario
    from pifa_lingshou.problem_solver.l3_milp import solve_l3_milp

    source = result['wholesale_engine']
    if source['meta']['selected_event'] != 'D-1':
        return {'status': 'NOT_APPLICABLE', 'reason': '该对照针对未执行储能的D-1计划'}
    raw = source['wholesale_raw_solution']
    if 'storage_settings' in raw:
        return compare_storage_layer(dict(raw_solution=raw,package=result['package'],risk_lambda=result['risk_lambda']))
    scenarios = tuple(JointTrajectoryScenario(
        s['scenario_id'], s['probability'], 'P50', 'P50', 'P50',
        tuple(s['load_mwh']), tuple(s['day_ahead_price']), tuple(s['real_time_price']),
    ) for s in raw['scenarios'])
    load_snapshot = raw.get('load_snapshot', {'rows': source['declaration']['rows']})
    price_snapshot = raw.get('price_snapshot', source['price_forecast'])
    # Reuse exact scenarios through the public L3 input, avoiding a second
    # sampling step that could confound the counterfactual comparison.
    disabled = solve_l3_milp(
        load_snapshot, price_snapshot,
        [r['p50_mwh'] for r in load_snapshot['rows']],
        result['risk_lambda'], source['park_config']['physical_peak_mw'],
        [r['day_ahead_price_yuan_per_mwh'] for r in source['declaration']['rows']],
        [r['real_time_price_forecast_yuan_per_mwh'] for r in source['declaration']['rows']],
        1, True, raw['contract_curve_mwh'], raw['contract_cost_yuan'],
        retail_revenue_builder=lambda _: [s['retail_revenue_yuan'] for s in raw['scenarios']],
        storage_power_override_mw=0, joint_scenarios=scenarios,
    )
    cost = sum(s['probability'] * s['wholesale_cost_yuan'] for s in disabled['raw_solution']['scenarios'])
    return dict(status=disabled['optimization']['status'], package=result['package'],
        risk_lambda=result['risk_lambda'], enabled_cost_yuan=result['expected_wholesale_cost_yuan'],
        disabled_cost_yuan=cost, cost_reduction_yuan=cost-result['expected_wholesale_cost_yuan'],
        comparison='相同合同、场景和λ；禁用充放电后重新优化日前与实时交易')


def compare_storage_layer(layer_result):
    """Counterfactual for the independent fixed-declaration storage layer."""
    from copy import deepcopy
    from ..problem_solver.storage_milp import dispatch
    raw=deepcopy(layer_result['raw_solution'])
    settings=dict(raw.get('storage_settings',{}),maximum_charge_mwh=0.,maximum_discharge_mwh=0.)
    if settings.get('initial_soc_mwh',10.)!=settings.get('terminal_soc_mwh',10.):
        return dict(status='NOT_APPLICABLE',reason='初末SOC不同，完全禁用储能无法满足相同终端条件')
    from pifa_lingshou.problem_solver.milp import MilpSolveError
    try:
        disabled=dispatch(raw,layer_result['risk_lambda'],settings)
    except MilpSolveError as exc:
        return dict(status='NOT_APPLICABLE',reason='禁用储能后约束不可满足或未找到可行解: '+str(exc))
    scenarios=disabled['raw_solution']['scenarios']
    cost=sum(s['probability']*s['wholesale_cost_yuan'] for s in scenarios)
    enabled=sum(s['probability']*s['wholesale_cost_yuan'] for s in layer_result['raw_solution']['scenarios'])
    return dict(status=disabled['optimization']['status'],package=layer_result['package'],
        risk_lambda=layer_result['risk_lambda'],enabled_cost_yuan=enabled,
        disabled_cost_yuan=cost,cost_reduction_yuan=cost-enabled if enabled is not None else None,
        comparison='合同、日前申报和场景锁定；关停储能后重求实时交易与偏差考核')
