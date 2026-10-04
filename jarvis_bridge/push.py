"""Web Push to the phone (and any browser where the Jarvis PWA is installed).

No third party in the loop except the browser vendor's push relay, which only
ever sees an encrypted payload. VAPID keys are generated once and kept in
``data/vapid.json``; subscriptions in ``data/push_subs.json``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.push")

DATA_DIR = Path(os.environ.get("JARVIS_BRAIN_DB") or Path(__file__).resolve().parent.parent / "data" / "brain.db").parent
VAPID_PATH = DATA_DIR / "vapid.json"
SUBS_PATH = DATA_DIR / "push_subs.json"
CONTACT = os.environ.get("JARVIS_PUSH_CONTACT", "mailto:jarvis@localhost")


class Push:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, str] | None = None
        self._subs: list[dict[str, Any]] = self._load_subs()

    # ------------------------------------------------------------------ keys
    def keys(self) -> dict[str, str] | None:
        if self._keys:
            return self._keys
        try:
            if VAPID_PATH.is_file():
                self._keys = json.loads(VAPID_PATH.read_text(encoding="utf-8"))
                return self._keys
            from py_vapid import Vapid, b64urlencode
            from cryptography.hazmat.primitives import serialization

            v = Vapid()
            v.generate_keys()
            priv = v.private_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            pub_raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
            self._keys = {"private_pem": priv, "public_key": b64urlencode(pub_raw)}
            VAPID_PATH.parent.mkdir(parents=True, exist_ok=True)
            VAPID_PATH.write_text(json.dumps(self._keys), encoding="utf-8")
            log.info("push: VAPID keys generated")
            return self._keys
        except Exception as exc:
            log.warning("push unavailable: %s", exc)
            return None

    @property
    def available(self) -> bool:
        return self.keys() is not None

    def public_key(self) -> str | None:
        k = self.keys()
        return k["public_key"] if k else None

    # ---------------------------------------------------------- subscriptions
    def _load_subs(self) -> list[dict[str, Any]]:
        try:
            return json.loads(SUBS_PATH.read_text(encoding="utf-8")) if SUBS_PATH.is_file() else []
        except Exception:
            return []

    def _save_subs(self) -> None:
        SUBS_PATH.parent.mkdir(parents=True, exist_ok=True)
        SUBS_PATH.write_text(json.dumps(self._subs), encoding="utf-8")

    def subscribe(self, sub: dict[str, Any], label: str = "") -> int:
        endpoint = str(sub.get("endpoint", ""))
        if not endpoint.startswith("https://") or not isinstance(sub.get("keys"), dict):
            raise ValueError("subscription non valida")
        with self._lock:
            self._subs = [s for s in self._subs if s.get("endpoint") != endpoint]
            self._subs.append({"endpoint": endpoint, "keys": sub["keys"], "label": label[:60]})
            self._save_subs()
            return len(self._subs)

    def unsubscribe(self, endpoint: str) -> int:
        with self._lock:
            self._subs = [s for s in self._subs if s.get("endpoint") != endpoint]
            self._save_subs()
            return len(self._subs)

    def status(self) -> dict[str, Any]:
        return {"available": self.available, "subscriptions": len(self._subs), "devices": [s.get("label") or "?" for s in self._subs]}

    # ------------------------------------------------------------------ send
    def send(self, title: str, body: str, *, url: str = "/", tag: str = "jarvis") -> int:
        """Delivers to every subscription; dead ones (410/404) are dropped. Returns deliveries."""
        keys = self.keys()
        if not keys or not self._subs:
            return 0
        from pywebpush import WebPushException, webpush

        payload = json.dumps({"title": title, "body": body, "url": url, "tag": tag}, ensure_ascii=False)
        sent = 0
        dead: list[str] = []
        for s in list(self._subs):
            try:
                webpush(subscription_info={"endpoint": s["endpoint"], "keys": s["keys"]}, data=payload,
                        vapid_private_key=keys["private_pem"], vapid_claims={"sub": CONTACT}, ttl=3600, timeout=15)
                sent += 1
            except WebPushException as exc:
                code = getattr(getattr(exc, "response", None), "status_code", None)
                if code in (404, 410):
                    dead.append(s["endpoint"])
                log.warning("push to %s failed: %s", (s.get("label") or s["endpoint"][:40]), str(exc)[:120])
            except Exception as exc:
                log.warning("push failed: %s", str(exc)[:120])
        for e in dead:
            self.unsubscribe(e)
        return sent
