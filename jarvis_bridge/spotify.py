"""Spotify user session (OAuth 2.0 Authorization Code with PKCE).

Two levels of access exist in the bridge:

* **App level** (client credentials, see ``actions._spotify_track_uri``): search
  only, no login. Used as a fallback.
* **User level** (this module): after a one-time login in the browser the bridge
  keeps a refresh token in ``~/.jarvis-bridge/spotify_token.json`` and can
  control playback through the Web API — play a URI on the active device,
  pause/resume, next/previous, volume, what's playing. Needs Spotify Premium
  for playback control (Spotify's rule, not ours).

Only the bridge talks to Spotify; the UI only sees whether a session exists.
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
from pathlib import Path
from typing import Any

log = logging.getLogger("jarvis.spotify")

SCOPES = "user-read-playback-state user-modify-playback-state user-read-currently-playing playlist-read-private"
TOKEN_PATH = Path(os.environ.get("JARVIS_BRIDGE_HOME", Path.home() / ".jarvis-bridge")) / "spotify_token.json"
AUTH_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"


class SpotifyError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SpotifySession:
    def __init__(self, redirect_uri: str):
        self.redirect_uri = redirect_uri
        self._pending: dict[str, str] = {}  # state -> code_verifier
        self._token: dict[str, Any] | None = self._load()

    # ----------------------------------------------------------------- config
    @property
    def client_id(self) -> str | None:
        return os.environ.get("SPOTIFY_CLIENT_ID")

    @property
    def client_secret(self) -> str | None:
        return os.environ.get("SPOTIFY_CLIENT_SECRET")

    @property
    def configured(self) -> bool:
        return bool(self.client_id)

    @property
    def logged_in(self) -> bool:
        return bool(self._token and self._token.get("refresh_token"))

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured,
            "logged_in": self.logged_in,
            "scopes": SCOPES.split() if self.logged_in else [],
            "token_file": str(TOKEN_PATH),
        }

    # ------------------------------------------------------------------ oauth
    def login_url(self) -> str:
        if not self.client_id:
            raise SpotifyError("not_configured", "SPOTIFY_CLIENT_ID mancante nel file .env")
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        state = secrets.token_urlsafe(16)
        self._pending[state] = verifier
        params = {
            "client_id": self.client_id,
            "response_type": "code",
            "redirect_uri": self.redirect_uri,
            "scope": SCOPES,
            "code_challenge_method": "S256",
            "code_challenge": challenge,
            "state": state,
        }
        return f"{AUTH_URL}?{urllib.parse.urlencode(params)}"

    def complete_login(self, code: str, state: str) -> None:
        verifier = self._pending.pop(state, None)
        if not verifier:
            raise SpotifyError("bad_state", "stato OAuth sconosciuto o scaduto")
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
            "client_id": self.client_id,
            "code_verifier": verifier,
        }
        self._token = self._token_request(data)
        self._save()
        log.info("spotify: user login completed")

    def logout(self) -> None:
        self._token = None
        try:
            TOKEN_PATH.unlink()
        except FileNotFoundError:
            pass

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        if self.client_secret:
            auth = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {auth}"
        req = urllib.request.Request(TOKEN_URL, data=urllib.parse.urlencode(data).encode(), headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                tok = json.load(r)
        except urllib.error.HTTPError as e:
            raise SpotifyError("token_error", e.read().decode()[:200]) from e
        tok["expires_at"] = time.time() + int(tok.get("expires_in", 3600)) - 30
        if "refresh_token" not in tok and self._token:
            tok["refresh_token"] = self._token.get("refresh_token")
        return tok

    def _access_token(self) -> str:
        if not self._token:
            raise SpotifyError("not_logged_in", "Spotify non collegato: apri /spotify/login")
        if time.time() >= float(self._token.get("expires_at", 0)):
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self._token["refresh_token"],
                "client_id": self.client_id or "",
            }
            self._token = self._token_request(data)
            self._save()
        return str(self._token["access_token"])

    def _load(self) -> dict[str, Any] | None:
        try:
            return json.loads(TOKEN_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def _save(self) -> None:
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(json.dumps(self._token), encoding="utf-8")

    # -------------------------------------------------------------------- api
    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, params: dict[str, str] | None = None) -> Any:
        url = f"{API}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json"},
            method=method,
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            detail = e.read().decode()[:200]
            if e.code == 404:
                raise SpotifyError("no_device", "nessun dispositivo Spotify attivo") from e
            if e.code == 403:
                raise SpotifyError("premium_required", "il controllo della riproduzione richiede Spotify Premium") from e
            raise SpotifyError("api_error", f"{e.code} {detail}") from e

    def search_track(self, query: str) -> dict[str, Any] | None:
        res = self._call("GET", "/search", params={"q": query, "type": "track", "limit": "1"})
        items = (res or {}).get("tracks", {}).get("items", [])
        if not items:
            return None
        t = items[0]
        return {"uri": t["uri"], "name": t["name"], "artist": ", ".join(a["name"] for a in t["artists"])}

    def devices(self) -> list[dict[str, Any]]:
        return list((self._call("GET", "/me/player/devices") or {}).get("devices", []))

    def _device_id(self) -> str | None:
        devs = self.devices()
        for d in devs:
            if d.get("is_active"):
                return str(d["id"])
        for d in devs:
            if "computer" in str(d.get("type", "")).lower():
                return str(d["id"])
        return str(devs[0]["id"]) if devs else None

    def play_uri(self, uri: str) -> None:
        dev = self._device_id()
        params = {"device_id": dev} if dev else None
        body: dict[str, Any] = {"context_uri": uri} if not uri.startswith("spotify:track:") else {"uris": [uri]}
        self._call("PUT", "/me/player/play", body=body, params=params)

    def pause(self) -> None:
        self._call("PUT", "/me/player/pause")

    def resume(self) -> None:
        self._call("PUT", "/me/player/play")

    def play_pause(self) -> str:
        state = self._call("GET", "/me/player")
        if state and state.get("is_playing"):
            self.pause()
            return "in pausa"
        self.resume()
        return "riparte"

    def next(self) -> None:
        self._call("POST", "/me/player/next")

    def previous(self) -> None:
        self._call("POST", "/me/player/previous")

    def volume_step(self, delta: int) -> int:
        state = self._call("GET", "/me/player") or {}
        current = int(((state.get("device") or {}).get("volume_percent")) or 50)
        target = max(0, min(100, current + delta))
        self._call("PUT", "/me/player/volume", params={"volume_percent": str(target)})
        return target

    def player(self) -> dict[str, Any]:
        return self._call("GET", "/me/player") or {}

    def is_playing(self) -> bool:
        return bool(self.player().get("is_playing"))

    def get_volume(self) -> int | None:
        dev = self.player().get("device") or {}
        v = dev.get("volume_percent")
        return int(v) if v is not None else None

    def set_volume(self, percent: int) -> None:
        percent = max(0, min(100, int(percent)))
        self._call("PUT", "/me/player/volume", params={"volume_percent": str(percent)})

    def now_playing(self) -> dict[str, Any] | None:
        state = self._call("GET", "/me/player/currently-playing")
        if not state or not state.get("item"):
            return None
        item = state["item"]
        return {
            "name": item["name"],
            "artist": ", ".join(a["name"] for a in item.get("artists", [])),
            "album": (item.get("album") or {}).get("name"),
            "is_playing": bool(state.get("is_playing")),
        }
