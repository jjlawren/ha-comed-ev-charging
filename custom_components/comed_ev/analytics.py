"""Pure analytics — derive suggested price thresholds from a price distribution.

No Home Assistant imports; offline-testable. Prices are in ¢/kWh. The observed
ComEd distribution is right-skewed (median low, long spike tail), so a low
percentile lands in the cheap overnight band and a high-but-sub-tail percentile
gives a realistic "max willing to pay" that still suppresses charging on spikes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from math import ceil, floor


@dataclass(frozen=True)
class ThresholdSuggestion:
    """A data-driven floor/ceiling pair with the sample it was drawn from."""

    price_floor: float
    price_ceiling: float
    sample_size: int
    window_days: int


def suggest_thresholds(
    prices: Iterable[float],
    *,
    floor_pct: float = 25,
    ceiling_pct: float = 90,
    window_days: int = 30,
) -> ThresholdSuggestion:
    """Suggest `price_floor`/`price_ceiling` from a price sample.

    `floor_pct` defaults to the 25th percentile (genuinely cheap) and
    `ceiling_pct` to the 90th (high but real, below the rare spike tail).
    An empty sample yields zeros.
    """
    ordered = sorted(prices)
    return ThresholdSuggestion(
        price_floor=_percentile(ordered, floor_pct),
        price_ceiling=_percentile(ordered, ceiling_pct),
        sample_size=len(ordered),
        window_days=window_days,
    )


def _percentile(ordered: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted list (empty -> 0.0)."""
    if not ordered:
        return 0.0
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low, high = floor(rank), ceil(rank)
    if low == high:
        return ordered[low]
    frac = rank - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac
