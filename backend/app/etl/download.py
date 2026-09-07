"""Fetch OurAirports reference files into data/. Skips files that already exist.

BTS T-100 Segment and On-Time Performance CSVs are NOT downloaded here: they come from the
TranStats download form (transtats.bts.gov) and are placed in data/ by hand. build_db.py
classifies every data/*.csv by header, so filenames do not matter.
"""

import sys

import httpx

from app import config as cfg

BASE = "https://davidmegginson.github.io/ourairports-data/"
FILES = ("airports.csv", "runways.csv")


def main(force: bool = False) -> None:
    cfg.DATA_DIR.mkdir(exist_ok=True)
    for name in FILES:
        dest = cfg.DATA_DIR / name
        if dest.exists() and not force:
            print(f"skip  {dest} (exists)")
            continue
        with httpx.stream("GET", BASE + name, follow_redirects=True, timeout=60) as r:
            r.raise_for_status()
            with dest.open("wb") as f:
                for chunk in r.iter_bytes():
                    f.write(chunk)
        print(f"saved {dest} ({dest.stat().st_size // 1024} KB)")


if __name__ == "__main__":
    main(force="--force" in sys.argv)
