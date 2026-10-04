"""Feed WAV files through the wake-word model (no microphone needed).

Usage: python tests/wake_offline.py positive.wav [negative.wav ...]
Prints the max score per file; the first file is expected to trigger (>= 0.5).
"""

import sys
import wave

import numpy as np
from openwakeword.model import Model

FRAME = 1280


def max_score(model: Model, path: str) -> float:
    with wave.open(path, "rb") as wf:
        assert wf.getframerate() == 16000 and wf.getnchannels() == 1 and wf.getsampwidth() == 2, "need 16 kHz mono 16-bit"
        pcm = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    # Pad with a second of silence so the model sees the whole phrase.
    pcm = np.concatenate([np.zeros(16000, dtype=np.int16), pcm, np.zeros(16000, dtype=np.int16)])
    model.reset()
    best = 0.0
    for i in range(0, len(pcm) - FRAME + 1, FRAME):
        best = max(best, float(model.predict(pcm[i : i + FRAME])["hey_jarvis"]))
    return best


model = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
ok = True
for idx, path in enumerate(sys.argv[1:]):
    score = max_score(model, path)
    expect_hit = idx == 0
    hit = score >= 0.5
    ok &= hit == expect_hit
    print(f"{'PASS' if hit == expect_hit else 'FAIL'} {path}: max score {score:.3f} ({'wake' if hit else 'no wake'})")
sys.exit(0 if ok else 1)
