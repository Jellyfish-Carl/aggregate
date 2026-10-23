"""Unified L3 declaration/storage MILP, shared before/after DA price publication.

RT settlement uses a single signed net quantity. Its convex absolute-value and
positive-part costs have exact linear epigraphs; buy/sell are its positive and
negative parts, so simultaneous gross trades cannot exist. Only storage modes
need binaries, independent of the number of scenarios.

``dispatch`` is the canonical storage entry point. ``solve_storage_milp`` is
kept as an explicit alias for callers that used the former split L3/storage
compatibility module.
"""
from __future__ import annotations
from copy import deepcopy
from math import isfinite
from time import perf_counter
from ..service.wholesale import engine
engine()
from pifa_lingshou.problem_solver.milp import LinearMilp
from pifa_lingshou.data_objects.scenario import cvar
from .storage_switching import SWITCHING_DEFAULTS, add_switching, switching_ledger, validate_switching

DEFAULTS = dict(minimum_soc_mwh=2., maximum_soc_mwh=20., initial_soc_mwh=10.,
    terminal_soc_mwh=10., maximum_charge_mwh=1.25, maximum_discharge_mwh=1.25,
    efficiency=.92, degradation_yuan_per_mwh=2.,
    **SWITCHING_DEFAULTS, friction_yuan_per_mwh=.01,
    deviation_ratio=.10, hard_deviation=False, time_limit_seconds=60., mip_relative_gap=.03,
    optimization_scenario_count=8,
    full_scenario_optimization=False,
    grid_import_limit_mwh=None, grid_export_limit_mwh=None,
    assessment_basis='grid_net_load', annual_price_yuan_per_mwh=405.,
    monthly_price_yuan_per_mwh=416., annual_coverage=.60, overall_lower_ratio=.90,
    overall_upper_ratio=1.10, assessment_penalty_multiplier=1.05)


def vector(values, name, length=96, nonnegative=False):
    if len(values) != length or any(not isfinite(float(x)) or (nonnegative and float(x)<0) for x in values):
        raise ValueError(f'{name}必须有{length}个有限' + ('非负' if nonnegative else '') + '数值')
    return [float(x) for x in values]


def dispatch(raw_input, risk_lambda=0., settings=None, fixed_until=0, prior=None, progress=None,
             *, optimize_declaration=False, physical_peak_mw=None, day_ahead_price_known=False):
    started = perf_counter()
    cfg = {**DEFAULTS, **(settings or {})}
    cfg['time_limit_seconds']=min(cfg['time_limit_seconds'],60.)
    # The default model keeps its small representative set. Explicit benchmark
    # requests opt into using every requested scenario and must not be clamped.
    if not cfg['full_scenario_optimization']:
        cfg['optimization_scenario_count'] = min(cfg['optimization_scenario_count'], 8)
    if set(cfg)-set(DEFAULTS):
        raise ValueError('未知储能参数: '+str(set(cfg)-set(DEFAULTS)))
    for k,v in cfg.items():
        if k == 'assessment_basis': continue
        if k in ('grid_import_limit_mwh','grid_export_limit_mwh') and v is None: continue
        if not isinstance(v,(float,int,bool)) or not isfinite(v):
            raise ValueError('储能参数必须为有限数值: '+k)
    if any(cfg[k] is not None and cfg[k]<0 for k in ('grid_import_limit_mwh','grid_export_limit_mwh')):
        raise ValueError('并网购入/送出电量上限不能为负')
    if not 0 < cfg['efficiency'] <= 1 or not 0 <= risk_lambda <= 1:
        raise ValueError('效率须位于(0,1]，λ须位于[0,1]')
    lo,hi=cfg['minimum_soc_mwh'],cfg['maximum_soc_mwh']
    if not 0 <= lo <= cfg['initial_soc_mwh'] <= hi or not lo <= cfg['terminal_soc_mwh'] <= hi:
        raise ValueError('SOC初值、终值必须在上下界内')
    if any(cfg[k]<0 for k in ('maximum_charge_mwh','maximum_discharge_mwh','degradation_yuan_per_mwh','friction_yuan_per_mwh','deviation_ratio','mip_relative_gap')) or cfg['time_limit_seconds']<=0:
        raise ValueError('功率、费用、偏差比例、gap不能为负；时限必须为正')
    if not isinstance(fixed_until,int) or not 0<=fixed_until<=96:
        raise ValueError('fixed_until必须为0到96的整数（已执行点数）')
    if fixed_until and prior is None:
        raise ValueError('日内滚动必须提供已执行储能轨迹')
    validate_switching(cfg)
    raw=deepcopy(raw_input)
    a=vector(raw['declaration_mwh'],'锁定日前申报',nonnegative=True)
    if optimize_declaration and fixed_until:
        raise ValueError('已进入日内，不能重新优化日前申报')
    if physical_peak_mw is not None and (not isfinite(physical_peak_mw) or physical_peak_mw<=0):
        raise ValueError('申报功率上限须为正有限数')
    for k in ('contract_curve_mwh','day_ahead_buy_mwh','day_ahead_sell_mwh'):
        vector(raw[k],k,nonnegative=k!='contract_curve_mwh')
    if any(min(b,s)>1e-8 for b,s in zip(raw['day_ahead_buy_mwh'],raw['day_ahead_sell_mwh'])):
        raise ValueError('已锁定日前交易不允许同时买卖')
    if not isfinite(raw['contract_cost_yuan']): raise ValueError('合同成本必须有限')
    if fixed_until:
        for key in ('charge_mwh','discharge_mwh','soc_mwh'):
            values=prior.get(key,[])
            if len(values) not in (fixed_until,96): raise ValueError(key+'须为已执行点数或96点')
            vector(values,key,len(values),True)
    if max(abs(a[j]-raw['contract_curve_mwh'][j]-raw['day_ahead_buy_mwh'][j]+raw['day_ahead_sell_mwh'][j]) for j in range(96))>1e-6:
        raise ValueError('合同+日前净交易与申报不平衡')
    scenarios=raw['scenarios']
    if not scenarios or any(not isfinite(s['probability']) or s['probability']<0 for s in scenarios) or abs(sum(s['probability'] for s in scenarios)-1)>1e-8:
        raise ValueError('场景概率须非负且和为1')
    for s in scenarios:
        for key in ('load_mwh','day_ahead_price','real_time_price'):
            vector(s[key],key,nonnegative=key=='load_mwh')
        if not isfinite(s.get('retail_revenue_yuan',0.)): raise ValueError('场景零售收入必须有限')
    if len({s['scenario_id'] for s in scenarios})!=len(scenarios):
        raise ValueError('场景ID不能重复')
    if not isinstance(cfg['optimization_scenario_count'],int) or cfg['optimization_scenario_count']<1:
        raise ValueError('optimization_scenario_count必须为正整数')
    if not 0<=cfg['annual_coverage']<=1 or not 0<=cfg['overall_lower_ratio']<=cfg['overall_upper_ratio'] or cfg['assessment_penalty_multiplier']<0:
        raise ValueError('考核覆盖率或罚款系数无效')
    if cfg['assessment_basis'] not in ('customer_load','grid_net_load'):
        raise ValueError('assessment_basis须为customer_load或grid_net_load')
    annual_curve=vector(raw.get('annual_contract_curve_mwh',[0.]*96),'年度合同曲线')
    overall_curve=vector(raw['contract_curve_mwh'],'总中长期合同曲线')
    # Stratify the representative storage MILP over no-storage scenario loss.
    # The resulting dispatch is then re-priced on every original scenario.
    def score(s):
        return sum(
        (s['day_ahead_price'][j]+cfg['friction_yuan_per_mwh'])*raw['day_ahead_buy_mwh'][j]
        -(s['day_ahead_price'][j]-cfg['friction_yuan_per_mwh'])*raw['day_ahead_sell_mwh'][j]
        +s['real_time_price'][j]*(s['load_mwh'][j]-a[j]) for j in range(96)
        )-float(s.get('retail_revenue_yuan',0.))
    ordered=sorted(scenarios,key=score)
    # Keep the optimization model bounded by a small representative set.  A
    # portfolio policy is a business-side feasibility check; it must not
    # silently expand the MILP to every scenario (which multiplies the same
    # storage variables and can prevent HiGHS from finding an incumbent).
    count = (len(ordered) if cfg['full_scenario_optimization']
             else min(len(ordered), cfg['optimization_scenario_count']))
    sampled=[]
    for i in range(count):
        q=(i+.5)/count; cumulative=0.; chosen=ordered[-1]
        for candidate in ordered:
            cumulative+=candidate['probability']
            if cumulative>=q: chosen=candidate; break
        if chosen not in sampled: sampled.append(chosen)
    if len(sampled)<count:
        sampled.extend(item for item in ordered if item not in sampled and len(sampled)<count)
    if count==len(scenarios):
        scenarios_opt=[dict(s) for s in scenarios]
    else:
        # Preserve both tails, assign each original probability mass to its
        # nearest representative. Report the approximation separately.
        if count>=2:
            sampled[0]=ordered[0]; sampled[-1]=ordered[-1]
        unique=[]
        for s in sampled:
            if s not in unique: unique.append(s)
        unique.extend(s for s in ordered if s not in unique and len(unique)<count)
        sampled=unique
        scenarios_opt=[dict(s,probability=0.) for s in sampled]
        for s in scenarios:
            nearest=min(scenarios_opt,key=lambda x:abs(score(x)-score(s)))
            nearest['probability']+=s['probability']
    if optimize_declaration and cfg['maximum_charge_mwh'] == 0 and cfg['maximum_discharge_mwh'] == 0:
        model_name = 'L3-DECLARATION-ONLY'
    elif not optimize_declaration:
        model_name = 'L3-STORAGE-DA'
    else:
        model_name = 'L3-JOINT-DECLARATION-STORAGE'
    m=LinearMilp(model_name)
    declarations=[]
    for j in range(96):
        # Same variable in both phases. Gate closure fixes its bounds only.
        lower=0. if optimize_declaration else a[j]
        upper=physical_peak_mw/4 if optimize_declaration and physical_peak_mw is not None else (max(s['load_mwh'][j] for s in scenarios)+cfg['maximum_charge_mwh'] if optimize_declaration else a[j])
        declarations.append(m.add_var(f'declaration_{j}',lower=lower,upper=upper))
    customer_cfg={}
    for cid,values in raw.get('customer_storage_config',{}).items():
        from ..data_objects.customer_storage import battery_settings
        customer_cfg[cid]=battery_settings(values)
    per_customer=bool(customer_cfg)
    if per_customer and any(set(s.get('customer_load_mwh',{}))!=set(customer_cfg) for s in scenarios):
        raise ValueError('逐客户储能与场景客户集合不一致，禁止退回聚合储能')
    for s in scenarios:
        if per_customer and any(abs(sum(v[j] for v in s['customer_load_mwh'].values())-s['load_mwh'][j])>1e-6 for j in range(96)):
            raise ValueError('逐客户场景负荷合计不一致')
    charge_vars={}; discharge_vars={}; soc_vars={}; switch_costs={}
    if per_customer:
        if optimize_declaration:
            for j in range(96):
                upper=physical_peak_mw/4 if physical_peak_mw is not None else max(s['load_mwh'][j] for s in scenarios)
                m.add_constraint({declarations[j]:1},upper=upper)
        for cid,p in customer_cfg.items():
            cs=[];ds=[];es=[]
            for j in range(96):
                capc=p['maximum_charge_mwh'];capd=p['maximum_discharge_mwh']
                c=m.add_var(f'{cid}_charge_{j}',upper=capc);d=m.add_var(f'{cid}_discharge_{j}',upper=capd)
                e=m.add_var(f'{cid}_soc_{j}',lower=p['minimum_soc_mwh'],upper=p['maximum_soc_mwh'])
                terms={e:1,c:-p['charge_efficiency'],d:1/p['discharge_efficiency']}
                if j:terms[es[-1]]=-1
                rhs=p['initial_soc_mwh'] if not j else 0
                m.add_constraint(terms,lower=rhs,upper=rhs)
                if j<fixed_until:
                    history=(prior or {}).get('customer_storage',{}).get(cid,{})
                    if not history: raise ValueError('逐客户储能执行历史缺失: '+cid)
                    for key,var in (('charge_mwh',c),('discharge_mwh',d),('soc_mwh',e)):
                        m.add_constraint({var:1},lower=history[key][j],upper=history[key][j])
                cs.append(c);ds.append(d);es.append(e)
            m.add_constraint({es[-1]:1},lower=p['terminal_soc_mwh'],upper=p['terminal_soc_mwh'])
            charge_vars[cid]=cs;discharge_vars[cid]=ds;soc_vars[cid]=es
            rows=add_switching(m,cs,ds,p,fixed_until,(prior or {}).get('customer_storage',{}).get(cid),prefix=cid+'_')
            switch_costs[cid]=rows
        cs=[m.add_var(f'aggregate_charge_{j}',lower=0,upper=sum(customer_cfg[cid]['maximum_charge_mwh'] for cid in customer_cfg)) for j in range(96)]
        ds=[m.add_var(f'aggregate_discharge_{j}',lower=0,upper=sum(customer_cfg[cid]['maximum_discharge_mwh'] for cid in customer_cfg)) for j in range(96)]
        for j in range(96):
            m.add_constraint({**{charge_vars[cid][j]:1 for cid in customer_cfg},cs[j]:-1},lower=0,upper=0)
            m.add_constraint({**{discharge_vars[cid][j]:1 for cid in customer_cfg},ds[j]:-1},lower=0,upper=0)
        switching_costs=[{v:cost for cid in customer_cfg for v,cost in switch_costs[cid][j].items()} for j in range(96)]
    else:
        cs,ds,es=[],[],[]
        eta=cfg['efficiency']; capc=cfg['maximum_charge_mwh']; capd=cfg['maximum_discharge_mwh']
        for j in range(96):
            c=m.add_var(f'charge_{j}',upper=capc); d=m.add_var(f'discharge_{j}',upper=capd)
            z=m.add_var(f'charge_mode_{j}',upper=1,integer=True)
            e=m.add_var(f'soc_{j}',lower=lo,upper=hi)
            m.add_constraint({c:1,z:-capc},upper=0);m.add_constraint({d:1,z:capd},upper=capd)
            terms={e:1,c:-eta,d:1/eta};rhs=cfg['initial_soc_mwh'] if not j else 0
            if j:terms[es[-1]]=-1
            m.add_constraint(terms,lower=rhs,upper=rhs)
            if j<fixed_until:
                for key,var in (('charge_mwh',c),('discharge_mwh',d),('soc_mwh',e)):
                    value=float(prior[key][j]);m.add_constraint({var:1},lower=value,upper=value)
            cs.append(c);ds.append(d);es.append(e)
        switching_costs=add_switching(m,cs,ds,cfg,fixed_until,prior)
        m.add_constraint({es[-1]:1},lower=cfg['terminal_soc_mwh'],upper=cfg['terminal_soc_mwh'])
    # DA net trade is shared across scenarios. Its friction cost therefore
    # needs only one epigraph per period, not one per scenario and period:
    # DA_s = price_s * (declaration - contract) + friction * abs(net).
    # Both expected loss and CVaR are monotone in each scenario loss, so the
    # common epigraph is tight at an optimum, just as the old separate ones.
    da_friction=[]
    for j in range(96):
        friction_cost=m.add_var(f'da_friction_{j}')
        for slope in (-cfg['friction_yuan_per_mwh'],cfg['friction_yuan_per_mwh']):
            m.add_constraint({friction_cost:1.,declarations[j]:-slope},lower=-slope*overall_curve[j])
        da_friction.append(friction_cost)
    var=m.add_var('cvar_eta',lower=-1e10,upper=1e10)
    m.add_to_objective(var,risk_lambda)
    representative=next((i for i,s in enumerate(scenarios_opt) if s['scenario_id']=='L50_DA50_SP50'),0)
    loss_names=[]
    for si,s in enumerate(scenarios_opt):
        loss=m.add_var(f'loss_{si}',lower=-1e10,upper=1e10)
        loss_names.append(loss)
        excess=m.add_var(f'tail_{si}',upper=1e10)
        terms={loss:1}; constant=raw['contract_cost_yuan']-s.get('retail_revenue_yuan',0.)
        for j in range(96):
            load=s['load_mwh'][j]; rt=s['real_time_price'][j]; da=s['day_ahead_price'][j]
            band=cfg['deviation_ratio']*load
            terms[declarations[j]]=-da
            terms[da_friction[j]]=-1.
            constant-=da*overall_curve[j]
            if per_customer:
                for cid,p in customer_cfg.items():
                    terms[charge_vars[cid][j]]=-p['degradation_yuan_per_mwh']
                    terms[discharge_vars[cid][j]]=-p['degradation_yuan_per_mwh']
            else:
                terms[cs[j]]=-cfg['degradation_yuan_per_mwh'];terms[ds[j]]=-cfg['degradation_yuan_per_mwh']
            for variable, cost in switching_costs[j].items():
                terms[variable] = -cost
            # The RT cash, friction and deviation charges form one convex
            # four-segment function of net deviation; four supporting cuts
            # need only one epigraph variable instead of four hinge variables.
            rt_cost=m.add_var(f'rt_cost_{si}_{j}',lower=-1e7,upper=1e7)
            terms[rt_cost]=-1.0
            buy_penalty=1.05*max(da-rt,0)
            sell_penalty=1.05*max(rt-da,0)
            friction=cfg['friction_yuan_per_mwh']
            segments=((rt-friction-sell_penalty,-sell_penalty*band),
                (rt-friction,0.),(rt+friction,0.),(rt+friction+buy_penalty,-buy_penalty*band))
            for slope,intercept in dict.fromkeys(segments):
                m.add_constraint({rt_cost:1.,cs[j]:-slope,ds[j]:slope,declarations[j]:slope},lower=slope*load+intercept)
            if cfg['hard_deviation'] and si==representative and j>=fixed_until:
                m.add_constraint({cs[j]:1,ds[j]:-1,declarations[j]:-1},lower=-band-load,upper=band-load)
        assessment_constant = 0.0
        annual_rate = cfg['assessment_penalty_multiplier'] * max(cfg['annual_price_yuan_per_mwh'] - cfg['monthly_price_yuan_per_mwh'], 0.0)
        for h in range(48):
            j0, j1 = 2*h, 2*h+1
            meter = s['load_mwh'][j0] + s['load_mwh'][j1]
            annual_contract = annual_curve[j0] + annual_curve[j1]
            overall_contract = overall_curve[j0] + overall_curve[j1]
            spot_half_hour = (s['real_time_price'][j0] + s['real_time_price'][j1]) / 2.0
            low_rate = cfg['assessment_penalty_multiplier'] * max(cfg['monthly_price_yuan_per_mwh'] - spot_half_hour, 0.0)
            high_rate = cfg['assessment_penalty_multiplier'] * max(spot_half_hour - cfg['monthly_price_yuan_per_mwh'], 0.0)
            if cfg['assessment_basis'] == 'customer_load':
                assessment_constant += annual_rate * max(cfg['annual_coverage'] * meter - annual_contract, 0.0)
                assessment_constant += low_rate * max(cfg['overall_lower_ratio'] * meter - overall_contract, 0.0)
                assessment_constant += high_rate * max(overall_contract - cfg['overall_upper_ratio'] * meter, 0.0)
            else:
                annual_under = m.add_var(f'annual_assessment_{si}_{h}', upper=1e8)
                overall_under = m.add_var(f'overall_assessment_under_{si}_{h}', upper=1e8)
                overall_over = m.add_var(f'overall_assessment_over_{si}_{h}', upper=1e8)
                m.add_constraint({annual_under:1, cs[j0]:-cfg['annual_coverage'], cs[j1]:-cfg['annual_coverage'], ds[j0]:cfg['annual_coverage'], ds[j1]:cfg['annual_coverage']}, lower=cfg['annual_coverage'] * meter - annual_contract)
                m.add_constraint({overall_under:1, cs[j0]:-cfg['overall_lower_ratio'], cs[j1]:-cfg['overall_lower_ratio'], ds[j0]:cfg['overall_lower_ratio'], ds[j1]:cfg['overall_lower_ratio']}, lower=cfg['overall_lower_ratio'] * meter - overall_contract)
                m.add_constraint({overall_over:1, cs[j0]:cfg['overall_upper_ratio'], cs[j1]:cfg['overall_upper_ratio'], ds[j0]:-cfg['overall_upper_ratio'], ds[j1]:-cfg['overall_upper_ratio']}, lower=overall_contract - cfg['overall_upper_ratio'] * meter)
                terms[annual_under] = -annual_rate
                terms[overall_under] = -low_rate
                terms[overall_over] = -high_rate
        constant += assessment_constant
        m.add_constraint(terms,lower=constant,upper=constant)
        m.add_constraint({loss:1,var:-1,excess:-1},upper=0)
        m.add_to_objective(loss,(1-risk_lambda)*s['probability'])
        m.add_to_objective(excess,risk_lambda*s['probability']/.05)
    policy=raw.get('portfolio_policy')
    if policy:
        m.add_constraint({v:s['probability'] for v,s in zip(loss_names,scenarios_opt)},upper=-policy['minimum_total_profit_yuan'])
        audit=raw['economic_audit']
        for entry in audit['customers'].values():
            m.add_constraint({},lower=policy['minimum_customer_saving_yuan']-entry['p10']-1e-6)
        m.add_constraint({},lower=policy['minimum_total_customer_saving_yuan']-audit['total_savings']['mean']-1e-6)
        if policy.get('maximum_profit_loss_cvar_yuan') is not None:
            eta=m.add_var('profit_loss_cap_eta');cap={eta:1.}
            for si,(loss,s) in enumerate(zip(loss_names,scenarios_opt)):
                tail=m.add_var(f'profit_loss_cap_tail_{si}')
                m.add_constraint({tail:1,eta:1,loss:-1},lower=0)
                cap[tail]=s['probability']/.05
            m.add_constraint(cap,upper=policy['maximum_profit_loss_cvar_yuan'])
    if progress:
        stage = ('只优化日前申报（储能功率固定为零）'
                 if optimize_declaration and cfg['maximum_charge_mwh'] == 0 and cfg['maximum_discharge_mwh'] == 0
                 else '联合优化申报与储能' if optimize_declaration
                 else '锁定已申报电量，更新价格与储能计划')
        progress(45,'L3统一MILP：'+stage+'；套利、偏差、覆盖考核与退化共同计价')
    # Hard compliance must cover all original trajectories even when the
    # cost objective is reduced to representative scenarios.
    if cfg['hard_deviation']:
        for s in scenarios:
            for j in range(fixed_until,96):
                load=s['load_mwh'][j]; band=cfg['deviation_ratio']*load
                m.add_constraint({cs[j]:1,ds[j]:-1,declarations[j]:-1},lower=-band-load,upper=band-load)
    # The inherited physical_peak_mw limits declarations, not metered flow.
    # Apply a separately supplied connection agreement to all future scenarios.
    for s in scenarios:
        for j in range(fixed_until,96):
            if cfg['grid_import_limit_mwh'] is not None:
                m.add_constraint({cs[j]:1,ds[j]:-1},upper=cfg['grid_import_limit_mwh']-s['load_mwh'][j])
            if cfg['grid_export_limit_mwh'] is not None:
                m.add_constraint({cs[j]:1,ds[j]:-1},lower=-cfg['grid_export_limit_mwh']-s['load_mwh'][j])
    solved=m.solve(time_limit_seconds=cfg['time_limit_seconds'],mip_relative_gap=cfg['mip_relative_gap'])
    a=[solved.values[n] for n in declarations] if optimize_declaration else a
    raw.update(declaration_mwh=a,day_ahead_buy_mwh=[max(x-y,0.) for x,y in zip(a,overall_curve)],
        day_ahead_sell_mwh=[max(y-x,0.) for x,y in zip(a,overall_curve)])
    raw.update(charge_mwh=[0. if solved.values[n] <= 1e-6 else solved.values[n] for n in cs],
        discharge_mwh=[0. if solved.values[n] <= 1e-6 else solved.values[n] for n in ds],
        soc_mwh=([sum(solved.values[soc_vars[cid][j]] for cid in customer_cfg) for j in range(96)]
                  if per_customer else [min(hi,max(lo,solved.values[n])) for n in es]),
        initial_soc_mwh=cfg['initial_soc_mwh'],storage_settings=cfg)
    if per_customer:
        raw['customer_storage']={cid:dict(charge_mwh=[solved.values[v] for v in charge_vars[cid]],
            discharge_mwh=[solved.values[v] for v in discharge_vars[cid]],
            soc_mwh=[solved.values[v] for v in soc_vars[cid]],settings=customer_cfg[cid]) for cid in customer_cfg}
    if per_customer:
        raw['initial_soc_mwh']=sum(p['initial_soc_mwh'] for p in raw['customer_storage_config'].values())
        for cid,r in raw['customer_storage'].items():
            r['switching_ledger']=switching_ledger(r['charge_mwh'],r['discharge_mwh'],customer_cfg[cid])
        ledgers=[r['switching_ledger'] for r in raw['customer_storage'].values()]
        switching={key:sum(v[key] for v in ledgers) for key in ('startup_count','shutdown_count','switching_cost_yuan')}
        switching['periods']=[{key:sum(v['periods'][j][key] for v in ledgers) for key in ('startup_count','shutdown_count','switching_cost_yuan')} for j in range(96)]
    else:
        switching = switching_ledger(raw['charge_mwh'], raw['discharge_mwh'], cfg)
    raw['switching_ledger'] = switching
    for s in scenarios:
        s['rt_buy_mwh']=[]; s['rt_sell_mwh']=[]; s['period_costs']=[]
        for j in range(96):
            c=raw['charge_mwh'][j]; d=raw['discharge_mwh'][j]
            net=s['load_mwh'][j]-a[j]+c-d
            rb=max(net,0.); rs=max(-net,0.)
            da=s['day_ahead_price'][j]; rt=s['real_time_price'][j]; band=cfg['deviation_ratio']*s['load_mwh'][j]
            parts=dict(day_ahead_cost_yuan=(da+cfg['friction_yuan_per_mwh'])*raw['day_ahead_buy_mwh'][j]-(da-cfg['friction_yuan_per_mwh'])*raw['day_ahead_sell_mwh'][j],
                real_time_cost_yuan=rt*net+cfg['friction_yuan_per_mwh']*abs(net),
                deviation_penalty_yuan=1.05*max(da-rt,0)*max(net-band,0)+1.05*max(rt-da,0)*max(-net-band,0),
                storage_degradation_yuan=(sum(customer_cfg[cid]['degradation_yuan_per_mwh']*(r['charge_mwh'][j]+r['discharge_mwh'][j]) for cid,r in raw['customer_storage'].items()) if per_customer else cfg['degradation_yuan_per_mwh']*(c+d)),
                storage_switching_degradation_yuan=switching['periods'][j]['switching_cost_yuan'])
            s['rt_buy_mwh'].append(rb); s['rt_sell_mwh'].append(rs); s['period_costs'].append(parts)
        assessment_penalty = 0.0
        annual_rate = cfg['assessment_penalty_multiplier'] * max(cfg['annual_price_yuan_per_mwh'] - cfg['monthly_price_yuan_per_mwh'], 0.0)
        for h in range(48):
            j0, j1 = 2*h, 2*h+1
            meter = sum(s['load_mwh'][j] for j in (j0, j1))
            if cfg['assessment_basis'] == 'grid_net_load':
                meter += raw['charge_mwh'][j0] + raw['charge_mwh'][j1] - raw['discharge_mwh'][j0] - raw['discharge_mwh'][j1]
            annual_contract = annual_curve[j0] + annual_curve[j1]
            overall_contract = overall_curve[j0] + overall_curve[j1]
            spot_half_hour = (s['real_time_price'][j0] + s['real_time_price'][j1]) / 2.0
            low_rate = cfg['assessment_penalty_multiplier'] * max(cfg['monthly_price_yuan_per_mwh'] - spot_half_hour, 0.0)
            high_rate = cfg['assessment_penalty_multiplier'] * max(spot_half_hour - cfg['monthly_price_yuan_per_mwh'], 0.0)
            half_penalty = annual_rate * max(cfg['annual_coverage'] * meter - annual_contract, 0.0)
            half_penalty += low_rate * max(cfg['overall_lower_ratio'] * meter - overall_contract, 0.0)
            half_penalty += high_rate * max(overall_contract - cfg['overall_upper_ratio'] * meter, 0.0)
            assessment_penalty += half_penalty
            for j in (j0,j1): s['period_costs'][j]['contract_assessment_penalty_yuan']=half_penalty/2
        s['contract_assessment_penalty_yuan'] = assessment_penalty
        s['wholesale_cost_yuan']=raw['contract_cost_yuan']+sum(sum(p.values()) for p in s['period_costs'])
    probs=[s['probability'] for s in scenarios]
    losses=[s['wholesale_cost_yuan']-s.get('retail_revenue_yuan',0.) for s in scenarios]
    raw['objective_yuan']=(1-risk_lambda)*sum(p*l for p,l in zip(probs,losses))+risk_lambda*cvar(losses,probs,.95)+raw.get('declaration_penalty_yuan',0.)
    opt_probs=[s['probability'] for s in scenarios_opt]
    opt_losses=[next(x for x in scenarios if x['scenario_id']==s['scenario_id'])['wholesale_cost_yuan']-next(x for x in scenarios if x['scenario_id']==s['scenario_id']).get('retail_revenue_yuan',0.) for s in scenarios_opt]
    solved_objective=(1-risk_lambda)*sum(p*l for p,l in zip(opt_probs,opt_losses))+risk_lambda*cvar(opt_losses,opt_probs,.95)+raw.get('declaration_penalty_yuan',0.)
    error=abs(solved_objective-raw.get('declaration_penalty_yuan',0.)-solved.objective)
    if error>max(1e-3,abs(solved.objective)*1e-7):
        raise ValueError('MILP目标与独立重算现金流不一致: '+str(error))
    if policy:
        audit=raw['economic_audit']
        audit['expected_profit_yuan']=-sum(p*l for p,l in zip(probs,losses))
        audit['profit_satisfied']=audit['expected_profit_yuan']+1e-4>=policy['minimum_total_profit_yuan']
        if not audit['savings_satisfied'] or not audit['profit_satisfied']:
            raise ValueError('求解结果未通过三项业务下限独立复核')
    opt={k:getattr(solved,k) for k in (
        'backend','status','mip_gap','solve_seconds','time_limit_seconds',
        'variable_count','binary_count','constraint_count','node_count','dual_bound'
    )}
    opt.update(solver_backend=solved.backend,milp_executed=True,objective_yuan=solved_objective,
        max_binary_integrality_residual=max((abs(v-round(v)) for name,v in solved.values.items() if '_on_' in name),default=0.),
        objective_reconciliation_yuan=error,end_to_end_seconds=perf_counter()-started,
        rt_mutual_exclusion='单一有符号净交易；买=max(net,0)，卖=max(-net,0)，不允许往返交易',
        long_term_assessment=dict(basis=cfg['assessment_basis'], annual_coverage=cfg['annual_coverage'],
            overall_band=[cfg['overall_lower_ratio'],cfg['overall_upper_ratio']],
            controllable=cfg['assessment_basis']=='grid_net_load',
            note='储能只优化计量口径下的考核偏差成本，不改变已锁定合同成交量'),
        optimization_scenario_count=len(scenarios_opt),
        time_count=96,
        optimization_scenarios=[dict(scenario_id=s['scenario_id'],probability=s['probability']) for s in scenarios_opt],
        scenario_reduction='全场景求解' if len(scenarios_opt)==len(scenarios) else '按无储能净损失分层，保留两端，最近代表分配概率；近似优化，原场景全量评估',
        evaluation_scenario_count=len(scenarios),
        storage_switching=dict(startup_count=switching['startup_count'],
            shutdown_count=switching['shutdown_count'],
            startup_cost_yuan=cfg['startup_cost_yuan'],shutdown_cost_yuan=cfg['shutdown_cost_yuan'],
            total_cost_yuan=switching['switching_cost_yuan'], per_customer=per_customer),
        evaluation_objective_yuan=raw['objective_yuan'],per_customer_storage=per_customer,
        fixed_until=fixed_until,model_structure=(
            'L3_DECLARATION_MILP' if optimize_declaration and cfg['maximum_charge_mwh'] == 0 and cfg['maximum_discharge_mwh'] == 0
            else 'L3_JOINT_DECLARATION_STORAGE_MILP'),
        declaration_optimized=optimize_declaration,day_ahead_price_known=day_ahead_price_known,
        mip_gap_scope=(
            '日前申报问题（储能固定为零）' if optimize_declaration and cfg['maximum_charge_mwh'] == 0 and cfg['maximum_discharge_mwh'] == 0
            else '当前优化情景的申报与储能联合问题' if optimize_declaration
            else '当前优化情景的固定申报储能问题'),
        within_service_budget=perf_counter()-started<=cfg['time_limit_seconds'])
    return dict(raw_solution=raw,optimization=opt,settings=cfg)


# Public compatibility name. Keep this next to the implementation so there is
# one storage model and one place to document its contract.
solve_storage_milp = dispatch
