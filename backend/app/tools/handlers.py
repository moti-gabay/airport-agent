"""Tool implementations: the only place the scoring layer and the database meet.

Two rules hold for everything here. Handlers never raise, so a bad argument becomes a
structured error the model can recover from rather than a broken conversation. And every
payload carries its own sources, assumptions and confidence, so the answer built from it
can state where each number came from.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pandas as pd

from app import config as cfg
from app.data import repository as repo
from app.scoring import scoring

BASE_ASSUMPTIONS = [
    ("Scores are percentile ranks within the eligible US airport set, not absolute measures: "
     "a score of 80 means 'higher than 80% of eligible US airports', not '80% congested'."),
    (f"Eligibility requires at least {cfg.MIN_ANNUAL_DEPARTURES:,} scheduled passenger "
     f"departures a year, at least {scoring.MIN_OTP_FLIGHTS:,} on-time records, and at least "
     "one usable runway. Smaller airports are excluded rather than ranked on thin samples."),
    ("All-cargo and non-scheduled operations are excluded, so freight-heavy airports are "
     "measured on their passenger business only."),
    ("On-time data covers January to June of both years, so seasonal summer disruption is "
     "outside the sample. Every airport is measured on the same months."),
    ("The score measures capacity pressure and demand momentum, not return on investment: "
     "construction cost, land, and airline commitments are not modelled."),
]


def _err(code: str, message: str, **extra: Any) -> dict:
    return {"error": code, "message": message, **extra}


def _resolve(codes: list[str]) -> tuple[pd.DataFrame, dict | None]:
    """Look up codes, or return the structured unknown_airport error with suggestions."""
    rows, missing = repo.get(codes)
    if missing:
        return rows, _err(
            "unknown_airport",
            f"No US airport with data for: {', '.join(missing)}. "
            "Check the IATA code, or pick from the suggestions.",
            unknown=missing,
            suggestions={m: repo.suggest(m) for m in missing},
        )
    return rows, None


def _envelope(payload: dict, extra_assumptions: list[str] | None = None) -> dict:
    return {**payload, "sources": repo.sources(),
            "assumptions": BASE_ASSUMPTIONS + (extra_assumptions or [])}


def _methodology() -> dict:
    return {
        "score_name": "Capacity Opportunity Score",
        "weights": cfg.WEIGHTS,
        "congestion_definition": scoring.CONGESTION_MIX,
        "normalization": "percentile rank (0-100) across the eligible US airport set",
        "min_annual_departures": cfg.MIN_ANNUAL_DEPARTURES,
        "long_haul_threshold_mi": cfg.LONG_HAUL_MI,
    }


# --- tools --------------------------------------------------------------------

def rank_airports(states: list[str] | None = None, region: str | None = None,
                  top_n: int = 10) -> dict:
    d = repo.scored()
    codes = [s.strip().upper() for s in (states or [])]
    if not codes and region:
        if region not in cfg.REGIONS:
            return _err("unknown_region", f"Unknown region '{region}'.",
                        valid_regions=sorted(cfg.REGIONS))
        codes = cfg.REGIONS[region]
    if codes:
        bad = [c for c in codes if c not in cfg.US_STATES]
        if bad:
            return _err("unknown_state", f"Not US state codes: {', '.join(bad)}.")

    in_scope = d[d["state"].isin(codes)] if codes else d
    eligible = in_scope[in_scope["eligible"]]
    if eligible.empty:
        return _envelope({
            "filters": {"states": codes or None, "region": region, "top_n": top_n},
            "universe": {"national_eligible": int(d["eligible"].sum()), "in_filter": 0},
            "ranked": [],
            "note": "No airport in this area clears the eligibility threshold.",
            "excluded_in_filter": _excluded(in_scope),
            "methodology": _methodology(),
        })

    top = eligible.nlargest(int(top_n), "composite_score")
    return _envelope({
        "filters": {"states": codes or None, "region": region, "top_n": int(top_n)},
        "universe": {"national_eligible": int(d["eligible"].sum()),
                     "in_filter": len(eligible)},
        "ranked": [scoring.to_score_dict(r, repo.periods()) for _, r in top.iterrows()],
        "excluded_in_filter": _excluded(in_scope),
        "methodology": _methodology(),
    })


def _excluded(in_scope: pd.DataFrame) -> list[dict]:
    """Airports inside the filter that were left out, so the exclusion is visible."""
    out = in_scope[~in_scope["eligible"]].nlargest(5, "dep_curr")
    return [{"iata": r["iata"], "name": r["name"],
             "annual_departures": int(r["dep_curr"]), "reasons": list(r["flags"])}
            for _, r in out.iterrows()]


COMPARE_METRICS = [
    ("composite_score", "Capacity Opportunity Score", "0-100", "stronger candidate"),
    ("congestion_score", "congestion score", "0-100", "more congested"),
    ("pct_delayed_15", "departures delayed 15+ min", "%", "more congested"),
    ("mean_taxi_out_min", "mean taxi-out time", "min", "more surface congestion"),
    ("mean_dep_delay_min", "mean departure delay", "min", "longer delays"),
    ("yoy_pax_growth_pct", "passenger growth year over year", "%", "faster growth"),
    ("load_factor_pct", "load factor", "%", "fuller aircraft"),
    ("departures_per_runway", "departures per runway", "per year", "more runway pressure"),
    ("long_haul_share_pct", f"departures at least {cfg.LONG_HAUL_MI} mi", "%",
     "more long-haul traffic"),
    ("dep_curr", "annual departures", "flights", "larger airport"),
]


def compare_airports(codes: list[str]) -> dict:
    if not codes or len(codes) < 2:
        return _err("bad_request", "Give at least two IATA codes to compare.")
    rows, err = _resolve(codes)
    if err:
        return err

    order = [c.strip().upper() for c in codes]
    rows = rows.loc[order]
    table = [{"metric": label, "unit": unit, "higher_means": higher,
              "values": {r["iata"]: scoring._r(r[col], 2) for _, r in rows.iterrows()}}
             for col, label, unit, higher in COMPARE_METRICS]
    return _envelope({
        "airports": [scoring.to_score_dict(r, repo.periods()) for _, r in rows.iterrows()],
        "table": table,
        "methodology": _methodology(),
    })


def long_haul_share(code: str, threshold_mi: int = cfg.LONG_HAUL_MI) -> dict:
    rows, err = _resolve([code])
    if err:
        return err
    row = rows.iloc[0]
    t = int(threshold_mi)

    r = repo.routes(row["iata"])
    total_dep, total_pax = r["departures"].sum(), r["passengers"].sum()
    if total_dep <= 0:
        return _err("no_data", f"No scheduled passenger departures recorded for {row['iata']}.")
    lh = r[r["distance_mi"] >= t]
    top = (lh.nlargest(5, "departures")[["dest", "distance_mi", "departures", "passengers"]]
           .astype({"departures": int, "passengers": int}))

    return _envelope({
        "iata": row["iata"], "name": row["name"], "state": row["state"],
        "period": repo.periods()["t100_curr"], "threshold_mi": t,
        "total_departures": int(total_dep),
        "long_haul_departures": int(lh["departures"].sum()),
        "share_pct": scoring._r(100 * lh["departures"].sum() / total_dep, 1),
        "share_pct_by_passengers": scoring._r(100 * lh["passengers"].sum() / total_pax, 1)
        if total_pax > 0 else None,
        "breakdown": {
            "domestic_long_haul_departures": int(lh[lh["international"] == 0]["departures"].sum()),
            "international_long_haul_departures":
                int(lh[lh["international"] == 1]["departures"].sum()),
        },
        "top_long_haul_destinations": top.to_dict("records"),
        "excluded_from_this_measure": {
            "all_cargo_departures": int(row["allcargo_dep_curr"]),
            "non_scheduled_departures": int(row["nonsched_dep_curr"]),
        },
        "confidence": scoring.confidence(row),
    }, [(f"Long haul means a segment distance of at least {t} statute miles, measured per "
         "flight segment rather than per itinerary: a connecting journey counts as its legs."),
        ("Freighter and charter departures are excluded and reported separately, which "
         "matters most at cargo hubs such as Anchorage, Memphis and Louisville.")])


def unmet_demand(code: str) -> dict:
    rows, err = _resolve([code])
    if err:
        return err
    row = rows.iloc[0]
    p = repo.periods()

    def block(value, **extra):
        return {"value": scoring._r(value, 1), "level": scoring.signal_level(value), **extra}

    components = {
        "load_factor": block(
            scoring._num(row["load_factor_pct_rank"]),
            unit="percentile", raw={"load_factor_pct": scoring._r(row["load_factor_pct"]),
                                    "passengers": scoring._num(row["pax_curr"]),
                                    "seats": scoring._num(row["seats_curr"])},
            period=p["t100_curr"], source="BTS T-100 Segment"),
        "congestion": block(
            scoring._num(row["congestion_score"]),
            unit="score", raw={"pct_delayed_15": scoring._r(row["pct_delayed_15"]),
                               "mean_taxi_out_min": scoring._r(row["mean_taxi_out_min"]),
                               "mean_dep_delay_min": scoring._r(row["mean_dep_delay_min"])},
            period=p["otp"], source="BTS On-Time Performance"),
        "growth": block(
            scoring._num(row["growth_score"]),
            unit="score", raw={"yoy_pax_growth_pct": scoring._r(row["yoy_pax_growth_pct"], 2)},
            period=p["t100_growth"], source="BTS T-100 Segment"),
        "capacity_pressure": block(
            scoring._num(row["capacity_pressure_score"]),
            unit="score", raw={"departures_per_runway":
                               scoring._r(row["departures_per_runway"]),
                               "runway_count": scoring._num(row["runway_count"])},
            period=p["t100_curr"], source="BTS T-100 Segment + OurAirports"),
    }
    nas = scoring._r(row["nas_delay_share_pct"])
    components["nas_delay_share"] = {
        "value": nas, "unit": "%", "level": None,
        "note": ("share of reported delay minutes attributed to National Airspace System "
                 "causes: volume, airspace and airport capacity rather than the airline"),
        "period": p["otp"], "source": "BTS On-Time Performance"}

    strong = [k for k, v in components.items() if v.get("level") == "high"]
    return _envelope({
        "iata": row["iata"], "name": row["name"], "state": row["state"],
        "components": components,
        "signals_at_high_level": strong,
        "interpretation_rule": (
            "Unmet demand is proxied, not measured: it is indicated when load factor, "
            "congestion and growth are all high, with capacity pressure and the airspace "
            "share of delay explaining why the constraint is at the airport. A signal counts "
            f"as high at {scoring.SIGNAL_HIGH:.0f} and above, medium from "
            f"{scoring.SIGNAL_MED:.0f}."),
        "confidence": scoring.confidence(row),
    }, [("No direct demand data is used: spill, denied bookings, slot requests, airfares and "
         "airline fleet plans are all outside the dataset. A high reading means demand is "
         "pressing against capacity, not a quantity of passengers turned away.")])


HANDLERS: dict[str, Callable[..., dict]] = {
    "rank_airports": rank_airports,
    "compare_airports": compare_airports,
    "long_haul_share": long_haul_share,
    "unmet_demand": unmet_demand,
}


def dispatch(name: str, args: dict) -> dict:
    """Run a tool by name. Any failure comes back as a structured error, never an exception."""
    fn = HANDLERS.get(name)
    if fn is None:
        return _err("unknown_tool", f"No tool named '{name}'.", available=list(HANDLERS))
    try:
        return fn(**args)
    except TypeError as e:
        return _err("bad_arguments", f"{name}: {e}")
    except Exception as e:  # noqa: BLE001 - a broken tool must not end the conversation
        return _err("tool_failed", f"{name} failed: {type(e).__name__}: {e}")
