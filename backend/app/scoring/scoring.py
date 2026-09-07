"""Deterministic KPI scoring. Pure functions over the airport_metrics frame.

This module knows nothing about the LLM, the database, or HTTP. Same input -> same output.
Every score here is a percentile rank across the *national eligible set*, never across a
query subset, so a New England ranking and a two-airport comparison use identical numbers.
"""

from __future__ import annotations

import math
from typing import Any

import pandas as pd

from app import config as cfg
from app.scoring.normalize import rank_within, safe_ratio

COMPONENTS = ("congestion", "growth", "long_haul_share", "capacity_pressure")

# Congestion blends two airport-side signals in equal parts. Mean departure delay is kept as
# an explanatory raw value only: it is dominated by carrier and late-aircraft causes, which
# renovating an airport does not fix. See DESIGN.md, "Scoring methodology".
CONGESTION_MIX = {"pct_delayed_15": 0.5, "mean_taxi_out_min": 0.5}

MIN_OTP_FLIGHTS = 1_000  # below this the delay statistics are too thin to rank
CONF_HIGH_OTP, CONF_MED_OTP = 5_000, 1_000
CONF_HIGH_MONTHS, CONF_MED_MONTHS = 12, 10

SIGNAL_HIGH, SIGNAL_MED = 80.0, 40.0


# --- raw ratios ---------------------------------------------------------------

def derive_raw(m: pd.DataFrame) -> pd.DataFrame:
    """Add the human-readable ratio columns. All inputs are raw sums from the ETL."""
    d = m.copy()
    op, sched = d["otp_flights_operated"], d["otp_flights_scheduled"]
    d["pct_delayed_15"] = safe_ratio(d["otp_delayed15"], op, 100)
    d["mean_dep_delay_min"] = safe_ratio(d["otp_dep_delay_min_sum"], op)
    d["mean_taxi_out_min"] = safe_ratio(d["otp_taxi_out_min_sum"], d["otp_taxi_out_n"])
    d["cancel_rate_pct"] = safe_ratio(d["otp_cancelled"], sched, 100)
    d["nas_delay_share_pct"] = safe_ratio(d["otp_nas_delay_min_sum"],
                                          d["otp_cause_delay_min_sum"], 100)
    d["prev_pct_delayed_15"] = safe_ratio(d["otp_prev_delayed15"],
                                          d["otp_prev_flights_operated"], 100)
    d["prev_mean_taxi_out_min"] = safe_ratio(d["otp_prev_taxi_out_min_sum"],
                                             d["otp_prev_taxi_out_n"])
    d["yoy_pax_growth_pct"] = safe_ratio(d["pax_curr"], d["pax_prev"], 100) - 100
    d["long_haul_share_pct"] = safe_ratio(d["long_haul_dep_curr"], d["dep_curr"], 100)
    d["departures_per_runway"] = safe_ratio(d["dep_curr"], d["runway_count"])
    d["load_factor_pct"] = safe_ratio(d["pax_curr"], d["seats_curr"], 100)
    return d


# --- eligibility --------------------------------------------------------------

def add_flags(d: pd.DataFrame) -> pd.DataFrame:
    """Mark why an airport can or cannot receive a composite score."""
    d = d.copy()
    small = d["dep_curr"] < cfg.MIN_ANNUAL_DEPARTURES
    no_otp = d["otp_flights_operated"].fillna(0) < MIN_OTP_FLIGHTS
    no_rwy = d["runway_count"].fillna(0) <= 0
    partial = (d["months_curr"] < 12) | (d["months_prev"] < 12)

    d["eligible"] = ~(small | no_otp | no_rwy)
    d["flags"] = [
        [f for f, hit in (("below_min_departures", s), ("no_otp_data", o),
                          ("no_runway_data", r), ("partial_t100_months", p)) if hit]
        for s, o, r, p in zip(small, no_otp, no_rwy, partial)
    ]
    return d


TIERS = ("high", "medium", "low")


def _tier(value: float, high: float, medium: float) -> str:
    return "high" if value >= high else ("medium" if value >= medium else "low")


def confidence(row: pd.Series) -> dict[str, Any]:
    """Confidence in this airport's numbers, from sample size and month coverage."""
    n_otp = _num(row.get("otp_flights_operated")) or 0
    months = min(_num(row.get("months_curr")) or 0, _num(row.get("months_prev")) or 0)
    reasons = [f"On-time sample: {int(n_otp):,} operated departures",
               f"T-100 month coverage: {int(months)}/12 in both years"]
    # Confidence is limited by the weakest input, not helped by the strongest: an airport with
    # full T-100 coverage but no on-time data is missing 35% of the composite outright.
    otp_tier = _tier(n_otp, CONF_HIGH_OTP, CONF_MED_OTP)
    month_tier = _tier(months, CONF_HIGH_MONTHS, CONF_MED_MONTHS)
    level = max(otp_tier, month_tier, key=TIERS.index)  # TIERS is best-to-worst
    for f in row.get("flags") or []:
        reasons.append(f"flag: {f}")
    return {"level": level, "reasons": reasons}


# --- scores -------------------------------------------------------------------

def add_scores(d: pd.DataFrame) -> pd.DataFrame:
    """Percentile scores over the eligible set, then the weighted composite."""
    d = d.copy()
    e = d["eligible"]

    congestion = sum(w * rank_within(d[col], e) for col, w in CONGESTION_MIX.items())
    d["congestion_score"] = congestion
    d["growth_score"] = rank_within(d["yoy_pax_growth_pct"], e)
    d["long_haul_share_score"] = rank_within(d["long_haul_share_pct"], e)
    d["capacity_pressure_score"] = rank_within(d["departures_per_runway"], e)
    d["load_factor_pct_rank"] = rank_within(d["load_factor_pct"], e)

    d["composite_score"] = sum(cfg.WEIGHTS[c] * d[f"{c}_score"] for c in COMPONENTS)
    d["rank"] = d["composite_score"].rank(ascending=False, method="min")
    d["rank_of"] = int(e.sum())
    return d


def score(metrics: pd.DataFrame) -> pd.DataFrame:
    """Entry point: raw metrics frame -> scored frame, sorted by IATA for determinism."""
    d = add_scores(add_flags(derive_raw(metrics)))
    return d.sort_values("iata").reset_index(drop=True)


# --- structured output --------------------------------------------------------

def _num(v: Any) -> float | None:
    """JSON-safe scalar: NaN/None -> None, numpy types -> float."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) else f


def _r(v: Any, nd: int = 1) -> float | None:
    f = _num(v)
    return None if f is None else round(f, nd)


def component_blocks(row: pd.Series, periods: dict[str, str]) -> dict[str, dict]:
    """The four weighted components, each carrying score, raw values, sample size and source."""
    otp_period = f"{row.get('otp_period_start')}..{row.get('otp_period_end')}"
    return {
        "congestion": {
            "weight": cfg.WEIGHTS["congestion"], "score": _r(row["congestion_score"]),
            "definition": "50% share of departures delayed >=15 min, 50% mean taxi-out time",
            "raw": {"pct_delayed_15": _r(row["pct_delayed_15"]),
                    "mean_taxi_out_min": _r(row["mean_taxi_out_min"]),
                    "mean_dep_delay_min": _r(row["mean_dep_delay_min"]),
                    "cancel_rate_pct": _r(row["cancel_rate_pct"]),
                    "prev_year_pct_delayed_15": _r(row["prev_pct_delayed_15"])},
            "n": _num(row["otp_flights_operated"]), "period": otp_period,
            "source": "BTS On-Time Performance",
        },
        "growth": {
            "weight": cfg.WEIGHTS["growth"], "score": _r(row["growth_score"]),
            "definition": "year-over-year passenger growth",
            "raw": {"pax_prev": _num(row["pax_prev"]), "pax_curr": _num(row["pax_curr"]),
                    "yoy_pct": _r(row["yoy_pax_growth_pct"], 2)},
            "n": _num(row["pax_curr"]), "period": periods["t100_growth"],
            "source": "BTS T-100 Segment",
        },
        "long_haul_share": {
            "weight": cfg.WEIGHTS["long_haul_share"], "score": _r(row["long_haul_share_score"]),
            "definition": f"share of departures with segment distance >= {cfg.LONG_HAUL_MI} mi",
            "raw": {"long_haul_departures": _num(row["long_haul_dep_curr"]),
                    "total_departures": _num(row["dep_curr"]),
                    "share_pct": _r(row["long_haul_share_pct"]),
                    "threshold_mi": cfg.LONG_HAUL_MI},
            "n": _num(row["dep_curr"]), "period": periods["t100_curr"],
            "source": "BTS T-100 Segment",
        },
        "capacity_pressure": {
            "weight": cfg.WEIGHTS["capacity_pressure"],
            "score": _r(row["capacity_pressure_score"]),
            "definition": "annual departures per usable runway",
            "raw": {"annual_departures": _num(row["dep_curr"]),
                    "runway_count": _num(row["runway_count"]),
                    "departures_per_runway": _r(row["departures_per_runway"])},
            "n": _num(row["dep_curr"]), "period": periods["t100_curr"],
            "source": f"BTS T-100 Segment + OurAirports runways (>= {cfg.RUNWAY_MIN_LEN_FT} ft)",
        },
    }


def to_score_dict(row: pd.Series, periods: dict[str, str]) -> dict[str, Any]:
    """One airport as the structured object every tool returns."""
    out = {
        "iata": row["iata"], "name": row["name"], "municipality": row["municipality"],
        "state": row["state"],
        "composite_score": _r(row["composite_score"]),
        "rank": None if _num(row["rank"]) is None else int(row["rank"]),
        "rank_of": int(row["rank_of"]),
        "eligible": bool(row["eligible"]),
        "components": component_blocks(row, periods),
        "confidence": confidence(row),
    }
    if row["flags"]:
        out["flags"] = list(row["flags"])
    if not row["eligible"]:
        out["note"] = ("Not scored: excluded from the national ranking set. Component values "
                       "are shown where the underlying data exists.")
    return out


def signal_level(v: float | None) -> str | None:
    if v is None:
        return None
    return "high" if v >= SIGNAL_HIGH else ("medium" if v >= SIGNAL_MED else "low")
