"""Google Calendar + Gmail, read-only, for the morning briefing.

Same shape as the Spotify session: OAuth 2.0 Authorization Code with PKCE, the
browser lands on ``/google/callback`` served by the bridge, the refresh token is
kept in ``~/.jarvis-bridge/google_token.json``. Only the bridge talks to Google;
the UI sees ``status()`` and the digest from ``today()``.

Scopes are *read-only* on purpose: Jarvis can tell what is on the calendar and
in the inbox, never create, send or delete anything.

Setup (one-off, by the user):
1. https://console.cloud.google.com → new project → "APIs & Services".
2. Enable **Google Calendar API** and **Gmail API**.
3. OAuth consent screen: External, add your own account as test user.
4. Credentials → OAuth client ID → type **Desktop app**. Copy client id/secret
   into ``jarvis-bridge/.env`` as GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET.
5. Open http://127.0.0.1:8765/google/login once and grant access.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.google")

SCOPES = ("https://www.googleapis.com/auth/calendar.readonly", "https://www.googleapis.com/auth/gmail.readonly")
TOKEN_PATH = Path(os.environ.get("JARVIS_BRIDGE_HOME", Path.home() / ".jarvis-bridge")) / "google_token.json"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
CAL_API = "https://www.googleapis.com/calendar/v3"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1"


class GoogleError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class GoogleSession:
    def __init__(self, redirect_uri: str):
        self.redirect_uri = redirect_uri
        self._pending: dict[str, str] = {}  # state -> code_verifier
        self._token: dict[str, Any] | None = self._load()

    # ----------------------------------------------------------------- config
    @property
    def client_id(self) -> str | None:
        return os.environ.get("GOOGLE_CLIENT_ID") or None

    @property
    def client_secret(self) -> str | None:
        return os.environ.get("GOOGLE_CLIENT_SECRET") or None

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    @property
    def logged_in(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "logged_in": self.logged_in,
            "scopes": list(SCOPES) if self.logged_in else [],
            "account": (self._token or {}).get("email"),
            "token_file": str(TOKEN_PATH),
            "setup": None if self.configured else "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET mancanti in jarvis-bridge/.env (vedi google.py)",
        }

    # ------------------------------------------------------------------ oauth
    def login_url(self) -> str:
        if not self.configured:
            raise GoogleError("not_configured", "GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET mancanti nel file .env")
        verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).rstrip(b"=").decode()
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        self._pending = {state: verifier}
        params = {
            "client_id": self.client_id, "response_type": "code", "redirect_uri": self.redirect_uri,
            "scope": " ".join(SCOPES), "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
            "access_type": "offline", "prompt": "consent", "include_granted_scopes": "true",
        }
        return AUTH_URL + "?" + urllib.parse.urlencode(params)

    def complete_login(self, code: str, state: str) -> None:
        verifier = self._pending.pop(state, None)
        if not verifier:
            raise GoogleError("bad_state", "stato OAuth sconosciuto o scaduto: riprova il login")
        data = {
            "grant_type": "authorization_code", "code": code, "redirect_uri": self.redirect_uri,
            "client_id": self.client_id, "client_secret": self.client_secret, "code_verifier": verifier,
        }
        token = self._post_token(data)
        if "refresh_token" not in token:
            raise GoogleError("no_refresh", "Google non ha restituito un refresh token: revoca l'accesso e riprova")
        token["obtained_at"] = time.time()
        self._token = token
        try:
            self._token["email"] = self._profile_email()
        except Exception:
            pass
        self._save()
        log.info("google: login completed")

    def logout(self) -> None:
        self._token = None
        if TOKEN_PATH.exists():
            TOKEN_PATH.unlink()

    def _access_token(self) -> str:
        if not self._token:
            raise GoogleError("not_logged_in", "Google non collegato")
        if time.time() - float(self._token.get("obtained_at", 0)) < float(self._token.get("expires_in", 3600)) - 60:
            return str(self._token["access_token"])
        data = {
            "grant_type": "refresh_token", "refresh_token": self._token["refresh_token"],
            "client_id": self.client_id, "client_secret": self.client_secret,
        }
        fresh = self._post_token(data)
        self._token.update(fresh)
        self._token["obtained_at"] = time.time()
        self._save()
        return str(self._token["access_token"])

    def _post_token(self, data: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(data).encode(), headers={"content-type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            raise GoogleError("token_error", f"Google token error {e.code}: {body}") from e

    def _load(self) -> dict[str, Any] | None:
        try:
            return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None

    def _save(self) -> None:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(json.dumps(self._token), encoding="utf-8")

    # -------------------------------------------------------------------- api
    def _get(self, url: str, params: dict[str, Any] | None = None) -> Any:
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"authorization": f"Bearer {self._access_token()}"})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                raise GoogleError("not_logged_in", "sessione Google scaduta: rifai il login") from e
            raise GoogleError("api_error", f"Google API {e.code}") from e

    def _profile_email(self) -> str | None:
        return self._get(f"{GMAIL_API}/users/me/profile").get("emailAddress")

    def events_today(self, now: datetime | None = None, days: int = 1) -> list[dict[str, Any]]:
        """Primary calendar, from now to the end of `days` days: start, title, location."""
        now = now or datetime.now().astimezone()
        end = (now + timedelta(days=days)).replace(hour=23, minute=59, second=59)
        data = self._get(f"{CAL_API}/calendars/primary/events", {
            "timeMin": now.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true", "orderBy": "startTime", "maxResults": 15,
        })
        out = []
        for ev in data.get("items", []):
            start = ev.get("start", {})
            when = start.get("dateTime") or start.get("date")
            out.append({"start": when, "all_day": "date" in start and "dateTime" not in start,
                        "title": ev.get("summary") or "(senza titolo)", "location": ev.get("location")})
        return out

    def unread_mail(self, limit: int = 5) -> dict[str, Any]:
        """Unread inbox mail from the last day: count + sender/subject of the newest few. Bodies are never read."""
        lst = self._get(f"{GMAIL_API}/users/me/messages", {"q": "is:unread in:inbox newer_than:1d", "maxResults": limit})
        ids = [m["id"] for m in lst.get("messages", [])]
        recent = []
        for mid in ids:
            msg = self._get(f"{GMAIL_API}/users/me/messages/{mid}?format=metadata&metadataHeaders=From&metadataHeaders=Subject")
            headers = {h["name"].lower(): h["value"] for h in msg.get("payload", {}).get("headers", [])}
            sender = headers.get("from", "")
            sender = sender.split("<")[0].strip(' "') or sender
            recent.append({"from": sender, "subject": headers.get("subject", "(senza oggetto)")})
        return {"unread": int(lst.get("resultSizeEstimate", len(ids))), "recent": recent}

    def today(self) -> dict[str, Any]:
        return {"events": self.events_today(), "mail": self.unread_mail()}

    def briefing_lines(self) -> list[str]:
        """Spoken fragments for the briefing. Empty when not logged in or on any error."""
        if not self.logged_in:
            return []
        lines: list[str] = []
        try:
            events = self.events_today()
            if events:
                parts = []
                for ev in events[:5]:
                    if ev["all_day"]:
                        parts.append(f"{ev['title']} tutto il giorno")
                    else:
                        t = datetime.fromisoformat(ev["start"]).strftime("%H:%M")
                        parts.append(f"{ev['title']} alle {t}")
                lines.append(f"in calendario {'hai' if len(parts) > 1 else 'c’è'}: " + "; ".join(parts))
            else:
                lines.append("il calendario di oggi è libero")
        except Exception as exc:
            log.info("briefing calendar skipped: %s", exc)
        try:
            mail = self.unread_mail(limit=3)
            n = mail["unread"]
            if n:
                who = ", ".join(m["from"] for m in mail["recent"][:3] if m["from"])
                lines.append(f"{n} mail non lett{'a' if n == 1 else 'e'} da ieri" + (f", tra cui da {who}" if who else ""))
        except Exception as exc:
            log.info("briefing mail skipped: %s", exc)
        return lines
