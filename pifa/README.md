# Aggregate MPC Demo

Three-layer stochastic MPC demonstration for an electricity retail aggregate.

## Structure

The code follows a layered application layout:

- `config`: runtime defaults.
- `constants`: shared grids and constants.
- `data_objects`: domain and scenario objects.
- `inputs`: forecasts and market data adapters.
- `problem_solver`: generic MILP infrastructure plus L1, L2, and L3 solvers.
- `service`: portfolio orchestration, simulation, and HTTP API.
- `outputs`: settlement and retail calculations.
- `utils`: common scenario helpers.

See `docs/architecture.md` for the dependency direction and migration notes.

## Run

Install the declared dependencies, then start the local UI:

```bash
python3 -m pip install -e ".[dev]"
python3 -m service serve
```

The installed `mpc-demo` command invokes the same entry point. Import from the
layer that owns the behavior, such as `service.simulator` or
`problem_solver.l3_milp`.

启动后可访问 `http://127.0.0.1:8765/configurator/` 打开集合体定义配置器。该页面独立于仿真看板，支持编辑交易渠道、资产映射、主体资产有效性，并联动生成最细粒度集合体组合。
