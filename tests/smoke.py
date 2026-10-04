"""Smoke test for the bridge: refusals must be refused, real actions optional.

Usage:  python tests/smoke.py            # refusals only
        python tests/smoke.py --real     # also opens Spotify, VS Code (jarvis-ui)
"""

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
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


REFUSALS = [
    ("shell action", {"action": "run_shell", "target": "powershell Remove-Item C:\\x"}),
    ("arbitrary path", {"action": "open_folder", "target": "C:\\qualcosa\\inventato"}),
    ("traversal", {"action": "open_project", "target": "../../Windows"}),
    ("unknown app", {"action": "open_app", "target": "notepad"}),
    ("command field", {"command": "powershell Remove-Item C:\\x"}),
]

ok = True
for name, body in REFUSALS:
    status, text = post(body)
    refused = status in (404, 422)
    ok &= refused
    print(f"{'PASS' if refused else 'FAIL'} refuse {name:15s} -> {status} {text[:90]}")

if "--real" in sys.argv:
    for body in ({"action": "open_app", "target": "spotify"}, {"action": "open_project", "target": "jarvis-ui"}):
        status, text = post(body)
        ok &= status == 200
        print(f"{'PASS' if status == 200 else 'FAIL'} real {body['action']}/{body['target']} -> {status} {text[:120]}")

sys.exit(0 if ok else 1)
