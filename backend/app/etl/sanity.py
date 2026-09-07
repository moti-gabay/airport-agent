"""Print derived metrics for a few well-known airports to eyeball the ETL output.

Usage: python -m app.etl.sanity [IATA ...]   (default: SFO LAX SNA ANC BOS)
"""

import sqlite3
import sys

import pandas as pd

from app import config as cfg

DEFAULT = ["SFO", "LAX", "SNA", "ANC", "BOS"]


def main(codes: list[str]) -> None:
    with sqlite3.connect(cfg.DB_PATH) as conn:
        placeholders = ",".join("?" * len(codes))
        m = pd.read_sql(f"SELECT * FROM airport_metrics WHERE iata IN ({placeholders})",
                        conn, params=codes).set_index("iata")
    m = m.reindex(codes)
    out = pd.DataFrame({
        "name": m["name"].str.slice(0, 30),
        "state": m["state"],
        "dep_curr": m["dep_curr"],
        "pax_curr_M": (m["pax_curr"] / 1e6).round(2),
        "pax_yoy_%": (100 * (m["pax_curr"] / m["pax_prev"] - 1)).round(2),
        "load_factor_%": (100 * m["pax_curr"] / m["seats_curr"]).round(1),
        "long_haul_%": (100 * m["long_haul_dep_curr"] / m["dep_curr"]).round(1),
        "allcargo_dep_excl": m["allcargo_dep_curr"],
        "runways": m["runway_count"],
        "otp_n": m["otp_flights_operated"],
        "pct_delayed_15": (100 * m["otp_delayed15"] / m["otp_flights_operated"]).round(1),
        "mean_dep_delay": (m["otp_dep_delay_min_sum"] / m["otp_flights_operated"]).round(1),
        "mean_taxi_out": (m["otp_taxi_out_min_sum"] / m["otp_taxi_out_n"]).round(1),
        "prev_pct_del15": (100 * m["otp_prev_delayed15"] / m["otp_prev_flights_operated"]).round(1),
        "nas_share_%": (100 * m["otp_nas_delay_min_sum"] / m["otp_cause_delay_min_sum"]).round(1),
    })
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(out.T)


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT)
