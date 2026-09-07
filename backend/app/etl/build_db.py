"""Build data/airports.db from every CSV directly under data/ (no subfolders).

Files are classified by content, never by filename:
  t100_dom   BTS T-100 Segment, domestic       DEPARTURES_PERFORMED + CLASS, no foreign DEST_COUNTRY
  t100_intl  BTS T-100 Segment, international  same, plus a foreign DEST_COUNTRY in the sample
  otp        BTS On-Time (marketing carrier)   DepDel15
  airports   OurAirports airports.csv          ident + iata_code
  runways    OurAirports runways.csv           airport_ident + length_ft

Output tables (all aggregates; raw rows are not persisted):
  airports, t100_route_year, t100_airport_month, otp_airport_month, airport_metrics, etl_meta
airport_metrics stores raw sums only; ratios, percentiles and scores are derived in scoring.py.
"""

from __future__ import annotations

import csv
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from app import config as cfg

T100_COLS = ["YEAR", "MONTH", "ORIGIN", "DEST", "DEPARTURES_PERFORMED", "SEATS", "PASSENGERS",
             "DISTANCE", "CLASS"]
T100_SAMPLE_ROWS = 5000
EXCLUDED_CLASS_KIND = {"G": "allcargo", "P": "allcargo", "L": "nonsched"}

OTP_CAUSE_COLS = ["CarrierDelay", "WeatherDelay", "NASDelay", "SecurityDelay", "LateAircraftDelay"]
OTP_COLS = ["Year", "Month", "Origin", "OriginState", "DepDelayMinutes", "DepDel15", "TaxiOut",
            "Cancelled", "Diverted", "Duplicate", *OTP_CAUSE_COLS]
OTP_KEY = ["origin", "year", "month"]
OTP_CHUNK = 500_000

AIRPORT_TYPES = ("large_airport", "medium_airport", "small_airport")


# --- classification -----------------------------------------------------------

def classify(path: Path) -> str | None:
    with path.open(newline="", encoding="utf-8-sig") as f:
        cols = {c.strip().upper() for c in next(csv.reader(f))}
    if "DEPDEL15" in cols:
        return "otp"
    if {"DEPARTURES_PERFORMED", "CLASS"} <= cols:
        return _classify_t100(path, cols)
    if {"IDENT", "IATA_CODE"} <= cols:
        return "airports"
    if {"AIRPORT_IDENT", "LENGTH_FT"} <= cols:
        return "runways"
    return None


def _classify_t100(path: Path, cols: set[str]) -> str:
    """Domestic vs international is decided from the data, not the header.

    Any non-US DEST_COUNTRY in the first T100_SAMPLE_ROWS rows -> international.
    A file with no DEST_COUNTRY column cannot express a foreign destination -> domestic.
    """
    if "DEST_COUNTRY" not in cols:
        return "t100_dom"
    sample = pd.read_csv(path, usecols=["DEST_COUNTRY"], nrows=T100_SAMPLE_ROWS)
    return "t100_intl" if (sample["DEST_COUNTRY"] != "US").any() else "t100_dom"


def scan(data_dir: Path) -> dict[str, list[Path]]:
    groups: dict[str, list[Path]] = {}
    for p in sorted(data_dir.glob("*.csv")):
        kind = classify(p)
        print(f"  {p.name:28s} -> {kind or 'SKIP (unrecognised header)'}")
        if kind:
            groups.setdefault(kind, []).append(p)
    return groups


# --- loaders ------------------------------------------------------------------

def _read_t100(path: Path) -> pd.DataFrame:
    """Read one T-100 file into a common shape: T100_COLS + STATE (may be NA) + IS_US_ORIGIN."""
    header = pd.read_csv(path, nrows=0).columns
    extra = [c for c in ("ORIGIN_STATE_ABR", "ORIGIN_COUNTRY") if c in header]
    df = pd.read_csv(path, usecols=T100_COLS + extra)
    df["STATE"] = df.pop("ORIGIN_STATE_ABR") if "ORIGIN_STATE_ABR" in extra else pd.NA
    df["IS_US_ORIGIN"] = ((df.pop("ORIGIN_COUNTRY") == "US") if "ORIGIN_COUNTRY" in extra
                          else df["STATE"].notna())
    return df


def load_t100(dom: list[Path], intl: list[Path]) -> tuple[pd.DataFrame, pd.Series]:
    """Returns (scheduled-passenger rows, excluded-class departures by origin/year/kind)."""
    d = pd.concat(map(_read_t100, dom), ignore_index=True).assign(INTERNATIONAL=0)
    i = pd.concat(map(_read_t100, intl), ignore_index=True).assign(INTERNATIONAL=1)
    t = pd.concat([d, i], ignore_index=True)
    t = t[t["IS_US_ORIGIN"]].drop(columns="IS_US_ORIGIN")

    # A file without a state column (the intl export) takes the state seen for the same
    # origin code in any file that has one.
    state_map = t.dropna(subset=["STATE"]).groupby("ORIGIN")["STATE"].agg(lambda s: s.mode().iat[0])
    t["STATE"] = t["STATE"].fillna(t["ORIGIN"].map(state_map))
    t = t[(t["DEPARTURES_PERFORMED"] > 0) & t["STATE"].isin(cfg.US_STATES)]

    excluded = (
        t[t["CLASS"] != cfg.T100_SERVICE_CLASS]
        .assign(kind=lambda x: x["CLASS"].map(EXCLUDED_CLASS_KIND).fillna("other"))
        .groupby(["ORIGIN", "YEAR", "kind"])["DEPARTURES_PERFORMED"].sum()
    )
    return t[t["CLASS"] == cfg.T100_SERVICE_CLASS].copy(), excluded


def _agg_otp_chunk(c: pd.DataFrame) -> pd.DataFrame:
    c = c[(c["Duplicate"] != "Y") & c["OriginState"].isin(cfg.US_STATES)]
    operated = (c["Cancelled"] == 0) & (c["Diverted"] == 0) & c["DepDelayMinutes"].notna()
    causes = c[OTP_CAUSE_COLS]
    rows = pd.DataFrame({
        "origin": c["Origin"], "year": c["Year"], "month": c["Month"],
        "flights_scheduled": 1,
        "flights_operated": operated.astype(int),
        "delayed15": c["DepDel15"].where(operated, 0).fillna(0),
        "dep_delay_min_sum": c["DepDelayMinutes"].where(operated, 0).fillna(0),
        "taxi_out_min_sum": c["TaxiOut"].where(operated, 0).fillna(0),
        "taxi_out_n": (operated & c["TaxiOut"].notna()).astype(int),
        "cancelled": c["Cancelled"].fillna(0),
        "nas_delay_min_sum": c["NASDelay"].fillna(0),
        "cause_delay_min_sum": causes.fillna(0).sum(axis=1),
        "cause_rows": causes.notna().any(axis=1).astype(int),
    })
    return rows.groupby(OTP_KEY).sum()


def load_otp(paths: list[Path]) -> pd.DataFrame:
    dtypes = {c: "float64" for c in OTP_COLS if c not in ("Origin", "OriginState", "Duplicate")}
    parts = []
    for p in paths:
        t0 = time.time()
        for chunk in pd.read_csv(p, usecols=OTP_COLS, dtype=dtypes, chunksize=OTP_CHUNK):
            parts.append(_agg_otp_chunk(chunk))
        print(f"  {p.name}: {time.time() - t0:.1f}s")
    out = pd.concat(parts).groupby(OTP_KEY).sum().reset_index()
    out[["year", "month"]] = out[["year", "month"]].astype(int)
    return out


def load_airports(airports_csv: Path, runways_csv: Path) -> pd.DataFrame:
    a = pd.read_csv(airports_csv, usecols=["ident", "type", "name", "latitude_deg", "longitude_deg",
                                           "iso_country", "iso_region", "municipality",
                                           "scheduled_service", "iata_code"])
    a = a[(a["iso_country"] == "US") & a["iata_code"].notna() & a["type"].isin(AIRPORT_TYPES)]
    # one row per IATA code: prefer scheduled service, then the larger airport type
    a = (a.assign(_svc=a["scheduled_service"] != "yes", _rank=a["type"].map(AIRPORT_TYPES.index))
          .sort_values(["_svc", "_rank"]).drop_duplicates("iata_code"))

    r = pd.read_csv(runways_csv, usecols=["airport_ident", "length_ft", "closed"])
    r = r[(r["closed"] == 0) & (r["length_ft"] >= cfg.RUNWAY_MIN_LEN_FT)]
    rw = r.groupby("airport_ident")["length_ft"].agg(runway_count="size", longest_runway_ft="max")

    a = a.merge(rw, left_on="ident", right_index=True, how="left")
    a["runway_count"] = a["runway_count"].fillna(0).astype(int)
    a["state"] = a["iso_region"].str[3:]
    return a.rename(columns={"iata_code": "iata", "ident": "icao", "latitude_deg": "lat",
                             "longitude_deg": "lon", "type": "airport_type"})[
        ["iata", "icao", "name", "municipality", "state", "lat", "lon", "airport_type",
         "runway_count", "longest_runway_ft"]].reset_index(drop=True)


# --- aggregates ---------------------------------------------------------------

def build_tables(t100: pd.DataFrame, excluded: pd.Series, otp: pd.DataFrame,
                 airports: pd.DataFrame) -> dict[str, pd.DataFrame]:
    yp, yc = cfg.YEAR_PREV, cfg.YEAR_CURR
    sums = {"departures": ("DEPARTURES_PERFORMED", "sum"), "seats": ("SEATS", "sum"),
            "passengers": ("PASSENGERS", "sum")}

    route = (t100.groupby(["ORIGIN", "DEST", "YEAR", "INTERNATIONAL"])
             .agg(distance_mi=("DISTANCE", "max"), **sums).reset_index()
             .rename(columns={"ORIGIN": "origin", "DEST": "dest", "YEAR": "year",
                              "INTERNATIONAL": "international"}))
    month = (t100.groupby(["ORIGIN", "YEAR", "MONTH"]).agg(**sums).reset_index()
             .rename(columns={"ORIGIN": "origin", "YEAR": "year", "MONTH": "month"}))

    year = month.groupby(["origin", "year"]).agg(
        departures=("departures", "sum"), seats=("seats", "sum"),
        passengers=("passengers", "sum"), months=("month", "nunique"))
    years_present = set(year.index.get_level_values("year"))
    prev = year.xs(yp, level="year") if yp in years_present else year.iloc[:0].droplevel("year")
    curr = year.xs(yc, level="year")

    m = pd.DataFrame(index=year.index.get_level_values("origin").unique().rename("iata"))
    m["state"] = t100.groupby("ORIGIN")["STATE"].agg(lambda s: s.mode().iat[0])
    m["dep_prev"], m["pax_prev"] = prev["departures"], prev["passengers"]
    m["months_prev"] = prev["months"]
    m["dep_curr"], m["pax_curr"] = curr["departures"], curr["passengers"]
    m["seats_curr"], m["months_curr"] = curr["seats"], curr["months"]

    rc = route[route["year"] == yc]
    m["intl_dep_curr"] = rc[rc["international"] == 1].groupby("origin")["departures"].sum()
    m["long_haul_dep_curr"] = (rc[rc["distance_mi"] >= cfg.LONG_HAUL_MI]
                               .groupby("origin")["departures"].sum())

    ex = excluded.unstack("kind", fill_value=0) if len(excluded) else None
    ex_curr = (ex.xs(yc, level="YEAR") if ex is not None
               and yc in ex.index.get_level_values("YEAR") else None)
    for kind in ("allcargo", "nonsched"):
        m[f"{kind}_dep_curr"] = (ex_curr[kind] if ex_curr is not None and kind in ex_curr else 0)

    otp_sum_cols = [c for c in otp.columns if c not in OTP_KEY]
    oc = otp[otp["year"] == yc].groupby("origin").agg(
        **{f"otp_{c}": (c, "sum") for c in otp_sum_cols},
        otp_months=("month", "nunique"), _m0=("month", "min"), _m1=("month", "max"))
    oc["otp_period_start"] = [f"{yc}-{int(v):02d}" for v in oc.pop("_m0")]
    oc["otp_period_end"] = [f"{yc}-{int(v):02d}" for v in oc.pop("_m1")]
    prev_cols = ("flights_operated", "delayed15", "dep_delay_min_sum", "taxi_out_min_sum",
                 "taxi_out_n")
    op = otp[otp["year"] == yp].groupby("origin").agg(
        **{f"otp_prev_{c}": (c, "sum") for c in prev_cols})

    m = m.join(oc).join(op)
    count_cols = ["dep_prev", "pax_prev", "months_prev", "dep_curr", "pax_curr", "seats_curr",
                  "months_curr", "intl_dep_curr", "long_haul_dep_curr", "allcargo_dep_curr",
                  "nonsched_dep_curr"]
    m[count_cols] = m[count_cols].fillna(0).astype("int64")

    m = m.reset_index().merge(airports.drop(columns="state"), on="iata", how="left")
    front = ["iata", "name", "municipality", "state", "lat", "lon", "airport_type",
             "runway_count", "longest_runway_ft"]
    m = m[front + [c for c in m.columns if c not in front]]
    return {"airports": airports, "t100_route_year": route, "t100_airport_month": month,
            "otp_airport_month": otp, "airport_metrics": m}


# --- main ---------------------------------------------------------------------

def main() -> None:
    t_start = time.time()
    print(f"scanning {cfg.DATA_DIR}")
    groups = scan(cfg.DATA_DIR)
    missing = {"t100_dom", "t100_intl", "otp", "airports", "runways"} - groups.keys()
    if missing:
        raise SystemExit(f"missing inputs: {sorted(missing)}")

    print("loading T-100")
    t100, excluded = load_t100(groups["t100_dom"], groups["t100_intl"])
    print("loading OTP")
    otp = load_otp(groups["otp"])
    print("loading OurAirports")
    airports = load_airports(groups["airports"][0], groups["runways"][0])
    print("building aggregates")
    tables = build_tables(t100, excluded, otp, airports)

    otp_m = sorted(cfg.OTP_MONTHS)
    meta = {
        "built_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "year_prev": cfg.YEAR_PREV, "year_curr": cfg.YEAR_CURR,
        "otp_period": f"{cfg.YEAR_CURR}-{otp_m[0]:02d}..{cfg.YEAR_CURR}-{otp_m[-1]:02d}",
        "otp_period_prev": f"{cfg.YEAR_PREV}-{otp_m[0]:02d}..{cfg.YEAR_PREV}-{otp_m[-1]:02d}",
        "t100_service_class": cfg.T100_SERVICE_CLASS,
        "runway_min_len_ft": cfg.RUNWAY_MIN_LEN_FT,
        "long_haul_mi": cfg.LONG_HAUL_MI,
        "input_files": {k: [p.name for p in v] for k, v in groups.items()},
        "row_counts": {k: len(v) for k, v in tables.items()},
    }

    if cfg.DB_PATH.exists():
        cfg.DB_PATH.unlink()
    with sqlite3.connect(cfg.DB_PATH) as conn:
        for name, df in tables.items():
            df.to_sql(name, conn, index=False)
        pd.DataFrame({"key": list(meta), "value": [json.dumps(v) for v in meta.values()]}
                     ).to_sql("etl_meta", conn, index=False)
        conn.executescript("""
            CREATE INDEX ix_route_origin_year ON t100_route_year(origin, year);
            CREATE INDEX ix_month_origin ON t100_airport_month(origin);
            CREATE INDEX ix_otp_origin ON otp_airport_month(origin);
            CREATE UNIQUE INDEX ix_metrics_iata ON airport_metrics(iata);
        """)
    print(f"wrote {cfg.DB_PATH} ({cfg.DB_PATH.stat().st_size // 1024} KB) "
          f"in {time.time() - t_start:.0f}s")
    print(json.dumps(meta["row_counts"]))


if __name__ == "__main__":
    main()
