from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping, Sequence


@dataclass(frozen=True)
class RetailMarketConfig:
    pricing_source: str = "MOCK_ASSUMPTION"
    package_type: str = "LINKED"
    annual_weight: float = 0.70
    monthly_weight: float = 0.20
    spot_weight: float = 0.10
    service_fee_yuan_per_mwh: float = 18.0
    fixed_price_yuan_per_mwh: float = 455.0
    share_ratio: float = 0.50
    cap_price_yuan_per_mwh: float = 490.0

    def validate(self) -> None:
        if self.pricing_source not in {"MOCK_ASSUMPTION", "USER_INPUT"}:
            raise ValueError("pricing_source 只能为 MOCK_ASSUMPTION 或 USER_INPUT")
        if self.package_type not in {"LINKED", "FIXED", "SHARE"}:
            raise ValueError("package_type 只能为 LINKED、FIXED 或 SHARE")
        if abs(self.annual_weight + self.monthly_weight + self.spot_weight - 1.0) > 1e-9:
            raise ValueError("零售参考价权重之和必须为 1")
        if not 0.0 <= self.share_ratio <= 1.0:
            raise ValueError("share_ratio 必须位于 [0, 1]")

    def to_dict(self) -> dict:
        return asdict(self)


def retail_reference_curve(
    annual_prices: Sequence[float],
    monthly_prices: Sequence[float],
    spot_prices: Sequence[float],
    config: RetailMarketConfig,
) -> list:
    config.validate()
    if not (len(annual_prices) == len(monthly_prices) == len(spot_prices)):
        raise ValueError("年度、月度、现货价格曲线长度必须一致")
    return [
        config.annual_weight * annual
        + config.monthly_weight * monthly
        + config.spot_weight * spot
        for annual, monthly, spot in zip(annual_prices, monthly_prices, spot_prices)
    ]


def load_weighted_price(load_curve: Sequence[float], price_curve: Sequence[float]) -> float:
    if len(load_curve) != len(price_curve):
        raise ValueError("负荷和价格曲线长度必须一致")
    denominator = sum(load_curve)
    if denominator <= 0:
        raise ValueError("客户结算电量必须大于 0")
    return sum(load * price for load, price in zip(load_curve, price_curve)) / denominator


def retail_settlement(
    load_curve: Sequence[float],
    annual_prices: Sequence[float],
    monthly_prices: Sequence[float],
    spot_prices: Sequence[float],
    config: RetailMarketConfig,
) -> Mapping[str, object]:
    reference_curve = retail_reference_curve(annual_prices, monthly_prices, spot_prices, config)
    reference_price = load_weighted_price(load_curve, reference_curve)
    if config.package_type == "LINKED":
        uncapped_price = reference_price + config.service_fee_yuan_per_mwh
    elif config.package_type == "FIXED":
        uncapped_price = config.fixed_price_yuan_per_mwh
    else:
        uncapped_price = (
            config.share_ratio * config.fixed_price_yuan_per_mwh
            + (1.0 - config.share_ratio) * (reference_price + config.service_fee_yuan_per_mwh)
        )
    settled_price = min(uncapped_price, config.cap_price_yuan_per_mwh)
    energy = sum(load_curve)
    return {
        "config": config.to_dict(),
        "pricing_status": (
            "MOCK_ASSUMPTION_NOT_CUSTOMER_CONTRACT"
            if config.pricing_source == "MOCK_ASSUMPTION"
            else "USER_CONFIGURED_CUSTOMER_CONTRACT"
        ),
        "reference_curve": [round(value, 3) for value in reference_curve],
        "reference_price_yuan_per_mwh": round(reference_price, 3),
        "uncapped_price_yuan_per_mwh": round(uncapped_price, 3),
        "settled_price_yuan_per_mwh": round(settled_price, 3),
        "cap_applied": uncapped_price > config.cap_price_yuan_per_mwh,
        "energy_mwh": round(energy, 3),
        "revenue_yuan": round(energy * settled_price, 2),
    }
