"""Conversation straight from the bridge to Ollama, streamed.

Replaces the OpenJarvis hop for ordinary chat: one fewer process, one fewer
network leg, and tokens reach the UI as they are generated so Jarvis can start
speaking after the first sentence. The UI falls back to OpenJarvis when this
endpoint reports the model as unavailable.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from typing import Any

log = logging.getLogger("jarvis.chat")

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")


def chat_model() -> str:
    return os.environ.get("JARVIS_CHAT_MODEL") or os.environ.get("JARVIS_INTENT_MODEL") or "qwen3.5:2b"


def available_models() -> list[str]:
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=4) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return []


def status() -> dict[str, Any]:
    names = available_models()
    model = chat_model()
    return {"available": model in names, "model": model, "models": names, "ollama": OLLAMA_URL}


def stream(messages: list[dict[str, str]], *, model: str | None = None, temperature: float = 0.7, max_tokens: int = 350) -> Iterator[dict[str, Any]]:
    """Yields {"delta": str} chunks, then {"done": True, "model", "seconds", "tokens"}; {"error": str} on failure."""
    model = model or chat_model()
    body = {
        "model": model, "stream": True, "keep_alive": "30m", "think": False,
        "options": {"temperature": temperature, "num_predict": max_tokens},
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages][-30:],
    }
    t0 = time.time()
    tokens = 0

    def request(payload: dict[str, Any]):
        req = urllib.request.Request(f"{OLLAMA_URL}/api/chat", data=json.dumps(payload).encode(), headers={"content-type": "application/json"})
        return urllib.request.urlopen(req, timeout=300)

    try:
        try:
            resp = request(body)
        except urllib.error.HTTPError as e:
            if e.code == 400 and "think" in body:
                body.pop("think")
                resp = request(body)
            else:
                raise
        with resp:
            for raw in resp:
                if not raw.strip():
                    continue
                data = json.loads(raw)
                piece = data.get("message", {}).get("content") or ""
                if piece:
                    tokens += 1
                    yield {"delta": piece}
                if data.get("done"):
                    tokens = int(data.get("eval_count") or tokens)
                    break
        yield {"done": True, "model": model, "seconds": round(time.time() - t0, 1), "tokens": tokens}
    except Exception as exc:
        log.warning("chat stream failed: %s", exc)
        yield {"error": str(exc)[:200]}
