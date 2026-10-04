"""Non-destructive extras: timers/alarms, weather (Open-Meteo, no key), read-only
file search in allowlisted folders, graceful app close.

Everything here takes at most one sanitised free-text `query` and never runs a
shell. Timers fire events on the bus so the UI can announce them with TTS.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from .bus import EventBus

log = logging.getLogger("jarvis.extras")

_DETACHED = 0
if sys.platform == "win32":
    _DETACHED = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "DETACHED_PROCESS", 0)


class ExtrasError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# ----------------------------------------------------------------- timers --

@dataclass
class Timer:
    id: int
    label: str
    fire_at: float
    kind: str  # "timer" | "alarm"
    thread: threading.Timer = field(repr=False)


class Timers:
    _DURATION = re.compile(
        r"(?:(?P<h>\d{1,2})\s*(?:ore?|h|hour?s?))?\s*(?:e\s+)?(?:(?P<m>\d{1,3})\s*(?:min(?:uti|uto|s)?|m\b))?\s*(?:(?P<s>\d{1,3})\s*(?:sec(?:ondi|ondo)?|s\b))?",
        re.IGNORECASE,
    )
    _CLOCK = re.compile(r"(?:alle|at|per\s+le)?\s*(?P<h>\d{1,2})(?:[:.,](?P<m>\d{2}))?\s*(?:e\s+(?P<mm>\d{1,2}))?", re.IGNORECASE)
    _WORDS = {"un": 1, "uno": 1, "una": 1, "due": 2, "tre": 3, "quattro": 4, "cinque": 5, "sei": 6, "sette": 7, "otto": 8, "nove": 9,
              "dieci": 10, "quindici": 15, "venti": 20, "trenta": 30, "quaranta": 40, "quarantacinque": 45, "sessanta": 60,
              "mezz'ora": 30, "mezzora": 30, "un'ora": 60, "un quarto d'ora": 15}

    def __init__(self, bus: EventBus):
        self.bus = bus
        self._timers: dict[int, Timer] = {}
        self._next = 1
        self._lock = threading.Lock()

    def _words_to_digits(self, text: str) -> str:
        for w, n in sorted(self._WORDS.items(), key=lambda kv: -len(kv[0])):
            text = re.sub(rf"\b{re.escape(w)}\b", str(n), text, flags=re.IGNORECASE)
        return text

    def parse(self, query: str) -> tuple[float, str, str]:
        """Returns (seconds_from_now, label, kind)."""
        q = self._words_to_digits(query.lower().strip())
        if re.search(r"\b(?:alle|at|per\s+le|sveglia|svegliami)\b", q) or re.fullmatch(r"\d{1,2}[:.]\d{2}", q):
            m = self._CLOCK.search(q)
            if not m:
                raise ExtrasError("bad_query", "a che ora?")
            h = int(m.group("h"))
            mi = int(m.group("m") or m.group("mm") or 0)
            if not (0 <= h <= 23 and 0 <= mi <= 59):
                raise ExtrasError("bad_query", "orario non valido")
            now = datetime.now()
            target = now.replace(hour=h, minute=mi, second=0, microsecond=0)
            if target <= now:
                target += timedelta(days=1)
            return (target - now).total_seconds(), f"sveglia alle {h:02d}:{mi:02d}", "alarm"
        if "30" in q and ("mezz" in query.lower()):
            return 30 * 60, "timer di 30 minuti", "timer"
        m = self._DURATION.search(q)
        h = int(m.group("h") or 0) if m else 0
        mi = int(m.group("m") or 0) if m else 0
        s = int(m.group("s") or 0) if m else 0
        if m and not (h or mi or s):
            # bare number → minutes ("timer 10")
            n = re.search(r"\d+", q)
            if n:
                mi = int(n.group(0))
        total = h * 3600 + mi * 60 + s
        if total <= 0 or total > 24 * 3600:
            raise ExtrasError("bad_query", "quanto deve durare il timer?")
        parts = []
        if h:
            parts.append(f"{h} or{'a' if h == 1 else 'e'}")
        if mi:
            parts.append(f"{mi} minut{'o' if mi == 1 else 'i'}")
        if s:
            parts.append(f"{s} second{'o' if s == 1 else 'i'}")
        return total, "timer di " + " e ".join(parts), "timer"

    _NOTE_STRIP = re.compile(
        r"\b(?:tra|fra|di|per|alle|at|un|una|e|minuti?|min|ore?|h|secondi?|sec|s|sveglia|svegliami|timer|ricordami|ricorda|avvisami|chiamami|dimmi)\b|\d+[:.]?\d*",
        re.IGNORECASE,
    )

    def set(self, query: str) -> str:
        seconds, label, kind = self.parse(query)
        # Anything left after removing the time expression is the reminder text ("chiamare Marco").
        note = re.sub(r"\s+", " ", self._NOTE_STRIP.sub(" ", self._words_to_digits(query))).strip(" ,.-:")
        if note and len(note) > 2:
            label = f"{label}: {note}"
        with self._lock:
            tid = self._next
            self._next += 1
            t = threading.Timer(seconds, self._fire, args=(tid,))
            t.daemon = True
            self._timers[tid] = Timer(id=tid, label=label, fire_at=time.time() + seconds, kind=kind, thread=t)
            t.start()
        log.info("timer %d set: %s (%.0fs)", tid, label, seconds)
        return label

    def _fire(self, tid: int) -> None:
        with self._lock:
            timer = self._timers.pop(tid, None)
        if not timer:
            return
        log.info("timer %d fired: %s", tid, timer.label)
        self.bus.publish({"type": "timer", "id": tid, "label": timer.label, "kind": timer.kind})
        if sys.platform == "win32":
            try:
                import winsound

                for _ in range(3):
                    winsound.Beep(880, 250)
                    time.sleep(0.15)
            except Exception:
                pass

    def cancel(self) -> str:
        with self._lock:
            items = list(self._timers.values())
            self._timers.clear()
        for t in items:
            t.thread.cancel()
        return f"{len(items)} annullat{'o' if len(items) == 1 else 'i'}" if items else "nessun timer attivo"

    def list(self) -> str:
        with self._lock:
            items = sorted(self._timers.values(), key=lambda t: t.fire_at)
        if not items:
            return "nessun timer attivo"
        out = []
        for t in items:
            left = max(0, int(t.fire_at - time.time()))
            out.append(f"{t.label} ({left // 60} min {left % 60} s)")
        return "; ".join(out)


# ---------------------------------------------------------------- weather --

_WMO = {
    0: "sereno", 1: "prevalentemente sereno", 2: "parzialmente nuvoloso", 3: "coperto", 45: "nebbia", 48: "nebbia con brina",
    51: "pioviggine leggera", 53: "pioviggine", 55: "pioviggine intensa", 61: "pioggia leggera", 63: "pioggia", 65: "pioggia forte",
    66: "pioggia gelata", 67: "pioggia gelata forte", 71: "neve leggera", 73: "neve", 75: "neve forte", 77: "granelli di neve",
    80: "rovesci leggeri", 81: "rovesci", 82: "rovesci violenti", 85: "rovesci di neve", 86: "rovesci di neve forti",
    95: "temporale", 96: "temporale con grandine", 99: "temporale con grandine forte",
}


def weather(query: str | None, default_city: str) -> str:
    text = (query or "").strip()
    tomorrow = bool(re.search(r"\bdomani\b|\btomorrow\b", text, re.IGNORECASE))
    city = re.sub(r"\b(?:domani|oggi|tomorrow|today|a|in|per|di|del|della|il|la|che|tempo|fa|meteo|previsioni|pioverà|piove|weather)\b", " ", text, flags=re.IGNORECASE)
    city = re.sub(r"\s+", " ", city).strip(" ,.?") or default_city
    try:
        geo = json.load(urllib.request.urlopen(
            "https://geocoding-api.open-meteo.com/v1/search?" + urllib.parse.urlencode({"name": city, "count": 1, "language": "it"}), timeout=8))
        res = (geo.get("results") or [None])[0]
        if not res:
            raise ExtrasError("not_found", f"non trovo la località «{city}»")
        params = {
            "latitude": res["latitude"], "longitude": res["longitude"], "timezone": "auto",
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max",
            "forecast_days": 2,
        }
        data = json.load(urllib.request.urlopen("https://api.open-meteo.com/v1/forecast?" + urllib.parse.urlencode(params), timeout=8))
    except ExtrasError:
        raise
    except Exception as exc:
        raise ExtrasError("api_error", f"servizio meteo non raggiungibile: {exc}") from exc
    name = res.get("name", city)
    daily = data["daily"]
    idx = 1 if tomorrow else 0
    code = int(daily["weather_code"][idx])
    desc = _WMO.get(code, "variabile")
    tmax, tmin = round(daily["temperature_2m_max"][idx]), round(daily["temperature_2m_min"][idx])
    rain = daily.get("precipitation_probability_max", [None, None])[idx]
    rain_txt = f", pioggia al {int(rain)}%" if rain is not None else ""
    if tomorrow:
        return f"domani a {name}: {desc}, tra {tmin} e {tmax} gradi{rain_txt}"
    cur = data["current"]
    return f"a {name} ora {round(cur['temperature_2m'])} gradi, {_WMO.get(int(cur['weather_code']), desc)}; oggi tra {tmin} e {tmax}{rain_txt}"


# --------------------------------------------------------------- find file --

_DOC_EXT = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".csv", ".txt", ".md", ".pptx", ".jpg", ".jpeg", ".png", ".zip", ".odt", ".ods"}


def find_file(query: str | None, roots: list[str], max_results: int = 5) -> str:
    needle = (query or "").strip().lower()
    if len(needle) < 2:
        raise ExtrasError("bad_query", "quale file devo cercare?")
    words = [w for w in re.split(r"\s+", needle) if w]
    hits: list[tuple[float, Path]] = []
    deadline = time.time() + 6
    for root in roots:
        base = Path(os.path.expandvars(root)).expanduser()
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "$", "node_modules", "__pycache__"))]
            for fn in filenames:
                low = fn.lower()
                if all(w in low for w in words):
                    p = Path(dirpath) / fn
                    try:
                        hits.append((p.stat().st_mtime, p))
                    except OSError:
                        pass
            if time.time() > deadline:
                break
    if not hits:
        return f"nessun file che contenga «{query}»"
    hits.sort(key=lambda h: -h[0])
    top = hits[:max_results]
    # Reveal the newest match in Explorer (read-only: select, do not open the file).
    best = top[0][1]
    try:
        subprocess.Popen(["explorer.exe", f"/select,{best}"], shell=False, creationflags=_DETACHED)  # noqa: S603
    except Exception:
        pass
    names = "; ".join(f"{p.name} ({p.parent.name})" for _, p in top)
    more = f" e altri {len(hits) - len(top)}" if len(hits) > len(top) else ""
    return f"trovat{'o' if len(hits) == 1 else 'i'} {len(hits)}: {names}{more}"


_SKIP_DIRS = {"node_modules", "__pycache__", ".git", ".venv", "venv", "AppData", "$Recycle.Bin", "Library", "site-packages"}


def find_folder(query: str | None, roots: list[str], max_depth: int = 4, max_results: int = 5) -> str:
    """Find a folder by name inside the allowlisted roots and open the best match in Explorer."""
    needle = (query or "").strip().lower()
    if len(needle) < 2:
        raise ExtrasError("bad_query", "quale cartella devo cercare?")
    words = [w for w in re.split(r"\s+", needle) if w]
    hits: list[tuple[int, float, Path]] = []  # (score, mtime, path): exact name beats partial, shallow beats deep
    seen: set[str] = set()
    deadline = time.time() + 6
    for root in roots:
        base = Path(os.path.expandvars(root)).expanduser()
        if not base.is_dir():
            continue
        base_depth = len(base.parts)
        for dirpath, dirnames, _files in os.walk(base):
            depth = len(Path(dirpath).parts) - base_depth
            dirnames[:] = [d for d in dirnames if not d.startswith((".", "$")) and d not in _SKIP_DIRS]
            if depth >= max_depth:
                dirnames[:] = []
            for d in dirnames:
                low = d.lower()
                if all(w in low for w in words):
                    p = Path(dirpath) / d
                    key = str(p).lower()
                    if key in seen:
                        continue
                    seen.add(key)
                    score = (2 if low == needle else 1) * 10 - depth
                    try:
                        hits.append((score, p.stat().st_mtime, p))
                    except OSError:
                        pass
            if time.time() > deadline:
                break
    if not hits:
        return f"nessuna cartella che si chiami «{query}»"
    hits.sort(key=lambda h: (-h[0], -h[1]))
    best = hits[0][2]
    os.startfile(str(best))  # noqa: S606 - directory inside an allowlisted root
    # "Ale" must not drag in alessio-ai, alessio-vinci-portfolio…: when the best hit is an
    # exact name match, only list other *exact* matches (same name elsewhere).
    if best.name.lower() == needle:
        others = [p.name for _, _, p in hits[1:max_results] if p.name.lower() == needle]
    else:
        others = [p.name for _, _, p in hits[1:max_results]]
    more = f"; altre: {', '.join(others)}" if others else ""
    return f"aperta {best.name} in {best.parent.name}{more}"


# -------------------------------------------------------------- close app --

def close_app(images: list[str]) -> str:
    """Graceful close (WM_CLOSE via taskkill without /F): the app can still ask to save."""
    if sys.platform != "win32":
        raise ExtrasError("unsupported", "solo Windows")
    closed = 0
    for image in images:
        if not re.fullmatch(r"[A-Za-z0-9_.-]+\.exe", image):
            continue
        try:
            r = subprocess.run(["taskkill.exe", "/IM", image], capture_output=True, text=True, timeout=10, creationflags=_DETACHED)  # noqa: S603
            if r.returncode == 0:
                closed += 1
        except Exception as exc:
            log.warning("close %s failed: %s", image, exc)
    return "chiusa" if closed else "non era aperta"
