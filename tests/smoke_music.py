"""Music/media smoke test. Refusals always; real actions with --real
(opens the Spotify search for "One U2" and toggles mute twice = net no-op)."""

import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8765"


def post(body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{BASE}/actions", data=json.dumps(body).encode(), headers={"content-type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


ok = True
REFUSALS = [
    ("shell chars in query", {"action": "play_music", "target": "spotify", "query": "x; rm -rf /"}),
    ("path in query", {"action": "play_music", "target": "spotify", "query": "C:/Windows"}),
    ("query too long", {"action": "play_music", "target": "spotify", "query": "a" * 81}),
    ("missing query", {"action": "play_music", "target": "spotify"}),
    ("unknown media key", {"action": "media", "target": "shutdown"}),
    ("query on open_app", {"action": "open_app", "target": "vscode", "query": "something"}),
]
for name, body in REFUSALS:
    status, text = post(body)
    refused = status in (404, 422)
    ok &= refused
    print(f"{'PASS' if refused else 'FAIL'} refuse {name:20s} -> {status} {text[:80]}")

if "--real" in sys.argv:
    for body in (
        {"action": "play_music", "target": "spotify", "query": "One U2"},
        {"action": "media", "target": "mute"},
        {"action": "media", "target": "mute"},
    ):
        status, text = post(body)
        ok &= status == 200
        print(f"{'PASS' if status == 200 else 'FAIL'} real {body['action']}/{body['target']} -> {status} {text[:120]}")

sys.exit(0 if ok else 1)
