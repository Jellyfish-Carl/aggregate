# Project Structure

The project is organized around the application boundaries in the reference
framework. Production code lives in the top-level packages below.

| Directory | Responsibility | Main modules |
| --- | --- | --- |
| `config` | Runtime defaults and package-level settings | `settings.py` |
| `constants` | Shared time-grid constants and transformations | `timegrid.py` |
| `data_objects` | Domain entities and stochastic trajectory objects | `domain.py`, `scenario.py` |
| `inputs` | Forecast construction and mock market input adapters | `load_forecast.py`, `mockdata.py` |
| `problem_solver` | Generic MILP infrastructure and L1/L2/L3 solvers | `milp.py`, `l1_contract_milp.py`, `l2_rolling_threshold.py`, `l3_milp.py` |
| `service` | Portfolio orchestration, simulation, and HTTP API | `trading.py`, `simulator.py`, `server.py` |
| `outputs` | Settlement and retail calculation outputs | `settlement.py`, `retail.py` |
| `utils` | Reusable deterministic scenario helpers | `mock_scenario.py` |

## Dependency Direction

`inputs`, `constants`, and `data_objects` provide the base layer. The solver
layer consumes those values, `service` orchestrates solver calls, and `outputs`
calculates customer-facing financial results. `service.server` is the only HTTP
entry point. This keeps framework and business logic out of the transport layer.

## Entry Point

`service/__main__.py` is the command-line entry point. Run it with
`python3 -m service serve`, or install the project and use `mpc-demo`.
