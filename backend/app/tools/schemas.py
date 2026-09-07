"""Tool definitions handed to Claude. Descriptions are part of the prompt: they tell the
model what each tool is for and, just as importantly, what it must not infer on its own.
"""

from __future__ import annotations

from app import config as cfg

_CODE = {"type": "string", "description": "IATA airport code, e.g. SFO. Case-insensitive."}

TOOL_SCHEMAS = [
    {
        "name": "rank_airports",
        "description": (
            "Rank US airports as terminal-expansion candidates using the deterministic "
            "Capacity Opportunity Score (congestion, passenger growth, long-haul share, "
            "capacity pressure). Use for any 'which airports' or 'best candidates' question. "
            "Filter by state codes or a named region; omit both for a national ranking. "
            "Scores are percentile ranks across the whole eligible US set, so a regional "
            "ranking uses the same numbers as a national one."),
        "input_schema": {
            "type": "object",
            "properties": {
                "states": {"type": "array", "items": {"type": "string"},
                           "description": "Two-letter US state codes. Omit for nationwide."},
                "region": {"type": "string", "enum": sorted(cfg.REGIONS),
                           "description": "Named region. Ignored when states is given."},
                "top_n": {"type": "integer", "minimum": 1, "maximum": 25, "default": 10},
            },
            "additionalProperties": False,
        },
    },
    {
        "name": "compare_airports",
        "description": (
            "Compare two to five named airports side by side on congestion, growth, load "
            "factor, long-haul share and capacity pressure. Use whenever the question names "
            "specific airports or cities to compare, including congestion-only questions."),
        "input_schema": {
            "type": "object",
            "properties": {
                "codes": {"type": "array", "items": _CODE, "minItems": 2, "maxItems": 5,
                          "description": "IATA codes to compare."},
            },
            "required": ["codes"],
            "additionalProperties": False,
        },
    },
    {
        "name": "long_haul_share",
        "description": (
            "Share of scheduled passenger departures from one airport that fly at least a "
            "given distance, with the top long-haul destinations. Use for any long-haul, "
            "short-haul or route-distance question about a single airport."),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": _CODE,
                "threshold_mi": {"type": "integer", "minimum": 500, "maximum": 5000,
                                 "default": cfg.LONG_HAUL_MI,
                                 "description": "Long-haul distance cutoff in statute miles."},
            },
            "required": ["code"],
            "additionalProperties": False,
        },
    },
    {
        "name": "unmet_demand",
        "description": (
            "Evidence of unmet flight demand at one airport, returned as separate signals "
            "(load factor, congestion, growth, capacity pressure, share of delay minutes "
            "caused by airspace and volume). There is no single unmet-demand number and no "
            "direct demand data; report the signals and the stated proxy rule as given."),
        "input_schema": {
            "type": "object",
            "properties": {"code": _CODE},
            "required": ["code"],
            "additionalProperties": False,
        },
    },
]

TOOL_NAMES = [t["name"] for t in TOOL_SCHEMAS]
