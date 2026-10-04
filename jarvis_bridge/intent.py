"""Intent classifier: free text → one allowlisted (action, target, query) or none.

Talks to Ollama directly (not through OpenJarvis) with a compact JSON prompt
built from the allowlist, thinking disabled and `format=json`. Measured on the
i7-8665U with qwen3.5:4b: ~17-20 s per call versus 40-80 s for the OpenAI-style
tool schema. The answer is validated against the allowlist before it is
returned, so the model can only ever *pick*, never *do*.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

log = logging.getLogger("jarvis.intent")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
PREFERRED_MODEL = os.environ.get("JARVIS_INTENT_MODEL", "qwen3.5:4b")
QUERY_RE = re.compile(r"^[\w '’\-.,&!?:]{1,80}$", re.UNICODE)

_HINTS = {
    "open_project": {"jarvis-ui": "frontend/UI di Jarvis", "openjarvis": "backend di Jarvis"},
}
_QUERY_HINT = {
    "play_music": "query=brano e/o artista",
    "web_search": "query=testo da cercare",
    "timer": "set richiede query=durata ('10 minuti') o orario ('alle 7:30'); cancel/list senza query",
    "weather": "query=città e/o 'domani' (opzionale)",
    "find_file": "query=parte del nome del file",
}


def build_prompt(allowlist: dict[str, dict[str, dict[str, Any]]]) -> str:
    lines = [
        'Classifica la richiesta dell\'utente in UN comando dalla lista, oppure none. Rispondi SOLO con JSON: {"intent": "...", "target": "...", "query": "..."}.',
        "Comandi disponibili (intent: target...):",
    ]
    for action, targets in allowlist.items():
        hints = _HINTS.get(action, {})
        tlist = "|".join(f"{t}({hints[t]})" if t in hints else t for t in targets)
        extra = f" — {_QUERY_HINT[action]}" if action in _QUERY_HINT else ""
        lines.append(f"- {action}: {tlist}{extra}")
    lines.append('Se nessun comando è adatto (domande, chiacchiere, azioni non in lista): {"intent": "none"}. Non inventare mai target.')
    return "\n".join(lines)


@dataclass
class IntentResult:
    intent: dict[str, Any] | None
    model: str
    trusted: bool
    seconds: float
    raw: str


class IntentClassifier:
    def __init__(self, allowlist: dict[str, dict[str, dict[str, Any]]]):
        self.allowlist = allowlist
        self.prompt = build_prompt(allowlist)
        self._model: str | None = None

    # ------------------------------------------------------------- model --
    def available_models(self) -> list[str]:
        try:
            with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=4) as r:
                return [m["name"] for m in json.load(r).get("models", [])]
        except Exception:
            return []

    def model(self) -> str | None:
        if self._model:
            return self._model
        names = self.available_models()
        if PREFERRED_MODEL in names:
            self._model = PREFERRED_MODEL
        elif names:
            self._model = names[0]
        return self._model

    def status(self) -> dict[str, Any]:
        m = self.model()
        return {"available": m is not None, "model": m, "trusted": m == PREFERRED_MODEL, "preferred": PREFERRED_MODEL}

    # ----------------------------------------------------------- classify --
    def classify(self, text: str, context: str | None = None) -> IntentResult:
        model = self.model()
        if not model:
            raise RuntimeError("nessun modello Ollama disponibile")
        if context:
            # Last desktop command, e.g. "find_file folders «ale del pc»": lets "no, la cartella Ale" be read as a correction.
            text = f"{text[:300]}\n(Contesto: l'ultimo comando eseguito era {context[:120]}. Se la frase lo corregge, riproponi quel comando con il nuovo target.)"
        body = {
            "model": model,
            "stream": False,
            "keep_alive": "30m",
            "think": False,
            "format": "json",
            "options": {"temperature": 0, "num_predict": 80},
            "messages": [{"role": "system", "content": self.prompt}, {"role": "user", "content": text[:300]}],
        }
        t = time.time()
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(), headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            # Older Ollama without `think`: retry without it.
            if e.code == 400 and "think" in body:
                body.pop("think")
                req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(body).encode(), headers={"content-type": "application/json"})
                with urllib.request.urlopen(req, timeout=180) as r:
                    data = json.load(r)
            else:
                raise
        raw = str(data.get("message", {}).get("content", ""))
        intent = self._validate(raw)
        log.info("intent %.1fs model=%s text=%r -> %s", time.time() - t, model, text[:60], intent)
        return IntentResult(intent=intent, model=model, trusted=model == PREFERRED_MODEL, seconds=time.time() - t, raw=raw)

    def _validate(self, raw: str) -> dict[str, Any] | None:
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except ValueError:
            return None
        action = str(parsed.get("intent") or parsed.get("action") or "").strip()
        if action not in self.allowlist:
            return None
        targets = self.allowlist[action]
        target = str(parsed.get("target") or "").strip()
        target = re.sub(r"\(.*?\)", "", target).strip()  # "openjarvis(backend)" → "openjarvis"
        if not target and len(targets) == 1:
            target = next(iter(targets))
        if target not in targets:
            return None
        query = parsed.get("query")
        query = str(query).strip() if isinstance(query, str) else ""
        query = re.sub(r"\s+", " ", query)
        needs_query = action in ("play_music", "web_search", "find_file") or (action == "timer" and target == "set")
        if needs_query and not query:
            return None
        if query and not QUERY_RE.match(query):
            return None
        out: dict[str, Any] = {"action": action, "target": target}
        if query and action in ("play_music", "web_search", "find_file", "timer", "weather"):
            out["query"] = query
        return out
