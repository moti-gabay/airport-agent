# Design and architecture

An agent that helps analysts find US airports where added flight and passenger capacity is
most likely to pay off. The language model plans and explains; every number it reports is
computed by deterministic code it cannot influence.

---

## 1. Architecture

Six layers, each depending only on the one below it.

```
  React chat UI  (frontend/)
        |  POST /api/chat  { message, session_id }
  FastAPI         app/api/         validates, calls the agent, shapes the reply
        |
  Agent           app/agent/       Claude tool-use loop, system prompt, session memory
        |  tool name + arguments            ^ tool result as JSON
  Tools           app/tools/       four tool schemas and their handlers
        |                                   |
  Scoring         app/scoring/     KPI ratios, percentile ranks, composite, confidence
        |                                   |
  Repository      app/data/        read-only SQLite access, cached scored frame
        |
  SQLite          data/airports.db
        ^
  ETL             app/etl/         BTS + OurAirports CSV -> aggregate tables (offline)
```

The important boundary is between scoring and everything above it. `app/scoring/` imports
pandas and the config module and nothing else. It has no knowledge of Claude, HTTP, or even
the database: it is a pure function from a metrics frame to a scored frame. That is what makes
the ranking reproducible and testable, and it is why the tests need no fixtures beyond
hand-written data.

`app/tools/handlers.py` is the only module where scoring and the repository meet. It is also
the trust boundary: handlers never raise. A bad airport code, an unknown region or an
unexpected failure all come back as a structured error object, which reaches the model as a
tool result marked `is_error`, so the conversation recovers instead of breaking.

### One request, end to end

1. The UI posts the question and the session id.
2. The agent loads the session history, appends the question, and calls Claude with the system
   prompt and four tool schemas.
3. Claude replies with one or more `tool_use` blocks. The loop dispatches each to a handler,
   records a trace entry, and returns all results in a single user message.
4. Steps 2 and 3 repeat until Claude stops calling tools, capped at six rounds. Hitting the cap
   triggers one final call with tools disabled, so a runaway loop still produces an answer.
5. The answer and the tool trace go back to the UI, which renders the answer as markdown and
   the trace as a collapsible list showing exactly which tools ran with which arguments.

The trace is a design choice, not a debug aid. An analyst who cannot see which query produced a
number has no way to check it.

---

## 2. Data sources

All three are public, downloaded once, and cached locally. The built SQLite file is committed
so the project runs without a four-gigabyte download.

| Source | Grain used | Period | Feeds |
|---|---|---|---|
| BTS T-100 Segment, domestic and international | carrier x month x aircraft x route segment, aggregated to route-year and airport-month | 2024 and 2025, full years | departures, seats, passengers, distance, growth, long-haul share, load factor |
| BTS On-Time Performance, marketing carrier | one row per flight, aggregated to airport-month | Jan-Jun 2024 and Jan-Jun 2025 | delay share, taxi-out, cancellations, delay causes |
| OurAirports `airports.csv` + `runways.csv` | one row per airport / per runway | current snapshot | names, cities, states, coordinates, runway counts |

The ETL classifies each file by inspecting its contents rather than trusting its filename, and
distinguishes the domestic from the international T-100 extract by sampling destination
countries. Raw flight rows are never persisted; only the five aggregate tables are.

### Database shape

| Table | Grain | Purpose |
|---|---|---|
| `airports` | airport | reference data, runway counts |
| `t100_route_year` | origin x destination x year | long-haul analysis at any distance threshold |
| `t100_airport_month` | origin x year x month | month coverage, growth inspection |
| `otp_airport_month` | origin x year x month | delay aggregates |
| `airport_metrics` | airport | the one table everything downstream reads |
| `etl_meta` | key/value | build timestamp, periods, input files, row counts |

`airport_metrics` stores **raw sums, never scores**. Ratios, percentiles, weights and confidence
are all derived at load time from about 750 rows, which costs under a millisecond. Changing a
weight or a threshold therefore requires no ETL re-run, and scoring can be tested against a
frame built by hand in a few lines.

---

## 3. Scoring methodology

### The Capacity Opportunity Score

Four KPIs, each normalized to a 0-100 percentile rank across the eligible US airport set, then
combined with fixed weights.

| Component | Weight | Definition | Reads as |
|---|---|---|---|
| Congestion | 0.35 | half the share of departures delayed 15+ minutes, half the mean taxi-out time | the airport is already straining |
| Growth | 0.30 | year-over-year passenger growth | demand is moving toward it |
| Long-haul share | 0.15 | share of departures flying 1,500+ miles | traffic needs bigger aircraft, wider gates, customs |
| Capacity pressure | 0.20 | annual departures per usable runway | throughput is high relative to physical plant |

The name matters. This is not a profitability model: it measures capacity pressure and demand
momentum. Construction cost, land, airline commitments and fares are not in the data, so the
score identifies where capacity is binding, not where a return is assured. The system prompt
requires the agent to say so whenever a question is framed around profit.

### Why congestion is not just mean delay

The obvious congestion metric is average departure delay, and it is the wrong one. Departure
delay is dominated by carrier and late-aircraft causes, which propagate through an airline's
network from somewhere else entirely and are not fixed by renovating a terminal. Two signals do
attach to the airport itself: the share of departures delayed 15 minutes or more, and mean
taxi-out time, which is close to a direct measurement of surface and runway-queue congestion.
The score is an equal blend of those two. Mean departure delay, cancellation rate and the prior
year's delay share are all returned as raw explanatory values, so the agent can discuss them,
but they carry no weight. A test pins this: changing mean departure delay alone must not move
any congestion score.

The tools also return the share of delay minutes attributed to National Airspace System causes,
which is the cleanest available separation of airport and airspace constraints from airline
ones.

### Normalization, and the mistake it avoids

Every score is a percentile rank computed **once, across the whole eligible national set**.
Tools then filter that pre-scored frame. They never re-score a subset.

This is the single most consequential decision in the scoring layer. If scores were computed
per query, then comparing two airports would always return 50 and 100 regardless of which two
they were, and a six-airport regional ranking would rescale itself into a meaningless spread.
Boston scoring 69.6 means it out-ranks about 70% of eligible US airports, and it means the same
number whether the question was about New England or the whole country. Two tests guard this:
one adds a large ineligible airport and asserts nobody's score moves, the other demonstrates
the 50/100 degeneracy that scoring a subset would produce.

### Eligibility

An airport is scored only if it has at least 10,000 scheduled passenger departures a year, at
least 1,000 on-time records, and at least one runway of 5,000 feet or more. 114 of 753 airports
qualify. Excluding rather than ranking small airports is deliberate: a percentile built on a few
hundred flights is noise wearing the costume of a measurement. Airports that fail are still
visible. The ranking tool reports which ones it dropped from the requested area and why, and the
comparison tools still return their raw values with the missing components flagged.

### Confidence: weakest link, not best case

Confidence is graded twice, once on on-time sample size and once on T-100 month coverage, and
the **worse of the two grades wins**.

| Input | high | medium | low |
|---|---|---|---|
| On-time records (operated departures) | >= 5,000 | >= 1,000 | below 1,000 |
| T-100 month coverage, both years | 12/12 | >= 10 | below 10 |

An earlier draft combined them with OR, so an airport qualified as medium if either input was
adequate. That is wrong when the two inputs feed different parts of the score and one carries
35% of the weight: an airport with full T-100 coverage and no on-time data at all would have
been labelled "medium" while its entire congestion component was missing. A missing high-weight
component must never be masked by a strong reading elsewhere.

### Unmet demand is a proxy, and is returned as one

There is no measurement of turned-away passengers in this data, so `unmet_demand` deliberately
returns no single number. It returns five separate signals: load factor percentile, congestion,
growth, capacity pressure, and the airspace share of delay minutes. Each is labelled high,
medium or low by fixed thresholds in code, so the reasoning is deterministic and the model only
phrases it. The stated rule is that unmet demand is indicated when load factor, congestion and
growth are all high, with capacity pressure and the airspace delay share explaining why the
constraint sits at the airport rather than with the airlines.

---

## 4. Where AI is used, and where it is not

| | |
|---|---|
| **The model does** | interpret the question; resolve place names to IATA codes ("Santa Ana" to SNA, "Sacramento Metro" to SMF); choose which tools to call with which arguments; decide when a follow-up needs new data or can reuse what is already in the conversation; explain the components in prose; assemble the assumptions section from the fields the tools returned; recognise an out-of-scope question and say so |
| **The model does not** | compute a score, a weight, a percentile or a ranking; decide a threshold; assign a confidence level; choose which airports are eligible; produce a score for an airport the tools did not score |

That second row used to read "compute, weight, rank, average or round any number", which was
broader than the truth and was written before it was measured. The eval described below shows
two things the model does do with figures a tool returned, neither of which is fabrication:

- **Display rounding.** It writes 15.8 for a taxi-out time of 15.75, and 21.27M for 21,269,882.
- **Simple comparative arithmetic.** It relates two values already quoted in the same answer:
  a 69.6 against a 63.1 becomes "a 6.5-point gap", a 29.6% against a 12.4% becomes "roughly
  2.4x". The inputs are on the page, so the arithmetic is checkable by the reader.

Neither can move a ranking or invent a quantity the data does not contain, which is what the
rule is protecting. What remains forbidden is any number that would compete with the scoring
layer: a re-weighted composite, an averaged score, a percentile for an unscored airport.

Three mechanisms enforce the split rather than merely requesting it. Structurally, the scoring
module cannot reach the model and the model cannot reach the data except through four typed
tools. Every tool payload carries its own raw values, sample sizes, periods, sources, confidence
and assumptions, so the model has no incentive to invent context. And the system prompt states
the rule directly: every number must come from a tool result in the conversation, and anything a
tool did not return is reported as unavailable.

The failure this design targets is the plausible fabricated statistic. An LLM asked to rank
airports will happily produce numbers that look right. Here the only numbers in scope are the
ones a tool returned, and the trace in the UI shows which call produced them.

### Checking that the split holds

`backend/eval.py` runs a fixed list of 19 cases, 24 turns in all, through the real agent: the
single-turn questions in fresh sessions, the follow-up sequences in one session each. Three
checks per answer, and two narrower ones on the sections that call for them.

| Check | What it asserts |
|---|---|
| assumptions | an "Assumptions & uncertainty" section is present |
| tools | a tool call succeeded, or was legitimately reused on a follow-up |
| numbers | every number in the prose is traceable to the tool JSON of that conversation |
| same_numbers | one region ranking asked three ways returns identical scores |
| requires | raising the long-haul threshold to 2,000 mi issues a **new** `long_haul_share` call |

The number check is the reason the file exists, and it is stricter than a substring search.
Tool values are harvested from the whole payload, dict keys and prose included, and a claim
matches only if a tool value rounds to it at the precision the model actually wrote: 21,269,882
supports "21.27M", a weight of 0.35 supports "35%", and a round integer may be a rounding of a
real one within one percent, so 46,987 supports "roughly 47,000" but not "52,000". Failures
are reported with the sentence around them, because "3.9 is untraceable" is not actionable and
"3.9 min longer than SNA" is.

**It does not accept a difference or ratio of two tool values as traceable, deliberately.**
Doing so looks reasonable and would quietly destroy the check. A turn puts on the order of two
hundred numbers into the allowed set; the set of pairwise differences and ratios over it runs
to tens of thousands of values, dense enough across the range that an invented score of 88.4
would stand a good chance of matching one by coincidence and passing. The check would then
confirm nothing. Keeping it strict costs some false alarms and keeps the one property worth
having: a number that is not in the data gets flagged. For the same reason the eval has no
notion of a hypothetical, so a figure the model offers rather than asserts, as in "I can rerun
it at, say, 2,000 mi", is reported like any other claim.

**Standing result: 14 of 19 cases pass outright.** Every structural check passes on all 24
turns, including the two narrow ones: three phrasings of the New England question return
identical scores, and the 2,000-mile follow-up re-queries instead of reusing the 1,500-mile
answer. Four of the five remaining cases are flagged for the comparative arithmetic described
above ("a 6.5-point gap", "3.8x the departures per runway", "3.6x smaller sample"). The fifth
is the model offering to rerun at a threshold it names itself, and it is intermittent. None of
the five is an invented number, and that distinction is the finding: the eval is not green, and
what it is not green about is documented rather than tuned away.

---

## 5. Assumptions and limitations

### Scoping assumptions

- **Passenger service only.** All-cargo and non-scheduled operations are excluded everywhere.
  This changes the picture materially at freight hubs: Anchorage has 41,723 all-cargo departures
  against 36,040 scheduled passenger departures, so the agent describes a minority of what that
  airport actually does. The long-haul tool reports the excluded cargo count explicitly.
- **US states and DC only.** Territories are excluded.
- **Departures only.** Origin-side traffic, consistent with questions about flights out of an
  airport.
- **A runway counts** if it is at least 5,000 feet and not closed, which drops helipads and
  general-aviation strips.

### T-100 domestic carrier scope differs between the years

The 2024 domestic extract is the All Carriers table and the 2025 extract is US Carriers Only, so
the two years are not drawn from an identical carrier universe. Since growth is 30% of the
score, this was measured rather than assumed. Twenty-eight carriers appear in 2024 and not in
2025, and none appear only in 2025. They are foreign flags with token domestic segment counts,
the largest being China Eastern, Lufthansa and Air Canada.

| | departures | passengers | share of 2024 domestic passengers |
|---|---|---|---|
| Carriers present in 2024 only | 9,685 | 98,359 | 0.0115% |

At about one hundredth of one percent, this is two orders of magnitude below the movements the
growth KPI reports, so the years are treated as comparable. The underlying reason is structural:
cabotage rules bar foreign carriers from US domestic point-to-point service, so the two tables
describe nearly the same universe by construction. Both international extracts are All Carriers.

### On-time data covers the first half of each year

January to June, for both years. Summer convective weather, the heaviest delay season, is
outside the sample, so absolute delay rates read lower than full-year figures would. Every
airport is measured on the same months and congestion is scored as a percentile across airports,
so the ranking holds; only the absolute levels are seasonally biased.

### Known weaknesses in the proxies

- **Departures per runway is crude.** It counts runways without regard to their geometry.
  San Francisco's real constraint is a pair of closely spaced parallel runways that lose most of
  their capacity in low visibility, and a count of four does not express that. The proxy ranks
  throughput pressure, not achievable capacity.
- **Runway pressure is not terminal pressure.** The question is usually about terminals and
  gates. Nothing in this data measures gate count, hold-room area or customs capacity, so runway
  loading stands in for physical strain and the agent is required to say so.
- **On-time data covers reporting carriers only,** which under-represents small operators, and
  therefore smaller airports, more than large ones.
- **Growth is a single year-over-year pair,** so it carries one year of noise with no trend and
  no smoothing.
- **Delay-cause fields are populated only for sufficiently delayed flights,** so the airspace
  share of delay minutes describes delayed flights, not all flights.
- **Load factor is computed from seats and passengers on scheduled passenger segments**, which
  is an average across the year and hides peak-hour and seasonal saturation, the conditions that
  actually drive terminal investment.

### An observation about the data

Nationally, departures rose 2.3% between the two years while passengers fell 1.1%, a load-factor
decline of roughly two points. The pattern is uniform across all twelve months and both years
have complete month coverage, so it is a property of the data rather than a gap in it. It does
mean the median airport's growth score sits on slightly negative passenger growth, which matters
when reading growth percentiles: a growth score of 60 is not the same as 60% growth, or even
necessarily positive growth in absolute terms.

---

## 6. Key tradeoffs

**Percentile rank over min-max normalization.** Percentiles are robust to the extreme outliers
this data is full of: Atlanta and Chicago would compress every other airport into the bottom of
a min-max scale. The cost is that percentiles discard magnitude. The gap between rank 1 and rank
2 may be enormous or negligible and the score looks the same either way, which is exactly why
every component also carries its raw value.

**Cached SQLite over live API calls.** BTS publishes bulk CSV, not a query API, and the volume
is around four gigabytes. Building an aggregate database once makes every query sub-millisecond,
makes the whole system reproducible, and means a demo does not depend on a third-party service
being up. The cost is data staleness, which is why the build timestamp and periods are recorded
in the database and surfaced in every tool result.

**Raw sums in the database, ratios in code.** Re-weighting requires no ETL run and scoring stays
unit-testable. The cost is that the database alone does not tell you an airport's score.

**A hand-written tool loop over the SDK tool runner.** Roughly forty lines, no beta dependency,
and each round is visible, which is what makes the UI trace possible. The runner would have been
shorter but would have hidden the thing worth showing.

**Non-streaming responses.** A question with two tool rounds takes something like ten to thirty
seconds, during which the UI shows a single pending state. Streaming would improve that and was
left out to keep the loop simple; the tool trace gives the user something concrete to read on
arrival.

**In-memory sessions.** A dict keyed by session id, holding full message history including tool
results. Restarting the server clears conversations. For an assignment this is the right size;
the alternative buys durability nobody asked for.

---

## 7. Considered and deferred

- **RAG.** There is no corpus to retrieve. The data is numeric and fits in a small database, and
  a vector store between the question and the numbers would add a failure mode without adding an
  answer.
- **MCP.** The tools are four functions in the same process. MCP earns its cost when tools are
  shared across clients or owned by another team.
- **Multi-agent.** One agent with four tools handles every question in the brief. A planner and
  a critic would multiply latency and cost to no observable benefit at this scope.
- **FAA live status.** The NAS Status feed shows ground stops and delay programs right now.
  It answers a different question from the historical one and would need to stay strictly out of
  the scoring layer, so it was scoped as a separate fifth tool and left out.
- **Voice.** The browser speech APIs would cover the bonus in a small amount of code, but it is
  polish, and the brief asks for reasoning over polish.
- **Streaming responses and persistent sessions.** Both are straightforward and neither changes
  any answer.
- **Server-side refusal fallbacks.** Available on this model, but airport capacity analysis does
  not generate refusals, so the parameter would be unexercised complexity.

## 8. Future work

In rough order of how much each would improve the answers:

1. **The cost side.** The score finds pressure, not return. FAA Airport Improvement Program
   grant history, enplanement-based cost benchmarks and published capital plans would let a
   pressure score become an actual investment ranking.
2. **Peak-hour capacity instead of annual averages.** Terminals are built for the busy hour. The
   on-time data has scheduled departure times, so a peak-hour utilization curve is derivable from
   data already loaded, and would replace the weakest proxy in the model.
3. **Multi-year trends.** Three to five years of T-100 would replace a single noisy year-over-year
   pair with a fitted trend, and would let growth be separated from post-pandemic recovery.
4. **Runway capacity rather than runway count.** FAA airport capacity benchmarks give actual
   hourly rates under different weather conditions, which is what the runway count is standing in
   for.

### Done: the evaluation set

This list used to carry a fifth item, an evaluation set, on the grounds that the scoring layer
was tested thoroughly while the agent's behaviour was verified only by inspection, so a prompt
change could loosen the citation rule with nothing to notice. `backend/eval.py` closes that gap:
19 cases over 24 turns, checking the assumptions section, tool use and the traceability of every
number, plus phrasing-invariance of a ranking and a forced re-query on a changed threshold.

It stands at 14 of 19 cases passing. Four of the five failures are the comparative arithmetic
recorded in section 4, differences and ratios between two values quoted in the same answer; the
fifth is a threshold the model offered to rerun at. None is an invented number. They are left
failing rather than excused, because the alternative is teaching the check to accept derived
values, which section 4 explains would cost it the ability to catch a fabrication at all.

What the eval still does not do is pin expected values. It asserts that every number is
traceable to a tool result, not that the correct number was quoted, so a scoring regression that
stayed internally consistent would pass. Anchoring a handful of questions to known figures is
the natural next step.
