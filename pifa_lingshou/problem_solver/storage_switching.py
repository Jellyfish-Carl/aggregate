"""Three-state storage transitions, shared by optimization and cash ledgers."""
EPS = 1e-7
SWITCHING_DEFAULTS = dict(startup_cost_yuan=1., shutdown_cost_yuan=1.,
                          minimum_active_power_ratio=.01, initial_mode=0)


def validate_switching(cfg):
    from math import isfinite
    for key in ('startup_cost_yuan', 'shutdown_cost_yuan', 'minimum_active_power_ratio'):
        if not isinstance(cfg[key], (int, float)) or not isfinite(cfg[key]) or cfg[key] < 0:
            raise ValueError(key+'必须为非负有限数')
    if not 0 < cfg['minimum_active_power_ratio'] <= 1:
        raise ValueError('minimum_active_power_ratio须在(0,1]，避免零功率假开机')
    if cfg['initial_mode'] not in (-1, 0, 1):
        raise ValueError('initial_mode须为-1放电、0停机或1充电')


def add_switching(model, charges, discharges, cfg, fixed_until=0, prior=None, prefix=''):
    """Add exact starts/stops. Historical modes come from executed energy.

    Minimum active power applies to future decisions only, never retroactively
    to telemetry. No fictional shutdown is added at the horizon boundary.
    """
    validate_switching(cfg)
    costs = []
    previous = None
    histories = [[], []]
    recent_stops = [[], []]
    for j, (charge, discharge) in enumerate(zip(charges, discharges)):
        modes = [model.add_var(f'{prefix}storage_{direction}_on_{j}', upper=1, integer=True)
                 for direction in ('charge', 'discharge')]
        model.add_constraint({mode: 1 for mode in modes}, upper=1)
        period_costs = {}
        for i, (direction, q, mode, cap, initial) in enumerate(zip(
                ('charge', 'discharge'), (charge, discharge), modes,
                (cfg['maximum_charge_mwh'], cfg['maximum_discharge_mwh']),
                (int(cfg['initial_mode'] == 1), int(cfg['initial_mode'] == -1)))):
            model.add_constraint({q: 1, mode: -cap}, upper=0)
            if j < fixed_until:
                on = int(prior[direction+'_mwh'][j] > EPS)
                model.add_constraint({mode: 1}, lower=on, upper=on)
            else:
                model.add_constraint({q: 1, mode: -cap*cfg['minimum_active_power_ratio']}, lower=0)
                if cap == 0:
                    model.add_constraint({mode: 1}, upper=0)
            start = model.add_var(f'{prefix}storage_{direction}_start_{j}', upper=1)
            stop = model.add_var(f'{prefix}storage_{direction}_stop_{j}', upper=1)
            # Positive event costs make these lower bounds tight at optimum:
            # start >= on[t] - on[t-1], stop >= on[t-1] - on[t].  Keeping
            # events continuous avoids two extra binary columns per period;
            # binary mode variables still enforce the physical state.
            prior_mode = initial if previous is None else previous[i]
            model.add_constraint({start: 1, mode: -1, **({prior_mode: 1} if previous is not None else {})}, lower=-initial if previous is None else 0)
            model.add_constraint({stop: 1, mode: 1, **({prior_mode: -1} if previous is not None else {})}, lower=initial if previous is None else 0)
            period_costs[start] = cfg['startup_cost_yuan']
            period_costs[stop] = cfg['shutdown_cost_yuan']
            histories[i].append((mode,start))
            # A start commits this direction for the supplied minimum dwell.
            # Starts near the end only constrain the visible horizon; the
            # remaining commitment is persisted for the next rolling run.
            for _,past_start in histories[i][max(0,j-cfg.get('minimum_on_periods',1)+1):j]:
                if j>=fixed_until: model.add_constraint({mode:1,past_start:-1},lower=0)
            recent_stops[i].append((j,stop))
        costs.append(period_costs)
        previous = modes
    # A stop at t requires t,...,t+cooldown-1 idle (both directions).
    # Add symmetrically after both modes exist to avoid direction bias.
    cooldown=cfg.get('cooldown_periods',0)
    for direction_stops in recent_stops:
      for when,stop in direction_stops:
        for t in range(max(fixed_until,when+1),min(len(charges),when+cooldown+1)):
            model.add_constraint({f'{prefix}storage_charge_on_{t}':1,stop:1},upper=1)
            model.add_constraint({f'{prefix}storage_discharge_on_{t}':1,stop:1},upper=1)
    return costs


def switching_ledger(charges, discharges, settings):
    """Reprice physical transitions independently, with legacy zero-fee fallback."""
    previous = settings.get('initial_mode', 0)
    rows = []
    for charge, discharge in zip(charges, discharges):
        if charge > EPS and discharge > EPS:
            raise ValueError('储能不能同时充放电')
        mode = 1 if charge > EPS else -1 if discharge > EPS else 0
        start = int(mode != 0 and mode != previous)
        stop = int(previous != 0 and mode != previous)
        rows.append(dict(mode=mode, startup_count=start, shutdown_count=stop,
                         switching_cost_yuan=start*settings.get('startup_cost_yuan', 0.)
                         +stop*settings.get('shutdown_cost_yuan', 0.)))
        previous = mode
    return dict(periods=rows, startup_count=sum(r['startup_count'] for r in rows),
                shutdown_count=sum(r['shutdown_count'] for r in rows), terminal_mode=previous,
                switching_cost_yuan=sum(r['switching_cost_yuan'] for r in rows))
