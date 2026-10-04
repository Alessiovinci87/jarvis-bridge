"""Routines: named sequences of allowlisted actions, triggered by a phrase.

Defined in ``routines.json`` next to the allowlist. Each step is validated
against the allowlist at load time, so a routine can never do anything a single
spoken command could not. The UI executes the steps one by one through
``POST /actions`` (each one audited), after the user says one of the phrases.
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.routines")

ROUTINES_PATH = Path(__file__).resolve().parent.parent / "routines.json"
_QUERY_RE = re.compile(r"^[\w '’\-.,&!?:/]{1,80}$", re.UNICODE)


def load_routines(allowlist: dict[str, dict[str, dict[str, Any]]]) -> list[dict[str, Any]]:
    if not ROUTINES_PATH.is_file():
        return []
    try:
        raw = json.loads(ROUTINES_PATH.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("routines.json unreadable: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for rid, spec in raw.items():
        if rid.startswith("_") or not isinstance(spec, dict):
            continue
        steps: list[dict[str, Any]] = []
        for s in spec.get("steps", []):
            action, target = str(s.get("action", "")), str(s.get("target", ""))
            if action not in allowlist or target not in allowlist[action]:
                log.warning("routine %s: step %s/%s not in allowlist, skipped", rid, action, target)
                continue
            step: dict[str, Any] = {"action": action, "target": target}
            q = s.get("query")
            if isinstance(q, str) and _QUERY_RE.match(q):
                step["query"] = q
            steps.append(step)
        phrases = [str(p).strip().lower() for p in spec.get("phrases", []) if str(p).strip()]
        if steps and phrases:
            out.append({"id": re.sub(r"[^a-z0-9_-]", "", rid.lower())[:32], "label": str(spec.get("label", rid)), "phrases": phrases, "steps": steps})
    log.info("routines: %d loaded", len(out))
    return out
