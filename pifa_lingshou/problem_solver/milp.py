from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from math import inf
import multiprocessing as mp
import os
import re
import tempfile
from time import perf_counter
from typing import Dict, List, Mapping, Optional, Sequence

try:
    import numpy as np
    from scipy.optimize import Bounds, LinearConstraint, linprog, milp
    from scipy.sparse import coo_matrix
except ImportError as exc:  # pragma: no cover - depends on the local runtime
    np = None
    Bounds = LinearConstraint = linprog = milp = coo_matrix = None
    _IMPORT_ERROR: Optional[ImportError] = exc
else:
    _IMPORT_ERROR = None


class MilpBackendUnavailable(RuntimeError):
    pass


class MilpSolveError(RuntimeError):
    def __init__(self, message, solver_metrics=None):
        super().__init__(message)
        self.solver_metrics = solver_metrics or {}


@dataclass(frozen=True)
class MilpSolveResult:
    status: str
    objective: float
    values: Mapping[str, float]
    mip_gap: Optional[float]
    node_count: Optional[int]
    message: str
    variable_count: int
    binary_count: int
    constraint_count: int
    solve_seconds: float
    time_limit_seconds: float
    backend: str = "scipy.optimize.milp / HiGHS"
    dual_bound: Optional[float] = None


@dataclass(frozen=True)
class LinearPricingResult:
    status: str
    objective: float
    values: Mapping[str, float]
    row_marginals: Sequence[float]
    message: str
    solve_seconds: float
    backend: str = "scipy.optimize.linprog / HiGHS"


MAX_SOLVER_WALL_TIME_SECONDS = 60.0
WATCHDOG_TERMINATION_MARGIN_SECONDS = 30.0


def _milp_worker(connection, c, integrality, lower, upper, matrix, row_lower, row_upper, gap, limit, log_path):
    try:
        log_stream = open(log_path, "a", buffering=1)
        os.dup2(log_stream.fileno(), 1)
        result = milp(
            c=c,
            integrality=integrality,
            bounds=Bounds(lower, upper),
            constraints=LinearConstraint(matrix, row_lower, row_upper),
            options={"disp": True, "presolve": True, "time_limit": float(limit), "mip_rel_gap": float(gap)},
        )
        connection.send((True, {
            "status": int(result.status),
            "objective": None if result.fun is None else float(result.fun),
            "values": None if result.x is None else result.x.tolist(),
            "message": str(result.message),
            "mip_gap": None if getattr(result, "mip_gap", None) is None else float(result.mip_gap),
            "node_count": None if getattr(result, "mip_node_count", None) is None else int(result.mip_node_count),
            "dual_bound": None if getattr(result, "mip_dual_bound", None) is None else float(result.mip_dual_bound),
        }))
    except BaseException as exc:
        connection.send((False, type(exc).__name__, str(exc)))
    finally:
        connection.close()


def _linprog_worker(connection, c, a_ub, b_ub, a_eq, b_eq, bounds, limit):
    try:
        result = linprog(
            c=c, A_ub=a_ub, b_ub=b_ub, A_eq=a_eq, b_eq=b_eq,
            bounds=bounds, method="highs",
            options={"presolve": True, "time_limit": float(limit)},
        )
        connection.send((True, {
            "success": bool(result.success), "objective": result.fun,
            "values": None if result.x is None else result.x.tolist(),
            "message": str(result.message),
            "equality_marginals": result.eqlin.marginals.tolist(),
            "inequality_marginals": result.ineqlin.marginals.tolist(),
        }))
    except BaseException as exc:
        connection.send((False, type(exc).__name__, str(exc)))
    finally:
        connection.close()


def _run_isolated(worker, args, time_limit_seconds: float):
    limit = min(float(time_limit_seconds), MAX_SOLVER_WALL_TIME_SECONDS)
    if limit <= 0:
        raise ValueError("求解时限必须为正数")
    worker_limit = limit
    context = mp.get_context("spawn")
    parent, child = context.Pipe(duplex=False)
    log_handle = tempfile.NamedTemporaryFile(prefix="pifa_highs_", suffix=".log", delete=False)
    log_path = log_handle.name
    log_handle.close()
    worker_args = (child, *args, worker_limit)
    if worker is _milp_worker:
        worker_args += (log_path,)
    process = context.Process(target=worker, args=worker_args)
    started = perf_counter()
    process.start()
    child.close()
    response = None
    log_offset = 0
    pending_log = ""
    try:
        deadline = started + limit + WATCHDOG_TERMINATION_MARGIN_SECONDS
        while response is None:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as stream:
                    stream.seek(log_offset)
                    chunk = stream.read()
                    log_offset = stream.tell()
                if chunk:
                    lines = (pending_log + chunk).splitlines(keepends=True)
                    pending_log = ""
                    if lines and not lines[-1].endswith(("\n", "\r")):
                        pending_log = lines.pop()
                    for line in lines:
                        timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
                        print("%s HiGHS | %s" % (timestamp, line.rstrip()), flush=True)
            except OSError:
                pass
            remaining = deadline - perf_counter()
            if remaining <= 0:
                process.terminate()
                process.join(timeout=0.5)
                if process.is_alive():
                    process.kill()
                    process.join()
                raise MilpSolveError(
                    "TIME_LIMIT_REACHED: 外部 watchdog 已在 %.3f 秒终止求解子进程（HiGHS时限 %.3f 秒，清理宽限 %.3f 秒）"
                    % (perf_counter() - started, limit, WATCHDOG_TERMINATION_MARGIN_SECONDS)
                )
            if parent.poll(min(0.05, remaining)):
                response = parent.recv()
            elif not process.is_alive():
                if parent.poll():
                    response = parent.recv()
                else:
                    raise MilpSolveError(
                        "SOLVER_PROCESS_FAILED: 求解子进程异常退出，exit code %s"
                        % process.exitcode
                    )
        process.join(timeout=0.5)
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as stream:
                stream.seek(log_offset)
                pending_log += stream.read()
            for line in pending_log.splitlines():
                timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
                print("%s HiGHS | %s" % (timestamp, line.rstrip()), flush=True)
            pending_log = ""
        except OSError:
            pass
        if pending_log:
            timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
            print("%s HiGHS | %s" % (timestamp, pending_log.rstrip()), flush=True)
        if worker is _milp_worker:
            try:
                with open(log_path, "r", encoding="utf-8", errors="replace") as stream:
                    log_text = stream.read()
                for key, label in (("primal_bound", "Primal bound"),
                                   ("dual_bound", "Dual bound"),
                                   ("mip_gap", "Gap"),
                                   ("node_count", "Nodes")):
                    matches = re.findall(r"^\s*" + re.escape(label) + r"\s+([^\s(]+)", log_text, re.MULTILINE)
                    if matches and response[0] and response[1].get(key) is None:
                        value = matches[-1]
                        if key == "node_count":
                            response[1][key] = int(value)
                        elif value.lower() in {"inf", "+inf", "-inf"}:
                            response[1][key] = None
                            response[1][key + "_status"] = "INFINITE"
                        else:
                            response[1][key] = float(value.rstrip('%')) / (100. if value.endswith('%') else 1.)
            except (OSError, ValueError, TypeError):
                pass
        elapsed = perf_counter() - started
        if not response[0]:
            raise MilpSolveError("SOLVER_PROCESS_FAILED: %s: %s" % (response[1], response[2]))
        return response[1], elapsed, worker_limit
    finally:
        parent.close()
        if process.is_alive():
            process.terminate()
            process.join(timeout=0.5)
            if process.is_alive():
                process.kill()
                process.join()
        try:
            os.unlink(log_path)
        except OSError:
            pass


class LinearMilp:
    """Small named-variable builder for SciPy's HiGHS MILP interface."""

    def __init__(self, name: str) -> None:
        self.name = name
        self._names: List[str] = []
        self._index: Dict[str, int] = {}
        self._objective: List[float] = []
        self._lower: List[float] = []
        self._upper: List[float] = []
        self._integrality: List[int] = []
        self._rows: List[Dict[int, float]] = []
        self._row_lower: List[float] = []
        self._row_upper: List[float] = []

    @staticmethod
    def available() -> bool:
        return milp is not None and np is not None

    @staticmethod
    def dependency_error() -> Optional[str]:
        return None if _IMPORT_ERROR is None else str(_IMPORT_ERROR)

    @property
    def variable_count(self) -> int:
        return len(self._names)

    @property
    def binary_count(self) -> int:
        return sum(self._integrality)

    @property
    def constraint_count(self) -> int:
        return len(self._rows)

    def dimensions(self) -> Mapping[str, int]:
        return {
            "variable_count": self.variable_count,
            "binary_count": self.binary_count,
            "constraint_count": self.constraint_count,
        }

    def add_var(
        self,
        name: str,
        lower: float = 0.0,
        upper: float = inf,
        objective: float = 0.0,
        integer: bool = False,
    ) -> str:
        if name in self._index:
            raise ValueError("变量重复: " + name)
        self._index[name] = len(self._names)
        self._names.append(name)
        self._lower.append(float(lower))
        self._upper.append(float(upper))
        self._objective.append(float(objective))
        self._integrality.append(1 if integer else 0)
        return name

    def set_objective(self, variable: str, coefficient: float) -> None:
        self._objective[self._index[variable]] = float(coefficient)

    def add_to_objective(self, variable: str, coefficient: float) -> None:
        self._objective[self._index[variable]] += float(coefficient)

    def add_constraint(
        self,
        coefficients: Mapping[str, float],
        lower: float = -inf,
        upper: float = inf,
    ) -> int:
        row: Dict[int, float] = {}
        for variable, coefficient in coefficients.items():
            if variable not in self._index:
                raise ValueError("未知变量: " + variable)
            if coefficient:
                row[self._index[variable]] = float(coefficient)
        self._rows.append(row)
        self._row_lower.append(float(lower))
        self._row_upper.append(float(upper))
        return len(self._rows) - 1

    def solve_fixed_integer_lp(
        self,
        integer_values: Mapping[str, float],
        time_limit_seconds: float = MAX_SOLVER_WALL_TIME_SECONDS,
    ) -> LinearPricingResult:
        """Resolve the MILP as an LP after fixing every integer decision.

        The resulting equality-row marginals are used as explainable pricing
        signals. They are not MILP duals; they are duals of the local LP around
        the chosen storage and trading direction modes.
        """

        if not self.available():
            raise MilpBackendUnavailable(
                "MILP/LP 后端不可用；请先安装 SciPy/HiGHS"
            )
        lower = list(self._lower)
        upper = list(self._upper)
        for index, is_integer in enumerate(self._integrality):
            if not is_integer:
                continue
            name = self._names[index]
            fixed = round(float(integer_values[name]))
            lower[index] = fixed
            upper[index] = fixed

        equality_rows: List[Dict[int, float]] = []
        equality_rhs: List[float] = []
        equality_source: List[int] = []
        upper_rows: List[Dict[int, float]] = []
        upper_rhs: List[float] = []
        upper_source: List[tuple] = []
        for row_index, (row, row_lower, row_upper) in enumerate(
            zip(self._rows, self._row_lower, self._row_upper)
        ):
            if row_lower == row_upper:
                equality_rows.append(row)
                equality_rhs.append(row_lower)
                equality_source.append(row_index)
                continue
            if row_upper < inf:
                upper_rows.append(row)
                upper_rhs.append(row_upper)
                upper_source.append((row_index, 1.0))
            if row_lower > -inf:
                upper_rows.append({key: -value for key, value in row.items()})
                upper_rhs.append(-row_lower)
                upper_source.append((row_index, -1.0))

        def matrix(rows: Sequence[Mapping[int, float]]):
            matrix_rows: List[int] = []
            matrix_columns: List[int] = []
            matrix_values: List[float] = []
            for row_index, row in enumerate(rows):
                for column_index, value in row.items():
                    matrix_rows.append(row_index)
                    matrix_columns.append(column_index)
                    matrix_values.append(value)
            return coo_matrix(
                (matrix_values, (matrix_rows, matrix_columns)),
                shape=(len(rows), len(self._names)),
                dtype=float,
            ).tocsc()

        result, solve_seconds, effective_limit = _run_isolated(
            _linprog_worker,
            (
                np.asarray(self._objective, dtype=float),
                matrix(upper_rows) if upper_rows else None,
                np.asarray(upper_rhs, dtype=float) if upper_rows else None,
                matrix(equality_rows) if equality_rows else None,
                np.asarray(equality_rhs, dtype=float) if equality_rows else None,
                list(zip(lower, upper)),
            ),
            time_limit_seconds,
        )
        if not result["success"] or result["values"] is None:
            raise MilpSolveError("PRICING_LP_FAILED: %s" % result["message"])
        row_marginals = [0.0] * len(self._rows)
        if equality_rows:
            for source, marginal in zip(equality_source, result["equality_marginals"]):
                row_marginals[source] += float(marginal)
        if upper_rows:
            for (source, sign), marginal in zip(upper_source, result["inequality_marginals"]):
                row_marginals[source] += sign * float(marginal)
        return LinearPricingResult(
            status="OPTIMAL",
            objective=float(result["objective"]),
            values={
                name: float(result["values"][index])
                for index, name in enumerate(self._names)
            },
            row_marginals=tuple(row_marginals),
            message=result["message"],
            solve_seconds=solve_seconds,
        )

    def solve(
        self,
        time_limit_seconds: float = MAX_SOLVER_WALL_TIME_SECONDS,
        mip_relative_gap: float = 0.03,
    ) -> MilpSolveResult:
        if not self.available():
            raise MilpBackendUnavailable(
                "MILP 后端不可用；请先执行 python3 -m pip install -e . 安装 SciPy/HiGHS"
            )
        variable_count = len(self._names)
        matrix_rows: List[int] = []
        matrix_columns: List[int] = []
        matrix_values: List[float] = []
        for row_index, row in enumerate(self._rows):
            for column_index, value in row.items():
                matrix_rows.append(row_index)
                matrix_columns.append(column_index)
                matrix_values.append(value)
        matrix = coo_matrix(
            (matrix_values, (matrix_rows, matrix_columns)),
            shape=(len(self._rows), variable_count),
            dtype=float,
        ).tocsc()
        solve_started = perf_counter()
        try:
            result, solve_seconds, effective_limit = _run_isolated(
                _milp_worker,
                (
                    np.asarray(self._objective, dtype=float),
                    np.asarray(self._integrality, dtype=int),
                    np.asarray(self._lower, dtype=float),
                    np.asarray(self._upper, dtype=float),
                    matrix,
                    np.asarray(self._row_lower, dtype=float),
                    np.asarray(self._row_upper, dtype=float),
                    float(mip_relative_gap),
                ),
                time_limit_seconds,
            )
        except MilpSolveError as exc:
            # A killed/failed worker cannot return its native result, but the
            # constructed model dimensions and measured waiting time are real.
            # Do not misreport those seconds as preprocessing or unknown sizes.
            exc.solver_metrics = {
                'variable_count': variable_count,
                'binary_count': sum(self._integrality),
                'constraint_count': len(self._rows),
                'solve_seconds': perf_counter() - solve_started,
                'time_limit_seconds': min(float(time_limit_seconds), MAX_SOLVER_WALL_TIME_SECONDS),
                'mip_gap': None,
                'gap_status': 'UNAVAILABLE_PROCESS_TERMINATED',
                **exc.solver_metrics,
            }
            raise
        status_map = {
            0: "OPTIMAL",
            1: "LIMIT_REACHED",
            2: "INFEASIBLE",
            3: "UNBOUNDED",
            4: "SOLVER_ERROR",
        }
        status = status_map.get(int(result["status"]), "UNKNOWN")
        if result["values"] is None or status not in {"OPTIMAL", "LIMIT_REACHED"}:
            objective = result.get("objective")
            if objective is None:
                objective = result.get("primal_bound")
            gap = result.get("mip_gap")
            dual_bound = result.get("dual_bound")
            metrics = dict(
                status=status,
                objective_yuan=(float(objective) if objective is not None and np.isfinite(objective) else None),
                dual_bound=(float(dual_bound) if dual_bound is not None and np.isfinite(dual_bound) else None),
                mip_gap=(float(gap) if gap is not None and np.isfinite(gap) else None),
                gap_status="INFINITE_NO_INCUMBENT" if result["values"] is None else "UNAVAILABLE",
                node_count=result.get("node_count"),
                variable_count=variable_count,
                binary_count=sum(self._integrality),
                constraint_count=len(self._rows),
                solve_seconds=solve_seconds,
                time_limit_seconds=effective_limit,
                backend="scipy.optimize.milp / HiGHS",
            )
            raise MilpSolveError("%s: %s" % (status, result["message"]), metrics)
        values = {
            name: float(result["values"][index])
            for index, name in enumerate(self._names)
        }
        raw_gap = result["mip_gap"]
        raw_nodes = result["node_count"]
        raw_bound = result["dual_bound"]
        return MilpSolveResult(
            status=status,
            objective=float(result["objective"]),
            values=values,
            mip_gap=None if raw_gap is None else float(raw_gap),
            node_count=None if raw_nodes is None else int(raw_nodes),
            message=result["message"],
            variable_count=variable_count,
            binary_count=sum(self._integrality),
            constraint_count=len(self._rows),
            solve_seconds=solve_seconds,
            time_limit_seconds=effective_limit,
            dual_bound=None if raw_bound is None or not np.isfinite(raw_bound) else float(raw_bound),
        )
