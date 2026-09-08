"""Behavioural eval for the agent. Runs a fixed prompt list and checks each answer.

    uv run python eval.py           all sections
    uv run python eval.py 3 6       only sections 3 and 6

Three checks per turn, plus two section-specific ones:

  assumptions  an "Assumptions & uncertainty" section is present
  tools        a tool call succeeded (or, where noted, was merely made or legitimately reused)
  numbers      every number in the answer is traceable to the tool JSON of this conversation
  requires     a specific tool call was made with specific arguments
  same_numbers two phrasings of one question produced identical scores

The numbers check is the reason this exists: the scoring layer is unit-tested, the citation
rule is only a sentence in the system prompt, and a prompt edit can quietly loosen it.

It is strict on purpose, and two known false-alarm classes follow from that. A difference or
ratio between two tool values ("a 6.5-point gap") is not accepted, because the pairwise closure
over a few hundred allowed numbers is dense enough that accepting it would let an invented score
through as well. A percentile restated as its complement ("a congestion score of 14.5 is less
congested than ~85%") is an instance of that class, not an exception to it. And a number the
model offers rather than asserts ("rerun it at, say, 2,000 mi") has no special status. Both are
reported with their surrounding sentence so they take a second to dismiss. A third class was
closed rather than accepted: a unit written flush against the digits ("1500mi") used to be
mis-tokenized as the shorter number 150 and flagged as untraceable, so CLAIM_RE now lets a
known unit suffix end a number. See DESIGN.md section 4.
"""

from __future__ import annotations

import re
import sys
import time
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()

from app.agent import agent
from app.agent.prompts import system_prompt
from app.tools import handlers

# --- the test list ------------------------------------------------------------


@dataclass
class Turn:
    prompt: str
    tools: str = "ok"  # ok | made | reuse | skip
    assumptions: bool = True
    requires: tuple[str, dict] | None = None  # (tool name, argument subset)


@dataclass
class Case:
    section: int
    name: str
    turns: list[Turn]
    same_numbers: str | None = None  # group key; ranked scores must match across the group


def one(section: int, name: str, prompt: str, **kw) -> Case:
    group = kw.pop("same_numbers", None)
    return Case(section, name, [Turn(prompt, **kw)], group)


CASES: list[Case] = [
    # 1. the original assignment questions
    one(1, "new england ranking",
        "Which airports in New England are strong candidates for terminal expansion?",
        same_numbers="new_england"),
    one(1, "LA vs Santa Ana", "Compare LA and Santa Ana airport congestion levels."),
    one(1, "Anchorage long haul",
        "What is the percentage of long haul flights out of Anchorage airport?"),
    one(1, "SFO unmet demand", "What is the unmet flight demand in SFO airport and why?"),

    # 2. same question, different phrasing: the numbers must not move
    one(2, "new england, reworded",
        "Which New England airports look strong for terminal expansion?",
        same_numbers="new_england"),
    one(2, "new england, states listed",
        "Rank airports in Connecticut, Maine, Massachusetts, New Hampshire, "
        "Rhode Island and Vermont.",
        same_numbers="new_england"),

    # 3. out of scope. A correct answer may use no tools and end without an assumptions
    #    section; what it must not do is produce numbers from outside the data.
    one(3, "non-US airport", "What about Heathrow's congestion levels?",
        tools="skip", assumptions=False),
    one(3, "construction cost",
        "Should we invest in expanding SFO's terminal — what would it cost?",
        tools="skip", assumptions=False),
    one(3, "live conditions", "What's the current weather delay situation at ORD right now?",
        tools="skip", assumptions=False),
    one(3, "airline comparison",
        "How does Delta's on-time performance compare to United?",
        tools="skip", assumptions=False),

    # 4. unresolvable or ambiguous names. unknown_airport is an is_error result, so the
    #    tool check here is that a call was made, not that it succeeded.
    one(4, "ambiguous city", "What about Springfield airport?", tools="made",
        assumptions=False),
    one(4, "same name, two states", "Compare Portland and Portland.", tools="made",
        assumptions=False),
    one(4, "nonexistent code", "Tell me about XYZ airport.", tools="made", assumptions=False),

    # 5. below the eligibility threshold: named exclusion reason, not silence or a guess
    one(5, "ineligible airport", "Is Nantucket a good candidate for expansion?", tools="made"),
    one(5, "ineligible score", "What's the congestion score for Bar Harbor?", tools="made"),

    # 6. one session. Reuse where the data is already in the conversation, a fresh call
    #    where it is not: turn 4 changes the threshold and must re-query.
    Case(6, "follow-up sequence", [
        Turn("Rank the top 5 airports nationally."),
        Turn("What about the fourth one on that list — why is it there?", tools="reuse"),
        Turn("Compare it to Boston."),
        Turn("And if the long-haul threshold were 2,000 miles instead?",
             requires=("long_haul_share", {"threshold_mi": 2000})),
        Turn("Which of the airports we've discussed has the lowest confidence?",
             tools="reuse"),
    ]),

    # 7. asked to do the arithmetic itself. Re-weighting needs a scoring run, not a guess.
    one(7, "re-weight request",
        "If we weighted growth at 50% instead of 30%, which airport would rank first?",
        tools="skip", assumptions=False),
    one(7, "average two scores", "Can you average BOS and JFK's scores for me?",
        tools="skip", assumptions=False),

    # 8. unmet demand where the signals disagree, then on an airport with no ranking at all
    Case(8, "mixed unmet demand", [
        Turn("What's the unmet demand picture for JFK?"),
        Turn("What about Nantucket's unmet demand?", tools="made"),
    ]),
]

# --- checks -------------------------------------------------------------------

ASSUMPTIONS_RE = re.compile(r"assumptions\s*(?:&|and)\s*uncertainty", re.IGNORECASE)
LIST_MARKER_RE = re.compile(r"(?m)^\s{0,6}\d+[.)]\s")
NUMBER = r"-?\d+(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?"
# Harvesting is deliberately permissive: a superset of allowed values costs nothing.
HARVEST_RE = re.compile(NUMBER)
# A claim is stricter. A digit run inside an identifier is not a claim (the "100" of
# "T-100"), and a hyphen between two numbers is a range, not a minus sign ("10-15 min").
# A unit written flush against the digits still ends the number, so "1500mi" is a claim
# of 1500; without this the match backs off to 150 and fails as untraceable.
UNITS = r"min|mi|pax|ft"
CLAIM_RE = re.compile(
    rf"(?<![A-Za-z0-9])(?<![A-Za-z]-)(?<![0-9]-)({NUMBER})"
    rf"\s*(million|billion|thousand|M|B|K|k)?(?:th|st|nd|rd)?"
    rf"(?:(?![A-Za-z])|(?=(?:{UNITS})(?![A-Za-z])))")
SCALE = {None: 1.0, "thousand": 1e3, "k": 1e3, "K": 1e3, "million": 1e6, "M": 1e6,
         "billion": 1e9, "B": 1e9}
MINUS = str.maketrans({"\u2212": "-"})  # the model writes a real minus sign, JSON writes a hyphen


def numbers_in_text(text: str, into: set[float]) -> None:
    for m in HARVEST_RE.finditer(text.translate(MINUS)):
        try:
            into.add(float(m.group(0).replace(",", "")))
        except ValueError:
            pass


def harvest(obj, into: set[float]) -> None:
    """Every number reachable in a tool payload, including the ones inside keys and prose."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            numbers_in_text(str(k), into)
            harvest(v, into)
    elif isinstance(obj, list):
        for v in obj:
            harvest(v, into)
    elif isinstance(obj, bool):
        pass
    elif isinstance(obj, (int, float)):
        into.add(float(obj))
    elif isinstance(obj, str):
        numbers_in_text(obj, into)


def tolerance(digits: str, decimals: int, scale: float) -> float:
    """How far a tool value may sit from the written number and still be the same number.

    Half a unit of the last written digit, so 69.63 supports "69.6". A round integer is
    also read as a rounded quantity, so 46,987 supports "47,000" - but never by more than
    one percent, which still separates a rounding from an invention.
    """
    tol = 0.5 * (10 ** -decimals) * scale
    value = abs(float(digits.replace(",", ""))) * scale
    if decimals == 0 and value >= 1000 and digits.rstrip("0") != digits:
        zeros = len(digits) - len(digits.rstrip("0"))
        tol = max(tol, min(0.5 * 10 ** zeros * scale, 0.01 * value))
    # A value ending in exactly half a unit ("15.75" written as "15.8") lands on the
    # boundary, where binary floats put it a hair outside. Widen by a hair.
    return tol * (1 + 1e-9)


def traceable(value: float, tol: float, allowed: set[float]) -> bool:
    """True if some tool value matches what the answer wrote, within tolerance.

    A tool value is also accepted as its percentage (weight 0.35 written as "35%").
    """
    return any(
        abs(abs(t) - abs(value)) <= tol  # prose carries the sign: "fell 0.86%" for -0.86
        or abs(abs(t) * 100 - abs(value)) <= tol
        or abs(abs(t) / 100 - abs(value)) <= tol
        for t in allowed
    )


def untraceable_numbers(answer: str, allowed: set[float]) -> tuple[list[str], int]:
    """Numeric claims in the answer with no counterpart in the tool results.

    Each one is reported with the words around it, because "3.9 is untraceable" is not
    actionable and "BOS taxi-out is 3.9 min longer than SNA" is: it says the model
    subtracted two figures rather than invented one.
    """
    text = LIST_MARKER_RE.sub("", answer).translate(MINUS)
    bad, total = [], 0
    for m in CLAIM_RE.finditer(text):
        digits, suffix = m.group(1).replace(",", ""), m.group(2)
        value = float(digits)
        total += 1
        decimals = len(digits.split(".")[1]) if "." in digits else 0
        scale = SCALE[suffix]
        # Bare small integers are list positions, counts of items shown, "top 5".
        if suffix is None and decimals == 0 and 0 <= value <= 10:
            continue
        if not traceable(value * scale, tolerance(digits, decimals, scale), allowed):
            context = " ".join(text[max(0, m.start() - 45):m.end() + 45].split())
            bad.append(f"{m.group(0).strip()}  in \u2026{context}\u2026")
    return bad, total


def check(turn: Turn, answer: str, calls: list[tuple], prior_ok: int,
          allowed: set[float]) -> tuple[list[str], str]:
    ok_calls = [c for c in calls if "error" not in c[2]]
    fails = []

    if turn.assumptions and not ASSUMPTIONS_RE.search(answer):
        fails.append("no assumptions section")

    if turn.tools == "ok" and not ok_calls:
        fails.append("no tool call succeeded")
    elif turn.tools == "made" and not calls:
        fails.append("no tool call made")
    elif turn.tools == "reuse" and not ok_calls and not prior_ok:
        fails.append("no tool data, in this turn or earlier")

    if turn.requires:
        name, args = turn.requires
        if not any(c[0] == name and all(c[1].get(k) == v for k, v in args.items())
                   for c in calls):
            fails.append(f"missing call {name}({args})")

    bad, total = untraceable_numbers(answer, allowed)
    fails += [f"untraceable: {b}" for b in bad]

    tools = "reused" if not calls else f"{len(ok_calls)}/{len(calls)} ok"
    stated = "yes" if ASSUMPTIONS_RE.search(answer) else "no"
    return fails, (f"assumptions {stated:<3} | tools {tools:<9} | "
                   f"numbers {total - len(bad)}/{total}")


# --- runner -------------------------------------------------------------------


@dataclass
class Outcome:
    passed: bool = True
    lines: list[str] = field(default_factory=list)
    ranked: set[tuple] = field(default_factory=set)


def run_case(case: Case) -> Outcome:
    out, session, prior_ok = Outcome(), None, 0
    # The system prompt states the data periods, weights and thresholds. Quoting those
    # back is not fabrication, so they start out allowed.
    allowed: set[float] = set()
    numbers_in_text(system_prompt(), allowed)
    multi = len(case.turns) > 1

    for turn in case.turns:
        numbers_in_text(turn.prompt, allowed)
        calls: list[tuple] = []
        original = handlers.dispatch

        def record(name, args, _o=original, _c=calls):
            result = _o(name, args)
            _c.append((name, args, result))
            return result

        handlers.dispatch = record
        started = time.time()
        try:
            reply = agent.chat(turn.prompt, session)
        except Exception as e:  # noqa: BLE001 - a transport failure is a result, not a crash
            out.passed = False
            out.lines.append(f"    ERROR {type(e).__name__}: {e}")
            return out
        finally:
            handlers.dispatch = original
        session = reply.session_id

        for name, args, result in calls:
            harvest(args, allowed)
            harvest(result, allowed)
            out.ranked |= {(a["iata"], a["composite_score"])
                           for a in result.get("ranked", []) if "iata" in a}
        fails, stats = check(turn, reply.answer, calls, prior_ok, allowed)
        prior_ok += sum("error" not in c[2] for c in calls)

        label = f"    {turn.prompt[:54]:<54}" if multi else ""
        mark = "PASS" if not fails else "FAIL"
        out.passed &= not fails
        if multi:
            out.lines.append(f"  {mark}{label}  {stats}  [{time.time() - started:.0f}s]")
        else:
            out.lines.append(f"  {stats}  [{time.time() - started:.0f}s]")
        out.lines += [f"        - {f}" for f in fails]
    return out


def main() -> int:
    wanted = {int(a) for a in sys.argv[1:] if a.isdigit()}
    cases = [c for c in CASES if not wanted or c.section in wanted]
    results, groups = [], {}

    for case in cases:
        print(f"[{case.section}] {case.name}", flush=True)
        out = run_case(case)
        print("\n".join(out.lines), flush=True)
        if case.same_numbers:
            groups.setdefault(case.same_numbers, []).append((case.name, out.ranked))
        mark = "PASS" if out.passed else "FAIL"
        print(f"  -> {mark}\n", flush=True)
        results.append((case, out.passed))

    for key, members in groups.items():
        if len(members) < 2:
            continue
        first, rest = members[0], members[1:]
        same = all(r[1] == first[1] for r in rest)
        print(f"[same_numbers: {key}] {'PASS' if same else 'FAIL'} "
              f"across {len(members)} phrasings, {len(first[1])} scored airports")
        if not same:
            for name, ranked in members:
                print(f"    {name}: {sorted(ranked)}")
            results.append((Case(2, f"same_numbers:{key}", []), False))
        print(flush=True)

    failed = [c.name for c, ok in results if not ok]
    print(f"{len(results) - len(failed)}/{len(results)} passed"
          + (f" — failed: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
