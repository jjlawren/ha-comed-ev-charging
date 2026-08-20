"""Offline unit tests for the pure analytics."""

from __future__ import annotations

import pytest

from custom_components.comed_ev.analytics import suggest_thresholds


def test_empty_sample_yields_zeros():
    s = suggest_thresholds([])
    assert s.price_floor == 0.0
    assert s.price_ceiling == 0.0
    assert s.sample_size == 0


def test_single_sample():
    s = suggest_thresholds([7.0])
    assert s.price_floor == 7.0
    assert s.price_ceiling == 7.0
    assert s.sample_size == 1


def test_percentiles_on_skewed_distribution():
    # Right-skewed: many cheap points, a long spike tail to ~59.
    prices = [1, 2, 2, 3, 3, 3, 4, 4, 5, 6, 8, 12, 20, 35, 59]
    s = suggest_thresholds(prices, floor_pct=25, ceiling_pct=90)
    assert s.price_floor < s.price_ceiling
    assert 2 <= s.price_floor <= 4  # 25th pct lands in the cheap band
    # 90th pct is high-but-real; the extreme spike tail stays above it.
    assert s.price_ceiling < 59
    assert s.price_ceiling >= 12


def test_spike_tail_stays_above_ceiling():
    prices = [2] * 90 + [50] * 10  # 10% spikes
    s = suggest_thresholds(prices, ceiling_pct=90)
    assert s.price_ceiling < 50  # the spikes sit above the ceiling -> suppressed


def test_window_days_recorded():
    s = suggest_thresholds([1, 2, 3], window_days=14)
    assert s.window_days == 14


def test_custom_percentile_points():
    prices = list(range(101))  # 0..100
    s = suggest_thresholds(prices, floor_pct=10, ceiling_pct=95)
    assert s.price_floor == pytest.approx(10.0)
    assert s.price_ceiling == pytest.approx(95.0)
