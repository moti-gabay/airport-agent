# Airport Investment Intelligence Agent

An AI agent that helps analysts identify US airports where renovation is most likely to pay off,
based on flight and passenger capacity pressure. Ask it questions in plain language; every number
in its answers is computed by deterministic code, not by the model.

The design rationale, scoring methodology and limitations are in **[DESIGN.md](DESIGN.md)**.

```
Which airports in New England are strong candidates for terminal expansion?
Compare LA and Santa Ana airport congestion levels.
What is the percentage of long haul flights out of Anchorage airport?
What is the unmet flight demand in SFO airport and why?
```

## What it does

- Ranks and compares US airports on a **Capacity Opportunity Score**: congestion 35%,
  passenger growth 30%, long-haul share 15%, capacity pressure 20%.
- Scores are percentile ranks across the 114 eligible US airports, computed once nationally, so
  a regional ranking and a two-airport comparison use the same numbers.
- Explains its reasoning from the component values, and ends every answer with an
  "Assumptions & uncertainty" section built from the confidence, sources and flags the tools
  returned.
- Handles follow-up questions conversationally, and says so plainly when a question is out of
  scope rather than guessing.
- Shows the tool calls behind each answer in the UI, so any number can be traced to the query
  that produced it.

## Requirements

- Python 3.10+ and [uv](https://docs.astral.sh/uv/)
- Node 18+
- An Anthropic API key

## Setup

```bash
cp .env.example .env          # then add your ANTHROPIC_API_KEY
cd backend && uv sync
cd ../frontend && npm install
```

The scored database `data/airports.db` is committed, so nothing needs downloading to run the
agent.

## Run

Two terminals:

```bash
cd backend  && uv run uvicorn app.api.main:app --port 8000
cd frontend && npm run dev
```

Open <http://localhost:5173>.

There is also a terminal client, which is the fastest way to try a question:

```bash
cd backend
uv run python cli.py                                    # interactive
uv run python cli.py "Compare BOS and JFK congestion."  # one shot
```

## Tests

```bash
cd backend && uv run pytest
```

21 tests over the scoring layer: normalization bounds and tie handling, weights summing to one,
the eligibility filter, division-by-zero safety, determinism under row reordering, and two tests
pinning the rule that percentiles are computed across the national set rather than the queried
subset.

## Evaluation

19 scripted conversations (24 turns) exercise the agent end-to-end against
live API calls: scope refusals, place-name resolution, follow-up memory
reuse, and self-compute refusals. Every answer is checked for an
"Assumptions & uncertainty" section, a successful tool call, and that every
number in the prose traces back to a tool result.

5 cases (7 of the 24 turns) are flagged by the strict checker. Four are the
agent computing a derived comparison — a difference or ratio between two
values already quoted in the same answer — which is correct arithmetic, not
fabrication. The fifth is the agent offering a hypothetical re-run threshold
("I can rerun it at, say, 2,000 mi"), which isn't a claim about the data at
all. See DESIGN.md §4 and §8 for how each is scoped and why the checker
doesn't auto-accept either.

```bash
cd backend
uv run python eval.py          # full suite, ~8 min, real API calls
uv run python eval.py 3 6      # only sections 3 and 6, faster iteration
```

Exit code is 0 only when every case passes, so it's CI-ready as-is. Requires
ANTHROPIC_API_KEY in .env.

## Rebuilding the database

Only needed to refresh or extend the data.

1. Download from [BTS TranStats](https://transtats.bts.gov/) into `data/`:
   - T-100 Segment, domestic and international, one file per year
   - On-Time Performance (Marketing Carrier), one file per month
2. Fetch the reference files: `cd backend && uv run python -m app.etl.download`
3. Build: `uv run python -m app.etl.build_db` (about 35 seconds)
4. Check: `uv run python -m app.etl.sanity`

Files are identified by their contents, not their names, so any naming convention works.
Periods, thresholds and weights live in `backend/app/config.py`.

## Layout

```
backend/app/
  config.py            every tunable: weights, thresholds, periods, regions
  etl/                 CSV -> SQLite, offline. build_db.py, download.py, sanity.py
  data/repository.py   read-only database access
  scoring/             KPI ratios, percentile ranks, composite, confidence. Pure functions.
  tools/               the four tool schemas and their handlers
  agent/               Claude tool-use loop, system prompt, session memory
  api/                 FastAPI endpoints
frontend/src/          React chat UI with a collapsible tool trace
```

## API

| Endpoint | Purpose |
|---|---|
| `POST /api/chat` | `{ message, session_id? }` -> answer, tool trace, session id |
| `GET /api/airports` | the scored airport list |
| `GET /api/health` | status, data build metadata, model |
| `DELETE /api/session/{id}` | clear a conversation |

## Data

Public sources, downloaded once and cached locally: BTS T-100 Segment (2024-2025), BTS On-Time
Performance (January to June, 2024 and 2025), and OurAirports reference data. Coverage, known
biases and the reasoning behind each threshold are documented in [DESIGN.md](DESIGN.md).
