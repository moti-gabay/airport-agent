"""Scoring is the part of the system that must not surprise anyone: these tests pin the
bounds, the weights, the eligibility filter, the national-set normalization and determinism.
No database and no network: every fixture is built by hand.
"""

from __future__ import annotations

import pandas as pd
import pytest

from app import config as cfg
from app.scoring import scoring
from app.scoring.normalize import percentile_rank, rank_within, safe_ratio

DEFAULTS = {
    "name": "Test Airport", "municipality": "Testville", "state": "CA",
    "runway_count": 2, "longest_runway_ft": 9000,
    "dep_prev": 50_000, "pax_prev": 5_000_000, "months_prev": 12,
    "dep_curr": 52_000, "pax_curr": 5_200_000, "seats_curr": 6_400_000, "months_curr": 12,
    "intl_dep_curr": 2_000, "long_haul_dep_curr": 10_000,
    "allcargo_dep_curr": 0, "nonsched_dep_curr": 0,
    "otp_flights_scheduled": 30_000, "otp_flights_operated": 29_000,
    "otp_delayed15": 5_000, "otp_dep_delay_min_sum": 350_000,
    "otp_taxi_out_min_sum": 500_000, "otp_taxi_out_n": 29_000,
    "otp_cancelled": 300, "otp_nas_delay_min_sum": 40_000,
    "otp_cause_delay_min_sum": 180_000, "otp_cause_rows": 5_000, "otp_months": 6,
    "otp_period_start": "2025-01", "otp_period_end": "2025-06",
    "otp_prev_flights_operated": 28_000, "otp_prev_delayed15": 6_000,
    "otp_prev_dep_delay_min_sum": 380_000, "otp_prev_taxi_out_min_sum": 495_000,
    "otp_prev_taxi_out_n": 28_000,
}
PERIODS = {"t100_curr": "2025", "t100_growth": "2024 vs 2025", "otp": "2025-01..2025-06"}


def make_metrics(*overrides: dict) -> pd.DataFrame:
    """Build an airport_metrics-shaped frame; each override dict is one airport."""
    rows = []
    for i, o in enumerate(overrides):
        row = {"iata": f"A{i:02d}", **DEFAULTS, **o}
        rows.append(row)
    return pd.DataFrame(rows)


def spread(n: int = 12, **base) -> pd.DataFrame:
    """n airports that differ on every KPI input, so percentiles have something to rank."""
    return make_metrics(*[
        {"iata": f"S{i:02d}",
         "dep_curr": 20_000 + 5_000 * i,
         "pax_curr": 2_000_000 + 400_000 * i,
         "pax_prev": 2_000_000,
         "seats_curr": 3_000_000 + 400_000 * i,
         "long_haul_dep_curr": 1_000 + 1_500 * i,
         "runway_count": 1 + i % 4,
         "otp_delayed15": 2_000 + 400 * i,
         "otp_taxi_out_min_sum": 300_000 + 30_000 * i,
         **base}
        for i in range(n)
    ])


# --- normalization primitives -------------------------------------------------

def test_percentile_rank_is_bounded_and_monotonic():
    s = pd.Series([3.0, 1.0, 2.0, 10.0])
    pr = percentile_rank(s)
    assert pr.min() > 0 and pr.max() == 100.0
    assert pr.idxmax() == 3 and pr.idxmin() == 1
    assert list(pr.sort_values().index) == [1, 2, 0, 3]


def test_percentile_rank_ties_share_a_score():
    pr = percentile_rank(pd.Series([5.0, 5.0, 9.0]))
    assert pr[0] == pr[1] < pr[2]


def test_percentile_rank_ignores_nan():
    pr = percentile_rank(pd.Series([1.0, float("nan"), 2.0]))
    assert pd.isna(pr[1])
    assert pr[0] == 50.0 and pr[2] == 100.0


def test_rank_within_excludes_masked_rows_from_the_ranking():
    s = pd.Series([1.0, 2.0, 3.0, 400.0])
    mask = pd.Series([True, True, True, False])
    pr = rank_within(s, mask)
    assert pd.isna(pr[3])                     # outside the set: no score
    assert pr[2] == 100.0                     # the outlier did not push it down


def test_safe_ratio_handles_zero_denominator():
    r = safe_ratio(pd.Series([5.0, 5.0]), pd.Series([0.0, 2.0]))
    assert pd.isna(r[0]) and r[1] == 2.5


# --- configuration ------------------------------------------------------------

def test_weights_sum_to_one_and_cover_every_component():
    assert set(cfg.WEIGHTS) == set(scoring.COMPONENTS)
    assert sum(cfg.WEIGHTS.values()) == pytest.approx(1.0)
    assert sum(scoring.CONGESTION_MIX.values()) == pytest.approx(1.0)


# --- eligibility --------------------------------------------------------------

def test_small_airports_are_excluded_and_flagged():
    d = scoring.score(make_metrics(
        {"iata": "BIG", "dep_curr": 100_000},
        {"iata": "TINY", "dep_curr": cfg.MIN_ANNUAL_DEPARTURES - 1},
    )).set_index("iata")
    assert d.loc["BIG", "eligible"] and not d.loc["TINY", "eligible"]
    assert "below_min_departures" in d.loc["TINY", "flags"]
    assert pd.isna(d.loc["TINY", "composite_score"])


def test_airport_without_otp_data_is_excluded_but_keeps_its_t100_values():
    d = scoring.score(make_metrics(
        {"iata": "HAS"},
        {"iata": "NONE", "otp_flights_operated": 0, "otp_flights_scheduled": 0,
         "otp_delayed15": 0, "otp_taxi_out_n": 0},
    )).set_index("iata")
    assert not d.loc["NONE", "eligible"]
    assert "no_otp_data" in d.loc["NONE", "flags"]
    assert pd.isna(d.loc["NONE", "composite_score"])
    assert d.loc["NONE", "long_haul_share_pct"] > 0        # T-100 side still computed


def test_airport_without_runways_does_not_divide_by_zero():
    d = scoring.score(make_metrics({"iata": "NRW", "runway_count": 0})).set_index("iata")
    assert pd.isna(d.loc["NRW", "departures_per_runway"])
    assert "no_runway_data" in d.loc["NRW", "flags"]


# --- scores -------------------------------------------------------------------

def test_component_scores_and_composite_stay_within_bounds():
    d = scoring.score(spread())
    e = d[d["eligible"]]
    for col in [f"{c}_score" for c in scoring.COMPONENTS] + ["composite_score"]:
        assert e[col].min() >= 0.0 and e[col].max() <= 100.0


def test_composite_is_the_declared_weighted_sum():
    d = scoring.score(spread()).set_index("iata")
    row = d.loc["S05"]
    expected = sum(cfg.WEIGHTS[c] * row[f"{c}_score"] for c in scoring.COMPONENTS)
    assert row["composite_score"] == pytest.approx(expected)


def test_ineligible_airports_do_not_shift_the_percentiles():
    """The Flag B guarantee: scores are ranked over the eligible national set, and adding a
    huge ineligible airport must not move anyone else's score."""
    base = scoring.score(spread()).set_index("iata")
    plus = scoring.score(pd.concat([
        spread(),
        make_metrics({"iata": "ZZZ", "dep_curr": 5_000, "pax_curr": 90_000_000,
                      "long_haul_dep_curr": 4_900, "runway_count": 1}),
    ], ignore_index=True)).set_index("iata")
    for c in scoring.COMPONENTS:
        pd.testing.assert_series_equal(base[f"{c}_score"], plus.loc[base.index, f"{c}_score"])


def test_scoring_a_subset_is_not_the_way_to_rank_a_region():
    """Scores must come from the national frame, then be filtered. Scoring a two-airport
    frame on its own produces 50/100 for everyone, which is exactly the trap."""
    pair = ["S00", "S11"]
    national = scoring.score(spread()).set_index("iata")
    subset_only = scoring.score(spread().query("iata in @pair")).set_index("iata")

    # Ranked among themselves, two airports can only ever score 50 and 100.
    assert sorted(subset_only["capacity_pressure_score"]) == [50.0, 100.0]
    # Ranked nationally they do not, which is why tools filter after scoring, never before.
    assert sorted(national.loc[pair, "capacity_pressure_score"]) != [50.0, 100.0]


def test_rank_orders_by_composite_and_counts_the_eligible_set():
    d = scoring.score(spread()).set_index("iata")
    ordered = d.sort_values("composite_score", ascending=False)
    assert ordered["rank"].iloc[0] == 1
    assert (d["rank_of"] == d["eligible"].sum()).all()


def test_congestion_uses_delay_share_and_taxi_out_not_mean_delay():
    """Mean departure delay is explanatory only: changing it alone must not move the score."""
    a = scoring.score(spread()).set_index("iata")
    b = scoring.score(spread(otp_dep_delay_min_sum=9_000_000)).set_index("iata")
    pd.testing.assert_series_equal(a["congestion_score"], b["congestion_score"])
    assert (a["mean_dep_delay_min"] != b["mean_dep_delay_min"]).all()


# --- determinism --------------------------------------------------------------

def test_same_input_gives_identical_output():
    m = spread()
    pd.testing.assert_frame_equal(scoring.score(m), scoring.score(m))


def test_row_order_of_the_input_does_not_change_any_score():
    m = spread()
    shuffled = m.iloc[::-1].reset_index(drop=True)
    pd.testing.assert_frame_equal(scoring.score(m), scoring.score(shuffled))


# --- structured output --------------------------------------------------------

def test_score_dict_carries_raw_values_sources_and_confidence():
    d = scoring.score(spread()).set_index("iata", drop=False)
    out = scoring.to_score_dict(d.loc["S07"], PERIODS)
    assert set(out["components"]) == set(scoring.COMPONENTS)
    for name, block in out["components"].items():
        assert block["weight"] == cfg.WEIGHTS[name]
        assert block["raw"] and block["source"] and block["period"]
        assert 0.0 <= block["score"] <= 100.0
    assert out["confidence"]["level"] in {"high", "medium", "low"}
    assert out["confidence"]["reasons"]


def test_score_dict_is_json_safe_for_an_airport_with_missing_data():
    import json
    d = scoring.score(make_metrics(
        {"iata": "GAP", "otp_flights_operated": 0, "otp_taxi_out_n": 0, "runway_count": 0,
         "pax_prev": 0},
    )).set_index("iata", drop=False)
    out = scoring.to_score_dict(d.loc["GAP"], PERIODS)
    assert json.loads(json.dumps(out))["composite_score"] is None
    # Full T-100 coverage but no on-time data: the weakest input sets the level.
    assert out["confidence"]["level"] == "low"


def test_confidence_falls_with_sample_size():
    d = scoring.score(make_metrics(
        {"iata": "HI", "otp_flights_operated": 50_000},
        {"iata": "MED", "otp_flights_operated": 2_000},
    )).set_index("iata", drop=False)
    assert scoring.confidence(d.loc["HI"])["level"] == "high"
    assert scoring.confidence(d.loc["MED"])["level"] == "medium"


def test_signal_levels_use_fixed_thresholds():
    assert scoring.signal_level(95.0) == "high"
    assert scoring.signal_level(50.0) == "medium"
    assert scoring.signal_level(10.0) == "low"
    assert scoring.signal_level(None) is None
