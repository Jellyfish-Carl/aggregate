from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from pifa_lingshou.data_objects.domain import Action, ActionType, EventType, SolveResult


@dataclass(frozen=True)
class SolverRequest:
    event_type: EventType
    risk_lambda: float
    expected_cost: float
    cvar95: float
    action_type: ActionType
    label: str
    quantity_mwh: float
    reason: str


class SolverBackend(Protocol):
    def solve(self, request: SolverRequest) -> SolveResult:
        ...


class DiagnosticPolicyAdapter:
    """Non-optimizing adapter available only to explicit diagnostic callers."""

    backend_name = "DIAGNOSTIC_POLICY_NOT_A_SOLVER"

    def solve(self, request: SolverRequest) -> SolveResult:
        objective = (
            (1.0 - request.risk_lambda) * request.expected_cost
            + request.risk_lambda * request.cvar95
        )
        action = Action(
            action_id="ACTION-" + request.event_type.value,
            action_type=request.action_type,
            event_id="DEMO-" + request.event_type.value,
            label=request.label,
            quantity_mwh=request.quantity_mwh,
            status="SOLVED",
            reason=request.reason,
        )
        return SolveResult(
            run_id="RUN-202609-DIAGNOSTIC",
            event_id="DEMO-" + request.event_type.value,
            status="DIAGNOSTIC_POLICY_ONLY",
            expected_cost=request.expected_cost,
            cvar95=request.cvar95,
            objective=objective,
            mip_gap=None,
            actions=[action],
            diagnostics=["动作由外部预测、报价和状态计算后传入；本适配器不再硬编码电量。"],
            backend=self.backend_name,
        )
