"""Every tunable in one place. All values below are declared assumptions in DESIGN.md."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = ROOT / "data"
DB_PATH = DATA_DIR / "airports.db"

# --- Data periods -------------------------------------------------------------
YEAR_PREV, YEAR_CURR = 2024, 2025  # T-100 full years; growth = YEAR_CURR vs YEAR_PREV
OTP_MONTHS = range(1, 7)  # Jan–Jun (H1) present for both years; congestion KPI uses YEAR_CURR H1

# --- ETL filters --------------------------------------------------------------
T100_SERVICE_CLASS = "F"  # scheduled passenger/cargo; G/P = all-cargo, L = non-scheduled
RUNWAY_MIN_LEN_FT = 5000  # drops helipads, GA strips
US_STATES = frozenset([
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "DC", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO",
    "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA",
    "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY",
])  # 50 states + DC; territories (PR, VI, GU, AS, MP) excluded

# --- Scoring ------------------------------------------------------------------
LONG_HAUL_MI = 1500
MIN_ANNUAL_DEPARTURES = 10_000  # eligibility: below this, small-sample noise dominates
WEIGHTS = {
    "congestion": 0.35,
    "growth": 0.30,
    "long_haul_share": 0.15,
    "capacity_pressure": 0.20,
}

# Named regions the ranking tool accepts. Kept small and conventional; anything else is
# expressed as an explicit list of state codes.
REGIONS = {
    "new_england": ["CT", "ME", "MA", "NH", "RI", "VT"],
    "mid_atlantic": ["DE", "DC", "MD", "NJ", "NY", "PA"],
    "southeast": ["AL", "FL", "GA", "KY", "MS", "NC", "SC", "TN", "VA", "WV"],
    "midwest": ["IL", "IN", "IA", "KS", "MI", "MN", "MO", "NE", "ND", "OH", "SD", "WI"],
    "southwest": ["AZ", "NM", "OK", "TX"],
    "mountain": ["CO", "ID", "MT", "NV", "UT", "WY"],
    "pacific": ["AK", "CA", "HI", "OR", "WA"],
    "west_coast": ["CA", "OR", "WA"],
}
