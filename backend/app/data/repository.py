"""Read-only access to data/airports.db. The only module that touches SQLite.

The scored national frame is built once on first use and cached: scoring is a pure function
of airport_metrics, so recomputing it per request would return identical numbers.
"""

from __future__ import annotations

import json
import sqlite3
from functools import lru_cache

import pandas as pd

from app import config as cfg
from app.scoring import scoring


def _connect() -> sqlite3.Connection:
    if not cfg.DB_PATH.exists():
        raise FileNotFoundError(
            f"{cfg.DB_PATH} not found. Build it first: python -m app.etl.build_db")
    return sqlite3.connect(f"file:{cfg.DB_PATH}?mode=ro", uri=True, check_same_thread=False)


@lru_cache(maxsize=1)
def meta() -> dict:
    with _connect() as conn:
        rows = pd.read_sql("SELECT key, value FROM etl_meta", conn)
    return {k: json.loads(v) for k, v in zip(rows["key"], rows["value"])}


@lru_cache(maxsize=1)
def periods() -> dict[str, str]:
    m = meta()
    return {
        "t100_curr": str(m["year_curr"]),
        "t100_prev": str(m["year_prev"]),
        "t100_growth": f"{m['year_prev']} vs {m['year_curr']}",
        "otp": m["otp_period"],
        "otp_prev": m["otp_period_prev"],
    }


@lru_cache(maxsize=1)
def scored() -> pd.DataFrame:
    """The national scored frame: one row per airport, indexed by IATA."""
    with _connect() as conn:
        m = pd.read_sql("SELECT * FROM airport_metrics", conn)
    return scoring.score(m).set_index("iata", drop=False)


def sources() -> list[dict]:
    m, p = meta(), periods()
    return [
        {"name": "BTS T-100 Segment (domestic + international, all carriers)",
         "period": f"{p['t100_prev']}-{p['t100_curr']}",
         "note": f"service class {m['t100_service_class']} (scheduled passenger) only"},
        {"name": "BTS On-Time Performance (marketing carrier)", "period": p["otp"],
         "note": "reporting carriers only; first half of the year for both years"},
        {"name": "OurAirports airports.csv + runways.csv", "retrieved": m["built_at"][:10],
         "note": f"runways at least {m['runway_min_len_ft']} ft and not closed"},
    ]


def get(codes: list[str]) -> tuple[pd.DataFrame, list[str]]:
    """Rows for the given IATA codes, plus the codes that were not found."""
    d = scored()
    wanted = [c.strip().upper() for c in codes]
    found = [c for c in wanted if c in d.index]
    return d.loc[found], [c for c in wanted if c not in d.index]


def suggest(term: str, limit: int = 5) -> list[dict]:
    """Airports whose code, name or city contains `term`. Used to answer an unknown code."""
    d = scored()
    t = term.strip().lower()
    if not t:
        return []
    hay = (d["iata"].str.lower() + " " + d["name"].fillna("").str.lower() + " "
           + d["municipality"].fillna("").str.lower())
    hits = d[hay.str.contains(t, regex=False)].sort_values("dep_curr", ascending=False)
    return [{"iata": r["iata"], "name": r["name"], "municipality": r["municipality"],
             "state": r["state"], "annual_departures": int(r["dep_curr"])}
            for _, r in hits.head(limit).iterrows()]


def routes(origin: str, year: int | None = None) -> pd.DataFrame:
    """Route-level rows for one origin. Backs long-haul analysis at any distance threshold."""
    y = year or meta()["year_curr"]
    with _connect() as conn:
        return pd.read_sql(
            "SELECT * FROM t100_route_year WHERE origin = ? AND year = ?",
            conn, params=(origin.upper(), y))
