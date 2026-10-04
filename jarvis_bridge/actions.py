"""Allowlisted desktop actions.

Security model
--------------
* The only inputs accepted from outside are an ``action`` id and a ``target`` id.
* Both must exist in ``allowlist.json``; everything else is refused before any
  OS call happens.
* Launching never goes through a shell (no ``shell=True``, no ``cmd /c``, no
  PowerShell).  Each allowlist entry has a *kind* and the code for that kind
  decides exactly which executable runs and with which fixed arguments.
* Paths used for launching come only from the allowlist file, never from the
  request.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import time
import urllib.parse
import urllib.request
import shutil
import subprocess
import sys
import webbrowser
import winreg
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import logging

log = logging.getLogger("jarvis.actions")

ALLOWLIST_PATH = Path(__file__).resolve().parent.parent / "allowlist.json"

ACTIONS = ("open_app", "open_folder", "open_project", "play_music", "media", "web_search", "close_app", "timer", "weather", "find_file")
QUERY_ACTIONS = ("play_music", "web_search", "timer", "weather", "find_file")

# Free-text music query: letters (any script), digits, spaces, a little punctuation. Max 80 chars.
MUSIC_QUERY_RE = re.compile(r"^[\w '’\-.,&!?:/]{1,80}$", re.UNICODE)

# Virtual-key codes for Windows media keys (keybd_event). Nothing else is ever sent.
_MEDIA_KEYS = {
    "play_pause": 0xB3,
    "next": 0xB0,
    "previous": 0xB1,
    "volume_up": 0xAF,
    "volume_down": 0xAE,
    "mute": 0xAD,
}

# Windows: start the child fully detached so the bridge never waits on it.
_DETACHED = 0
if sys.platform == "win32":
    _DETACHED = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)


class ActionError(Exception):
    """Refused or failed action; ``code`` is machine readable."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ActionResult:
    action: str
    target: str
    label: str
    detail: str


def load_allowlist() -> dict[str, dict[str, dict[str, Any]]]:
    with ALLOWLIST_PATH.open(encoding="utf-8") as fh:
        raw = json.load(fh)
    return {a: dict(raw.get(a, {})) for a in ACTIONS}


def catalog(allowlist: dict[str, dict[str, dict[str, Any]]]) -> dict[str, Any]:
    """Public, non-sensitive view: ids and labels only (no paths)."""
    return {
        "actions": [
            {
                "action": action,
                "targets": [{"id": tid, "label": entry.get("label", tid)} for tid, entry in targets.items()],
            }
            for action, targets in allowlist.items()
        ]
    }


# --------------------------------------------------------------------------- #
# Resolvers (all fixed, nothing from the request)
# --------------------------------------------------------------------------- #

def _vscode_exe() -> Path | None:
    """Prefer Code.exe (no .cmd interpreter); fall back to `code.cmd`."""
    code_cmd = shutil.which("code") or shutil.which("code.cmd")
    if code_cmd:
        exe = Path(code_cmd).resolve().parent.parent / "Code.exe"
        if exe.is_file():
            return exe
        return Path(code_cmd)
    default = Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe"
    return default if default.is_file() else None


def _known_folder(name: str) -> Path | None:
    guids = {"downloads": "{374DE290-123F-4565-9164-39C4925E467B}"}
    guid = guids.get(name)
    if guid and sys.platform == "win32":
        try:
            with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders",
            ) as key:
                value, _ = winreg.QueryValueEx(key, guid)
                path = Path(os.path.expandvars(value))
                if path.is_dir():
                    return path
        except OSError:
            pass
    fallback = Path.home() / name.capitalize()
    return fallback if fallback.is_dir() else None


def _spawn(argv: list[str], cwd: str | None = None) -> None:
    subprocess.Popen(  # noqa: S603 - argv is built only from allowlisted constants
        argv,
        cwd=cwd,
        shell=False,
        close_fds=True,
        creationflags=_DETACHED,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# --------------------------------------------------------------------------- #
# Executors per allowlist "kind"
# --------------------------------------------------------------------------- #

def _run_vscode(entry: dict[str, Any], query: str | None = None) -> str:
    exe = _vscode_exe()
    if not exe:
        raise ActionError("not_installed", "Visual Studio Code non è installato")
    _spawn([str(exe)])
    return "VS Code avviato"


def _run_vscode_project(entry: dict[str, Any], query: str | None = None) -> str:
    exe = _vscode_exe()
    if not exe:
        raise ActionError("not_installed", "Visual Studio Code non è installato")
    path = Path(str(entry.get("path", "")))
    if not path.is_absolute() or not path.is_dir():
        raise ActionError("bad_allowlist", f"cartella progetto non trovata: {path.name}")
    _spawn([str(exe), str(path)])
    return f"VS Code aperto su {path.name}"


def _run_store_app(entry: dict[str, Any], query: str | None = None, *, minimized: bool = False) -> str:
    # 1) App-execution alias (…\WindowsApps\Spotify.exe) → a real executable, no shell.
    alias = entry.get("alias")
    if alias:
        alias_path = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WindowsApps" / str(alias)
        if alias_path.is_file():
            # Spotify honours --minimized: it starts in the tray/taskbar without taking the screen.
            _spawn([str(alias_path)] + (["--minimized"] if minimized else []))
            return "avviata"
    # 2) Fallback: explorer.exe with the fixed AUMID from the allowlist.
    aumid = entry.get("aumid")
    if aumid:
        _spawn(["explorer.exe", f"shell:AppsFolder\\{aumid}"])
        return "avviata"
    raise ActionError("not_installed", "applicazione non installata")


def _run_default_browser(entry: dict[str, Any], query: str | None = None) -> str:
    url = str(entry.get("url", "about:blank"))
    if not url.startswith(("http://", "https://", "about:")):
        raise ActionError("bad_allowlist", "url non valido nella allowlist")
    if not webbrowser.open_new(url):
        raise ActionError("failed", "impossibile aprire il browser predefinito")
    return "browser predefinito aperto"


def _run_known_folder(entry: dict[str, Any], query: str | None = None) -> str:
    folder = _known_folder(str(entry.get("known", "")))
    if not folder:
        raise ActionError("not_found", "cartella non trovata")
    os.startfile(str(folder))  # noqa: S606 - directory path from a fixed known-folder lookup
    return f"cartella {folder.name} aperta"


def _spotify_pids() -> set[int]:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["tasklist.exe", "/FI", "IMAGENAME eq Spotify.exe", "/NH", "/FO", "CSV"],
            capture_output=True, text=True, timeout=5, creationflags=_DETACHED,
        ).stdout
    except Exception:
        return set()
    pids: set[int] = set()
    for line in out.splitlines():
        parts = [c.strip('"') for c in line.split('","')]
        if len(parts) > 1 and parts[0].lower() == "spotify.exe":
            try:
                pids.add(int(parts[1]))
            except ValueError:
                pass
    return pids


def minimize_spotify_windows() -> int:
    """Send every visible top-level Spotify window to the taskbar (SW_MINIMIZE).
    Used after a background launch so the player never steals the screen. Returns how many."""
    pids = _spotify_pids()
    if not pids:
        return 0
    user32 = ctypes.windll.user32
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    done = 0

    def cb(hwnd, _):
        nonlocal done
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if pid.value in pids and user32.GetWindowTextLengthW(hwnd) > 0:
            user32.ShowWindow(hwnd, 6)  # SW_MINIMIZE
            done += 1
        return True

    user32.EnumWindows(EnumWindowsProc(cb), 0)
    return done


def _spotify_running() -> bool:
    try:
        out = subprocess.run(  # noqa: S603 - fixed argv
            ["tasklist.exe", "/FI", "IMAGENAME eq Spotify.exe", "/NH"],
            capture_output=True, text=True, timeout=5, creationflags=_DETACHED,
        ).stdout
        return "Spotify.exe" in out
    except Exception:
        return False


# Italian connectors that help the sentence but hurt Spotify's search ("One degli U2" -> "One U2").
_QUERY_NOISE = re.compile(r"\b(?:degli|dei|delle|della|dello|del|di|da|the\s+song|by)\b", re.IGNORECASE)

# User-level Spotify session (OAuth). Injected by main.py; None means "search-only" fallbacks.
spotify_session: Any = None


def set_spotify_session(session: Any) -> None:
    global spotify_session
    spotify_session = session


def normalize_music_query(query: str) -> str:
    cleaned = _QUERY_NOISE.sub(" ", query)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" ,.-")
    return cleaned or query


def _spotify_track_uri(query: str) -> str | None:
    """Resolve the best matching track via Spotify Web API *only if* client
    credentials are configured (SPOTIFY_CLIENT_ID / SPOTIFY_CLIENT_SECRET).
    Without them we fall back to the in-app search page."""
    cid, secret = os.environ.get("SPOTIFY_CLIENT_ID"), os.environ.get("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        return None
    try:
        import base64

        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        req = urllib.request.Request(
            "https://accounts.spotify.com/api/token",
            data=b"grant_type=client_credentials",
            headers={"Authorization": f"Basic {auth}", "Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=8) as r:
            token = json.load(r)["access_token"]
        q = urllib.parse.urlencode({"q": normalize_music_query(query), "type": "track", "limit": 1})
        req = urllib.request.Request(f"https://api.spotify.com/v1/search?{q}", headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(req, timeout=8) as r:
            items = json.load(r)["tracks"]["items"]
        if not items:
            return None
        uri = str(items[0]["uri"])
        return uri if re.fullmatch(r"spotify:track:[A-Za-z0-9]{10,40}", uri) else None
    except Exception:
        return None


def _run_spotify_play(entry: dict[str, Any], query: str | None = None) -> str:
    if not query or not MUSIC_QUERY_RE.match(query):
        raise ActionError("bad_query", "cosa devo riprodurre?")
    launched = False
    if not _spotify_running():
        _run_store_app(entry)
        time.sleep(3.5)  # let the client come up before handing it a URI
        launched = True

    # 1) Logged-in user: resolve + play on the active device through the Web API.
    sess = spotify_session
    if sess is not None and getattr(sess, "logged_in", False):
        try:
            track = sess.search_track(normalize_music_query(query))
            if track:
                try:
                    sess.play_uri(track["uri"])
                    return f"riproduco {track['name']} di {track['artist']}"
                except Exception as exc:  # no device yet (client just started) → URI fallback below
                    code = getattr(exc, "code", "")
                    if code == "premium_required":
                        raise ActionError("premium_required", str(exc)) from exc
                    if code == "no_device" and not launched:
                        # Client is open but not registered as a device: nudge it with the URI.
                        pass
                os.startfile(track["uri"])  # noqa: S606 - spotify:track:<id> from the API
                return f"riproduco {track['name']} di {track['artist']}"
        except ActionError:
            raise
        except Exception as exc:
            log.warning("spotify user API failed, falling back: %s", exc)

    # 2) App-level credentials: exact track URI, auto-plays when opened.
    uri = _spotify_track_uri(query)
    if uri:
        os.startfile(uri)  # noqa: S606 - spotify:track:<id> URI validated above
        return f"riproduco {query}"
    # 3) Nothing configured: in-app search.
    os.startfile("spotify:search:" + urllib.parse.quote(query))  # noqa: S606 - fixed scheme + sanitised query
    return f"ricerca aperta su Spotify: {query}"


def _press_media_key(key: str) -> None:
    vk = _MEDIA_KEYS.get(key)
    if vk is None or sys.platform != "win32":
        raise ActionError("bad_allowlist", "tasto multimediale non valido")
    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.keybd_event(vk, 0, 0, 0)
    user32.keybd_event(vk, 0, 2, 0)  # KEYEVENTF_KEYUP


def _run_media_key(entry: dict[str, Any], query: str | None = None) -> str:
    key = str(entry.get("key"))
    label = str(entry.get("label", "ok"))
    sess = spotify_session
    # Prefer the Spotify API when logged in (works even if Spotify is not the focused app);
    # system media keys remain the fallback and the only path for "mute".
    if sess is not None and getattr(sess, "logged_in", False) and key != "mute":
        try:
            if key == "play_pause":
                return sess.play_pause()
            if key == "next":
                sess.next()
                return label
            if key == "previous":
                sess.previous()
                return label
            if key == "volume_up":
                return f"volume {sess.volume_step(+10)}%"
            if key == "volume_down":
                return f"volume {sess.volume_step(-10)}%"
        except Exception as exc:
            log.warning("spotify media via API failed (%s), using media key", exc)
    _press_media_key(key)
    return label


def _run_spotify_info(entry: dict[str, Any], query: str | None = None) -> str:
    sess = spotify_session
    if sess is None or not getattr(sess, "logged_in", False):
        raise ActionError("not_logged_in", "Spotify non è collegato al tuo account")
    try:
        info = sess.now_playing()
    except Exception as exc:
        raise ActionError("api_error", str(exc)) from exc
    if not info:
        return "niente in riproduzione"
    state = "" if info["is_playing"] else " (in pausa)"
    return f"{info['name']} di {info['artist']}{state}"


def _run_web_search(entry: dict[str, Any], query: str | None = None) -> str:
    if not query or not MUSIC_QUERY_RE.match(query):
        raise ActionError("bad_query", "cosa devo cercare?")
    base = str(entry.get("url", ""))
    if not base.startswith("https://"):
        raise ActionError("bad_allowlist", "url di ricerca non valido")
    if not webbrowser.open_new(base + urllib.parse.quote_plus(query)):
        raise ActionError("failed", "impossibile aprire il browser predefinito")
    return f"ricerca web: {query}"


# Extras (timers, weather, files, close) live in extras.py; the Timers instance is injected by main.
timers: Any = None


def set_timers(instance: Any) -> None:
    global timers
    timers = instance


def _wrap_extras(fn):
    def runner(entry: dict[str, Any], query: str | None = None) -> str:
        from .extras import ExtrasError

        try:
            return fn(entry, query)
        except ExtrasError as exc:
            raise ActionError(exc.code, str(exc)) from exc

    return runner


def _run_timer_set(entry: dict[str, Any], query: str | None = None) -> str:
    if timers is None:
        raise ActionError("unavailable", "timer non disponibili")
    if not query:
        from .extras import ExtrasError

        raise ExtrasError("bad_query", "quanto deve durare il timer?")
    return timers.set(query)


def _run_timer_cancel(entry: dict[str, Any], query: str | None = None) -> str:
    return timers.cancel() if timers is not None else "nessun timer"


def _run_timer_list(entry: dict[str, Any], query: str | None = None) -> str:
    return timers.list() if timers is not None else "nessun timer"


def _run_weather(entry: dict[str, Any], query: str | None = None) -> str:
    from .extras import weather

    return weather(query, str(entry.get("default_city", "Roma")))


def _run_find_file(entry: dict[str, Any], query: str | None = None) -> str:
    from .extras import find_file

    return find_file(query, [str(r) for r in entry.get("roots", [])])


def _run_find_folder(entry: dict[str, Any], query: str | None = None) -> str:
    from .extras import find_folder

    return find_folder(query, [str(r) for r in entry.get("roots", [])], int(entry.get("max_depth", 4)))


def _run_close_app(entry: dict[str, Any], query: str | None = None) -> str:
    from .extras import close_app

    return close_app([str(i) for i in entry.get("images", [])])


_KINDS = {
    "timer_set": _wrap_extras(_run_timer_set),
    "timer_cancel": _wrap_extras(_run_timer_cancel),
    "timer_list": _wrap_extras(_run_timer_list),
    "weather": _wrap_extras(_run_weather),
    "find_file": _wrap_extras(_run_find_file),
    "find_folder": _wrap_extras(_run_find_folder),
    "close_app": _wrap_extras(_run_close_app),
    "web_search": _run_web_search,
    "spotify_play": _run_spotify_play,
    "spotify_info": _run_spotify_info,
    "media_key": _run_media_key,
    "vscode": _run_vscode,
    "vscode_project": _run_vscode_project,
    "store_app": _run_store_app,
    "default_browser": _run_default_browser,
    "known_folder": _run_known_folder,
}


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #

def execute(
    allowlist: dict[str, dict[str, dict[str, Any]]], action: str, target: str, query: str | None = None
) -> ActionResult:
    if action not in allowlist:
        raise ActionError("unknown_action", f"azione non consentita: {action!r}")
    targets = allowlist[action]
    if target not in targets:
        raise ActionError("unknown_target", f"target non consentito per {action}: {target!r}")
    entry = targets[target]
    runner = _KINDS.get(str(entry.get("kind")))
    if runner is None:
        raise ActionError("bad_allowlist", f"kind sconosciuto per {action}/{target}")
    if query is not None and action not in QUERY_ACTIONS:
        raise ActionError("unexpected_query", "questa azione non accetta parametri")
    detail = runner(entry, query)
    return ActionResult(action=action, target=target, label=str(entry.get("label", target)), detail=detail)
