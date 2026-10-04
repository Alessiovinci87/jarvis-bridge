"""Offline wake-word engine ("hey jarvis") built on openWakeWord + ONNX Runtime.

* Audio never leaves the machine: the microphone is read with sounddevice at
  16 kHz mono and fed to a small ONNX model in 80 ms frames.
* The engine runs in its own thread only while at least one client asked for it
  (``start``); it releases the microphone on ``stop``.
* Detections are broadcast to asyncio queues (one per SSE subscriber).
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("jarvis.wake")

SAMPLE_RATE = 16_000
FRAME = 1280  # 80 ms at 16 kHz, the frame size openWakeWord expects
MODEL_NAME = "hey_jarvis"


@dataclass
class WakeStatus:
    available: bool
    running: bool
    model: str
    threshold: float
    reason: str | None = None
    detections: int = 0
    last_score: float = 0.0
    last_detection: float | None = None
    extra: dict[str, Any] = field(default_factory=dict)


class WakeEngine:
    def __init__(self, threshold: float = 0.5, cooldown_s: float = 2.0, vad_threshold: float = 0.3):
        self.threshold = threshold
        self.cooldown_s = cooldown_s
        self.vad_threshold = vad_threshold
        self._model: Any = None
        self._stream: Any = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._subs: set[asyncio.Queue] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._available: bool | None = None
        self._reason: str | None = None
        self._detections = 0
        self._last_score = 0.0
        self._last_detection: float | None = None

    # ------------------------------------------------------------------ setup
    def probe(self) -> bool:
        """Load the model once; report whether the local wake word can run."""
        if self._available is not None:
            return self._available
        try:
            import sounddevice as sd  # noqa: F401
            from openwakeword.model import Model
            from openwakeword.utils import download_models

            download_models(model_names=[MODEL_NAME])
            self._model = Model(
                wakeword_models=[MODEL_NAME],
                inference_framework="onnx",
                vad_threshold=self.vad_threshold,
            )
            self._available = True
        except Exception as exc:  # pragma: no cover - depends on host
            log.warning("local wake word unavailable: %s", exc)
            self._available = False
            self._reason = str(exc)
        return self._available

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    # --------------------------------------------------------------- control
    def start(self) -> bool:
        if not self.probe():
            return False
        with self._lock:
            if self._thread and self._thread.is_alive():
                return True
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="wake-word", daemon=True)
            self._thread.start()
        return True

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            thread = self._thread
            self._thread = None
        if thread:
            thread.join(timeout=2)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def status(self) -> WakeStatus:
        return WakeStatus(
            available=bool(self.probe()),
            running=self.running,
            model=MODEL_NAME,
            threshold=self.threshold,
            reason=self._reason,
            detections=self._detections,
            last_score=round(self._last_score, 3),
            last_detection=self._last_detection,
        )

    # ------------------------------------------------------------ subscribers
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=16)
        self._subs.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subs.discard(q)

    def _broadcast(self, event: dict[str, Any]) -> None:
        loop = self._loop
        if not loop:
            return
        for q in list(self._subs):
            try:
                loop.call_soon_threadsafe(q.put_nowait, event)
            except Exception:  # queue full or loop closed
                pass

    # ------------------------------------------------------------------ loop
    def _run(self) -> None:
        import numpy as np
        import sounddevice as sd

        model = self._model
        model.reset()
        last_fire = 0.0
        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16", blocksize=FRAME) as stream:
                self._stream = stream
                self._broadcast({"type": "status", "running": True})
                while not self._stop.is_set():
                    data, _overflow = stream.read(FRAME)
                    frame = np.frombuffer(data, dtype=np.int16)
                    scores = model.predict(frame)
                    score = float(scores.get(MODEL_NAME, 0.0))
                    self._last_score = score
                    now = time.monotonic()
                    if score >= self.threshold and now - last_fire >= self.cooldown_s:
                        last_fire = now
                        self._detections += 1
                        self._last_detection = time.time()
                        log.info("wake word detected (score %.2f)", score)
                        self._broadcast({"type": "wake", "score": round(score, 3), "model": MODEL_NAME})
                        model.reset()
        except Exception as exc:  # microphone missing / busy
            log.error("wake engine stopped: %s", exc)
            self._reason = str(exc)
            self._broadcast({"type": "error", "message": str(exc)})
        finally:
            self._stream = None
            self._broadcast({"type": "status", "running": False})
