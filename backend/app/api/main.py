"""HTTP surface: a chat endpoint, a health check, and the scored airport list.

The API owns no logic of its own. It validates input, calls the agent, and shapes the reply.
"""

from __future__ import annotations

from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

from app.agent import agent, memory
from app.api.models import AirportOut, ChatRequest, ChatResponse
from app.data import repository as repo
from app.scoring import scoring

app = FastAPI(title="Airport Investment Intelligence Agent", version="0.1.0")
# Any localhost port, because Vite moves to 5174, 5175 and so on when 5173 is taken.
# Development scope only: a deployment would name its real origin here.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_methods=["*"], allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict:
    d = repo.scored()
    return {"status": "ok", "airports": len(d), "eligible": int(d["eligible"].sum()),
            "data": repo.meta(), "model": agent.MODEL}


@app.get("/api/airports", response_model=list[AirportOut])
def airports(eligible_only: bool = True, limit: int = 200) -> list[AirportOut]:
    """The scored universe. Backs any list or picker in the UI."""
    d = repo.scored()
    if eligible_only:
        d = d[d["eligible"]]
    d = d.sort_values("composite_score", ascending=False).head(limit)
    return [AirportOut(iata=r["iata"], name=r["name"], municipality=r["municipality"],
                       state=r["state"], composite_score=scoring._r(r["composite_score"]),
                       rank=None if scoring._num(r["rank"]) is None else int(r["rank"]))
            for _, r in d.iterrows()]


@app.post("/api/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    try:
        r = agent.chat(req.message, req.session_id)
    except RuntimeError as e:                      # missing API key and similar setup faults
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=502,
                            detail=f"Model call failed: {type(e).__name__}: {e}") from e
    return ChatResponse(session_id=r.session_id, answer=r.answer,
                        trace=[t.__dict__ for t in r.trace],
                        rounds=r.rounds, stop_reason=r.stop_reason)


@app.delete("/api/session/{session_id}")
def reset(session_id: str) -> dict:
    memory.reset(session_id)
    return {"status": "reset", "session_id": session_id}
