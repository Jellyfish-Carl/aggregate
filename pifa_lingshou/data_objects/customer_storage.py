"""Request-side battery defaults and validation (MW input, MWh model)."""
from math import isfinite


def default_customer_storage(index=0):
    power = (.4, .5, .6)[index % 3]
    capacity = 4 * power
    return dict(capacity_mwh=capacity, minimum_soc_mwh=.1*capacity,
        maximum_soc_mwh=.9*capacity, initial_soc_mwh=.5*capacity,
        terminal_soc_mwh=.5*capacity, maximum_charge_mw=power,
        maximum_discharge_mw=power, charge_efficiency=.94,
        discharge_efficiency=.94, degradation_yuan_per_mwh=8.,
        startup_cost_yuan=5., shutdown_cost_yuan=5.,
        minimum_active_power_ratio=.1, initial_mode=0,
        minimum_on_periods=2, cooldown_periods=1)


def battery_settings(value):
    required = set(default_customer_storage())
    if required-set(value) or set(value)-required-{'maximum_charge_mwh','maximum_discharge_mwh'}:
        raise ValueError('逐客户储能参数缺失或未知: '+str(required ^ set(value)))
    if any(not isinstance(v,(int,float)) or not isfinite(v) for v in value.values()):
        raise ValueError('逐客户储能参数必须为有限数值')
    v = dict(value)
    if not 0 <= v['minimum_soc_mwh'] <= v['initial_soc_mwh'] <= v['maximum_soc_mwh'] <= v['capacity_mwh']:
        raise ValueError('逐客户储能容量或SOC边界错误')
    if not v['minimum_soc_mwh'] <= v['terminal_soc_mwh'] <= v['maximum_soc_mwh']:
        raise ValueError('逐客户储能终值SOC错误')
    for key in ('charge_efficiency','discharge_efficiency'):
        if not 0 < v[key] <= 1: raise ValueError(key+'须在(0,1]')
    for key in ('maximum_charge_mw','maximum_discharge_mw','degradation_yuan_per_mwh',
                'startup_cost_yuan','shutdown_cost_yuan'):
        if v[key] < 0: raise ValueError(key+'不能为负')
    for key in ('minimum_on_periods','cooldown_periods'):
        if type(v[key]) is not int or not 0 <= v[key] <= 96:
            raise ValueError(key+'必须是0到96的整数')
    if v['initial_mode'] not in (-1,0,1) or not 0 < v['minimum_active_power_ratio'] <= 1:
        raise ValueError('储能初始模式或最小有效功率错误')
    v['maximum_charge_mwh'] = v['maximum_charge_mw'] * .25
    v['maximum_discharge_mwh'] = v['maximum_discharge_mw'] * .25
    return v
