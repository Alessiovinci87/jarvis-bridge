"""Speech-to-text for voice commands (faster-whisper, CPU, int8).

Why here and not only in OpenJarvis: Whisper accepts an *initial prompt* that
biases recognition towards known vocabulary. With the names from the allowlist
("Jarvis", "Spotify", "Visual Studio Code", "U2"…) the `base` model went from
4/8 to 8/8 correct command transcriptions on this machine at the same speed,
while `small` was 4-6× slower. OpenJarvis's endpoint does not expose that
prompt, so the bridge runs its own instance with the same library and model
cache. Audio never leaves the PC.
"""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
from typing import Any

log = logging.getLogger("jarvis.stt")

MODEL_SIZE = os.environ.get("JARVIS_STT_MODEL", "base")

# Extra vocabulary beyond what the allowlist implies. Keep it short: the prompt is
# a hint, not a dictionary (Whisper reads at most ~220 tokens of it).
EXTRA_VOCAB = [
    "Jarvis", "Spotify", "Visual Studio Code", "VS Code", "OpenJarvis", "Jarvis UI",
    "browser", "Download", "timer", "sveglia", "meteo", "U2", "AC/DC", "Pink Floyd",
]


def build_prompt(allowlist: dict[str, dict[str, dict[str, Any]]]) -> str:
    labels: list[str] = []
    for targets in allowlist.values():
        for entry in targets.values():
            label = str(entry.get("label", "")).strip()
            if label and label not in labels:
                labels.append(label)
    words = EXTRA_VOCAB + [l for l in labels if l not in EXTRA_VOCAB]
    # Phrased as example commands: Whisper imitates the style (punctuation, casing) too.
    return (
        "Jarvis, apri Spotify. Apri Visual Studio Code sul progetto Jarvis UI. "
        "Metti One degli U2. Cerca sul browser. Che tempo fa a Milano? Metti un timer di dieci minuti. "
        + ", ".join(words)
        + "."
    )


class SpeechToText:
    def __init__(self, allowlist: dict[str, dict[str, dict[str, Any]]]):
        self.prompt = build_prompt(allowlist)
        self._model: Any = None
        self._lock = threading.Lock()
        self._error: str | None = None

    def load(self) -> bool:
        with self._lock:
            if self._model is not None:
                return True
            try:
                from faster_whisper import WhisperModel

                t = time.time()
                self._model = WhisperModel(MODEL_SIZE, device="cpu", compute_type="int8")
                log.info("whisper %s loaded in %.1fs", MODEL_SIZE, time.time() - t)
                return True
            except Exception as exc:  # pragma: no cover
                self._error = str(exc)
                log.warning("whisper unavailable: %s", exc)
                return False

    def status(self) -> dict[str, Any]:
        return {"available": self._error is None, "loaded": self._model is not None, "model": MODEL_SIZE, "reason": self._error}

    def transcribe(self, data: bytes, suffix: str = ".wav", language: str = "it") -> dict[str, Any]:
        if not self.load():
            raise RuntimeError(self._error or "whisper unavailable")
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as fh:
            fh.write(data)
            path = fh.name
        try:
            t = time.time()
            with self._lock:
                segments, info = self._model.transcribe(
                    path, language=language or None, beam_size=5, initial_prompt=self.prompt, vad_filter=True
                )
                text = " ".join(s.text.strip() for s in segments).strip()
            return {
                "text": text,
                "language": getattr(info, "language", language),
                "confidence": float(getattr(info, "language_probability", 0.0) or 0.0),
                "duration_seconds": float(getattr(info, "duration", 0.0) or 0.0),
                "seconds": round(time.time() - t, 2),
            }
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass
