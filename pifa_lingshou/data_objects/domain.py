from __future__ import annotations

from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any, Dict, List, Mapping, Optional, Sequence


class Layer(str, Enum):
    LAYER_1 = "1"
    LAYER_2 = "2"
    LAYER_3 = "3"
    SETTLEMENT = "SETTLEMENT"


class EventType(str, Enum):
    ANNUAL = "ANNUAL"
    MONTHLY = "MONTHLY"
    TEN_DAY = "TEN_DAY"
    D3 = "D-3"
    D2 = "D-2"
    D1 = "D-1"
    D1_PRICE = "D-1_PRICE"
    REAL_TIME = "REAL_TIME"
    MONTH_END = "MONTH_END"


class DataStatus(str, Enum):
    FORECAST = "FORECAST"
    OPERATIONAL_ESTIMATE = "OPERATIONAL_ESTIMATE"
    DAILY_PRELIMINARY = "DAILY_PRELIMINARY"
    MONTHLY_OFFICIAL = "MONTHLY_OFFICIAL"

    @property
    def rank(self) -> int:
        return {
            DataStatus.FORECAST: 0,
            DataStatus.OPERATIONAL_ESTIMATE: 1,
            DataStatus.DAILY_PRELIMINARY: 2,
            DataStatus.MONTHLY_OFFICIAL: 3,
        }[self]


class ActionType(str, Enum):
    TRADE = "TRADE"
    DAY_AHEAD = "DAY_AHEAD"
    RESOURCE = "RESOURCE"


@dataclass(frozen=True)
class DecisionEvent:
    event_id: str
    layer: Layer
    event_type: EventType
    source_type: str
    as_of: str
    delivery_set: Sequence[str]
    input_version: str

    def validate(self) -> List[str]:
        errors: List[str] = []
        if not self.event_id:
            errors.append("event_id 不能为空")
        if self.source_type != "MOCK":
            errors.append("首版事件必须显式标记 source_type=MOCK")
        if "+08:00" not in self.as_of:
            errors.append("as_of 必须携带 Asia/Shanghai 时区")
        if len(set(self.delivery_set)) != len(self.delivery_set):
            errors.append("delivery_set 存在重复时段")
        return errors


@dataclass(frozen=True)
class ContractFill:
    fill_id: str
    product_class: str
    event_id: str
    side: str
    signed_quantity: float
    price: float
    fee: float
    delivery_curve: Mapping[str, float]
    assessment_base_eligible: bool
    status: str = "POSTED"
    equivalent_delivery_days: float = 1.0

    def validate(self) -> List[str]:
        errors: List[str] = []
        if self.side not in {"BUY", "SELL"}:
            errors.append("side 只能为 BUY 或 SELL")
        if self.side == "BUY" and self.signed_quantity < 0:
            errors.append("BUY 的 signed_quantity 必须非负")
        if self.side == "SELL" and self.signed_quantity > 0:
            errors.append("SELL 的 signed_quantity 必须非正")
        if self.equivalent_delivery_days <= 0:
            errors.append("equivalent_delivery_days 必须大于 0")
        curve_total = sum(self.delivery_curve.values())
        if abs(curve_total - self.signed_quantity) > 1e-6:
            errors.append("delivery_curve 分时量之和必须等于 signed_quantity")
        return errors


@dataclass
class MpcState:
    state_version: int
    event_id: str
    current_interval: Optional[str]
    posted_contracts: List[ContractFill] = field(default_factory=list)
    declaration_curve: Dict[str, float] = field(default_factory=dict)
    accepted_curve: Dict[str, float] = field(default_factory=dict)
    real_time_buy_curve: Dict[str, float] = field(default_factory=dict)
    real_time_sell_curve: Dict[str, float] = field(default_factory=dict)
    charge_curve: Dict[str, float] = field(default_factory=dict)
    discharge_curve: Dict[str, float] = field(default_factory=dict)
    locked_plan_ids: List[str] = field(default_factory=list)
    soc_mwh: float = 10.0
    previous_mode: str = "idle"
    charge_starts_used: int = 0
    discharge_starts_used: int = 0
    month_to_date_energy_mwh: float = 0.0
    realized_cost_yuan: float = 0.0
    forecast_snapshot_id: str = "snapshot-initial"
    forecast_version: str = "v1"
    applied_source_ids: List[str] = field(default_factory=list)

    def apply_fill(self, fill: ContractFill, before_version: int) -> bool:
        if before_version != self.state_version:
            raise ValueError("STALE_STATE_VERSION")
        if fill.fill_id in self.applied_source_ids:
            return False
        errors = fill.validate()
        if errors:
            raise ValueError("; ".join(errors))
        self.posted_contracts.append(fill)
        self.applied_source_ids.append(fill.fill_id)
        self.state_version += 1
        return True

    def apply_measurement(
        self,
        measurement_id: str,
        soc_mwh: float,
        incoming_status: DataStatus,
        current_status: DataStatus,
        before_version: int,
    ) -> bool:
        if before_version != self.state_version:
            raise ValueError("STALE_STATE_VERSION")
        if measurement_id in self.applied_source_ids:
            return False
        if incoming_status.rank < current_status.rank:
            return False
        self.soc_mwh = soc_mwh
        self.applied_source_ids.append(measurement_id)
        self.state_version += 1
        return True


@dataclass(frozen=True)
class Action:
    action_id: str
    action_type: ActionType
    event_id: str
    label: str
    quantity_mwh: float
    status: str
    reason: str


@dataclass(frozen=True)
class CostBreakdown:
    energy: float
    trade_fee: float
    day_ahead_recovery: float
    annual_recovery: float
    over_profit_recovery: float
    curve_adjustment: float
    adjustment_energy: float
    external_items: float
    flex_impact: float
    inventory_value: float

    @property
    def wholesale_total(self) -> float:
        return (
            self.energy
            + self.trade_fee
            + self.day_ahead_recovery
            + self.annual_recovery
            + self.over_profit_recovery
            + self.curve_adjustment
            + self.adjustment_energy
            + self.external_items
        )

    @property
    def economic_total(self) -> float:
        return self.wholesale_total + self.flex_impact + self.inventory_value

    def to_dict(self) -> Dict[str, float]:
        result = asdict(self)
        result["wholesale_total"] = self.wholesale_total
        result["economic_total"] = self.economic_total
        return result


@dataclass(frozen=True)
class SolveResult:
    run_id: str
    event_id: str
    status: str
    expected_cost: float
    cvar95: float
    objective: float
    mip_gap: Optional[float]
    actions: Sequence[Action]
    diagnostics: Sequence[str]
    backend: str

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["actions"] = [asdict(item) for item in self.actions]
        return payload
