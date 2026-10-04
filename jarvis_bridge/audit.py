"""Persistent audit log for the Local Action Bridge.

One JSON object per line, appended to ``jarvis-bridge/.jarvis-run/actions.log``
(override with ``JARVIS_AUDIT_LOG``). The file is runtime state and is excluded
from Git.

What is recorded
----------------
* every request that could change something on the PC (``POST`` endpoints,
  Spotify login/callback), with the HTTP status it got;
* every desktop action, with ``accepted`` (passed validation / allowlist),
  ``executed`` (the runner actually ran to completion) and ``result``/``error``.

What is deliberately *not* recorded: shell commands (the bridge never builds
any), file system paths, request bodies other than the ``action``/``target``
ids and the short free-text ``query`` an action takes, tokens or audio.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.audit")

_DEFAULT_PATH = Path(__file__).resolve().parent.parent / ".jarvis-run" / "actions.log"
LOG_PATH = Path(os.environ.get("JARVIS_AUDIT_LOG") or _DEFAULT_PATH)

_MAX_TEXT = 200
_lock = threading.Lock()
_last_executed: dict[str, Any] | None = None
_last_event: dict[str, Any] | None = None


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > _MAX_TEXT:
        return value[: _MAX_TEXT - 1] + "…"
    return value


def _write(event: dict[str, Any]) -> None:
    line = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _lock, LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:  # never let logging break an action
        log.error("audit log write failed (%s): %s", LOG_PATH, exc)


def record(
    kind: str,
    *,
    source: str | None = None,
    action: str | None = None,
    target: str | None = None,
    query: str | None = None,
    accepted: bool | None = None,
    executed: bool | None = None,
    result: str | None = None,
    error: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Append one event. ``kind`` is ``action``, ``request`` or ``bridge``."""
    global _last_executed, _last_event
    event: dict[str, Any] = {
        "ts": datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds"),
        "kind": kind,
        "source": source,
        "action": action,
        "target": target,
        "accepted": accepted,
        "executed": executed,
        "result": _clip(result),
        "error": _clip(error),
    }
    if query is not None:
        event["query"] = _clip(query)
    for key, value in extra.items():
        if value is not None:
            event[key] = _clip(value)
    _write(event)
    _last_event = event
    if kind == "action" and executed:
        _last_executed = event
    return event


def source_of(request: Any) -> str:
    """Compact origin string: client host + Origin/Referer header when present."""
    host = request.client.host if request.client else "?"
    user = request.headers.get("tailscale-user-login")
    if user:
        host = f"tailnet:{user}@{request.headers.get('x-forwarded-for', host)}"
    origin = request.headers.get("origin") or request.headers.get("referer") or ""
    ua = request.headers.get("user-agent", "")
    parts = [host]
    if origin:
        parts.append(origin[:120])
    elif ua:
        parts.append(ua[:60])
    return " ".join(parts)


def last_executed() -> dict[str, Any] | None:
    return _last_executed


def last_event() -> dict[str, Any] | None:
    return _last_event


def summary(event: dict[str, Any] | None) -> str | None:
    """Short human label for the HUD, e.g. ``open_app/vscode``."""
    if not event:
        return None
    label = f"{event.get('action')}/{event.get('target')}"
    if event.get("query"):
        label += f" «{event['query']}»"
    return label
