"""Normalization primitives. Pure functions on pandas Series; no IO, no config."""

from __future__ import annotations

import pandas as pd


def percentile_rank(s: pd.Series) -> pd.Series:
    """Map values to a 0-100 percentile rank. Ties share the average rank.

    NaN in -> NaN out, and NaN values take no part in the ranking. That is how a subset is
    ranked: blank out the rows that are not in the set, and the survivors are ranked among
    themselves. Highest value -> 100.
    """
    return s.rank(pct=True, method="average") * 100.0


def rank_within(s: pd.Series, mask: pd.Series) -> pd.Series:
    """Percentile rank computed over `mask` rows only; rows outside the mask come back NaN."""
    return percentile_rank(s.where(mask))


def safe_ratio(num: pd.Series, den: pd.Series, scale: float = 1.0) -> pd.Series:
    """num/den*scale, with division by zero (or by NaN) yielding NaN rather than inf."""
    return (num / den.where(den > 0)) * scale
