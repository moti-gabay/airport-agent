"""Terminal chat against the agent. Usage:

    uv run python cli.py                     interactive
    uv run python cli.py "question" ...      one shot per argument, same session
"""

from __future__ import annotations

import sys

from dotenv import load_dotenv

load_dotenv()

from app.agent import agent


def ask(question: str, session: str | None) -> str:
    print(f"\n\033[1m> {question}\033[0m\n")
    r = agent.chat(question, session)
    for t in r.trace:
        mark = "ok " if t.ok else "ERR"
        print(f"  \033[90m[{mark}] {t.name}({t.input}) -> {t.summary}\033[0m")
    print(f"\n{r.answer}\n" + "-" * 78)
    return r.session_id


def main() -> None:
    session = None
    if len(sys.argv) > 1:
        for q in sys.argv[1:]:
            session = ask(q, session)
        return
    print("Airport investment agent. Ctrl-C to quit.")
    while True:
        try:
            q = input("\n> ").strip()
        except (EOFError, KeyboardInterrupt):
            return
        if q:
            session = ask(q, session)


if __name__ == "__main__":
    main()
