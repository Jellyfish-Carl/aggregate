from __future__ import annotations

from dataclasses import dataclass
from math import inf
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
    pass


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


@dataclass(frozen=True)
class LinearPricingResult:
    status: str
    objective: float
    values: Mapping[str, float]
    row_marginals: Sequence[float]
    message: str
    solve_seconds: float
    backend: str = "scipy.optimize.linprog / HiGHS"


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
        time_limit_seconds: float = 9.0,
    ) -> LinearPricingResult:
        """Resolve the MILP as an LP after fixing every integer decision.

        The resulting equality-row marginals are used as explainable pricing
        signals. They are not MILP duals; they are duals of the local LP around
        the chosen integer storage mode.
        """

        if not self.available() or linprog is None:
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

        started = perf_counter()
        result = linprog(
            c=np.asarray(self._objective, dtype=float),
            A_ub=matrix(upper_rows) if upper_rows else None,
            b_ub=np.asarray(upper_rhs, dtype=float) if upper_rows else None,
            A_eq=matrix(equality_rows) if equality_rows else None,
            b_eq=np.asarray(equality_rhs, dtype=float) if equality_rows else None,
            bounds=list(zip(lower, upper)),
            method="highs",
            options={
                "presolve": True,
                "time_limit": float(time_limit_seconds),
            },
        )
        solve_seconds = perf_counter() - started
        if not result.success or result.x is None:
            raise MilpSolveError("PRICING_LP_FAILED: %s" % result.message)
        row_marginals = [0.0] * len(self._rows)
        if equality_rows:
            for source, marginal in zip(equality_source, result.eqlin.marginals):
                row_marginals[source] += float(marginal)
        if upper_rows:
            for (source, sign), marginal in zip(upper_source, result.ineqlin.marginals):
                row_marginals[source] += sign * float(marginal)
        return LinearPricingResult(
            status="OPTIMAL",
            objective=float(result.fun),
            values={
                name: float(result.x[index])
                for index, name in enumerate(self._names)
            },
            row_marginals=tuple(row_marginals),
            message=str(result.message),
            solve_seconds=solve_seconds,
        )

    def solve(
        self,
        time_limit_seconds: float = 9.0,
        mip_relative_gap: float = 1e-3,
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
        constraints = LinearConstraint(
            matrix,
            np.asarray(self._row_lower, dtype=float),
            np.asarray(self._row_upper, dtype=float),
        )
        started = perf_counter()
        result = milp(
            c=np.asarray(self._objective, dtype=float),
            integrality=np.asarray(self._integrality, dtype=int),
            bounds=Bounds(
                np.asarray(self._lower, dtype=float),
                np.asarray(self._upper, dtype=float),
            ),
            constraints=constraints,
            options={
                "disp": False,
                "presolve": True,
                "time_limit": float(time_limit_seconds),
                "mip_rel_gap": float(mip_relative_gap),
            },
        )
        solve_seconds = perf_counter() - started
        status_map = {
            0: "OPTIMAL",
            1: "LIMIT_REACHED",
            2: "INFEASIBLE",
            3: "UNBOUNDED",
            4: "SOLVER_ERROR",
        }
        status = status_map.get(int(result.status), "UNKNOWN")
        if result.x is None or status not in {"OPTIMAL", "LIMIT_REACHED"}:
            raise MilpSolveError("%s: %s" % (status, result.message))
        values = {
            name: float(result.x[index])
            for index, name in enumerate(self._names)
        }
        raw_gap = getattr(result, "mip_gap", None)
        raw_nodes = getattr(result, "mip_node_count", None)
        return MilpSolveResult(
            status=status,
            objective=float(result.fun),
            values=values,
            mip_gap=None if raw_gap is None else float(raw_gap),
            node_count=None if raw_nodes is None else int(raw_nodes),
            message=str(result.message),
            variable_count=variable_count,
            binary_count=sum(self._integrality),
            constraint_count=len(self._rows),
            solve_seconds=solve_seconds,
            time_limit_seconds=float(time_limit_seconds),
        )
