"""Conversation memory: an in-process dict of session id -> message list.

Deliberately not a database. Sessions are cheap to rebuild, the assignment does not ask for
durability, and keeping it here means the agent layer has no storage dependency.
"""

from __future__ import annotations

import threading
from typing import Any

MAX_MESSAGES = 40  # trimmed from the front, so the oldest exchanges drop out first

_store: dict[str, list[dict[str, Any]]] = {}
_lock = threading.Lock()


def get(session_id: str) -> list[dict[str, Any]]:
    with _lock:
        return list(_store.get(session_id, []))


def set(session_id: str, messages: list[dict[str, Any]]) -> None:
    trimmed = messages[-MAX_MESSAGES:]
    # Never start the history with a tool result: that is an orphan without its tool_use.
    while trimmed and _starts_with_tool_result(trimmed[0]):
        trimmed = trimmed[1:]
    with _lock:
        _store[session_id] = trimmed


def reset(session_id: str) -> None:
    with _lock:
        _store.pop(session_id, None)


def _starts_with_tool_result(msg: dict[str, Any]) -> bool:
    content = msg.get("content")
    return (isinstance(content, list) and bool(content)
            and isinstance(content[0], dict) and content[0].get("type") == "tool_result")
