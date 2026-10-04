"""Jarvis Local Action Bridge.

Loopback-only HTTP service used by the Jarvis UI for two things:

1. ``POST /actions`` — run one allowlisted desktop action (open app / folder /
   project).  Only ``{action, target}`` ids are accepted; see ``actions.py``.
2. ``/wake/*`` — offline "hey jarvis" wake word, streamed to the UI over SSE.

The server refuses to bind to anything but 127.0.0.1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import threading
from pathlib import Path
from contextlib import asynccontextmanager
from typing import Literal

import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import actions as act
from . import audit
from .brain import Brain, BrainError
from .bus import EventBus
from .google import GoogleError, GoogleSession
from . import chat as chat_mod
from .routines import load_routines
from .telegram import Telegram
from .extras import Timers
from .intent import IntentClassifier
from .spotify import SpotifyError, SpotifySession
from .stt import SpeechToText
from .wake import WakeEngine

def _load_dotenv() -> None:
    """Read KEY=VALUE lines from jarvis-bridge/.env into the environment (never overrides)."""
    path = Path(__file__).resolve().parent.parent / ".env"
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()

HOST = "127.0.0.1"
PORT = int(os.environ.get("JARVIS_BRIDGE_PORT", "8765"))
ALLOWED_ORIGINS = [
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("jarvis.bridge")

ALLOWLIST = act.load_allowlist()
WAKE = WakeEngine(threshold=float(os.environ.get("JARVIS_WAKE_THRESHOLD", "0.5")))
# Redirect URI must match the one registered in the Spotify developer dashboard.
SPOTIFY = SpotifySession(redirect_uri=f"http://127.0.0.1:{PORT}/callback")
act.set_spotify_session(SPOTIFY)
BUS = EventBus()
TIMERS = Timers(BUS)
act.set_timers(TIMERS)
INTENT = IntentClassifier(ALLOWLIST)
STT = SpeechToText(ALLOWLIST)
# Google Calendar + Gmail, read-only, for the briefing. Inert until GOOGLE_CLIENT_ID/SECRET are in .env.
GOOGLE = GoogleSession(redirect_uri=f"http://127.0.0.1:{PORT}/google/callback")
BRAIN = Brain(BUS)
BRAIN.weather_city = str(ALLOWLIST.get("weather", {}).get("forecast", {}).get("default_city") or "") or None
TELEGRAM = Telegram(lambda text: BRAIN.handle(text))
ROUTINES = load_routines(ALLOWLIST)


def _on_reminder(kind: str, item: dict) -> None:
    audit.record("brain", action="reminder", target="fired", accepted=True, executed=True, result=f"#{item['id']}")
    # On the phone too, when the bot is configured.
    TELEGRAM.send(("⚠️ " if item.get("priority") == 2 else "⏰ ") + item["text"])


BRAIN.on_event(_on_reminder)


@asynccontextmanager
async def lifespan(_: FastAPI):
    WAKE.bind_loop(asyncio.get_running_loop())
    BUS.bind_loop(asyncio.get_running_loop())
    # Probe in a thread: downloads the model on first run and loads ONNX sessions.
    available = await asyncio.to_thread(WAKE.probe)
    log.info("local wake word: %s", "available" if available else f"unavailable ({WAKE.status().reason})")
    log.info("audit log: %s", audit.LOG_PATH)
    audit.record("bridge", result="start", port=PORT, wake_available=available)
    BRAIN.start()
    TELEGRAM.start()
    log.info("brain db: %s", BRAIN.store.path)
    log.info("chat: %s", chat_mod.status())
    # Warm the STT model in the background so the first voice command is not slow.
    asyncio.get_running_loop().run_in_executor(None, STT.load)
    yield
    TELEGRAM.stop()
    BRAIN.stop()
    WAKE.stop()
    audit.record("bridge", result="stop")


app = FastAPI(title="Jarvis Local Action Bridge", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_methods=["GET", "POST"],
    allow_headers=["content-type"],
)


@app.middleware("http")
async def loopback_only(request: Request, call_next):
    client = request.client.host if request.client else ""
    if client not in ("127.0.0.1", "::1", "testclient"):
        audit.record("request", source=audit.source_of(request), method=request.method, path=request.url.path, accepted=False, executed=False, error="loopback only")
        raise HTTPException(status_code=403, detail="loopback only")
    response = await call_next(request)
    path = request.url.path
    detailed = path in ("/actions", "/spotify/intro", "/brain") and request.method == "POST"
    if not detailed and (request.method == "POST" or path in ("/spotify/login", "/callback")):
        audit.record(
            "request", source=audit.source_of(request), method=request.method, path=path,
            accepted=response.status_code < 400, executed=response.status_code < 400, status=response.status_code,
        )
    return response


@app.exception_handler(RequestValidationError)
async def _validation_audit(request: Request, exc: RequestValidationError):
    """Malformed action bodies are refused by pydantic before the handler runs: log them too."""
    if request.url.path in ("/actions", "/spotify/intro"):
        body = exc.body if isinstance(exc.body, dict) else {}
        audit.record(
            "action", source=audit.source_of(request),
            action=str(body.get("action"))[:32] if body.get("action") is not None else None,
            target=str(body.get("target"))[:64] if body.get("target") is not None else None,
            accepted=False, executed=False, error="validation: " + "; ".join(str(e.get("msg")) for e in exc.errors()[:3]),
        )
    return JSONResponse(status_code=422, content={"detail": exc.errors()})


# ----------------------------------------------------------------- actions --


class ActionRequest(BaseModel):
    action: Literal[
        "open_app", "open_folder", "open_project", "play_music", "media", "web_search",
        "close_app", "timer", "weather", "find_file",
    ]
    target: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9_-]*$")
    # Only play_music uses it: a search string, never a command or a path.
    query: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[\w '’\-.,&!?:/]+$")


@app.get("/health")
async def health():
    last = audit.last_executed()
    return {
        "status": "ok",
        "wake": WAKE.status().__dict__,
        "last_action": {"ts": last["ts"], "label": audit.summary(last), "result": last.get("result")} if last else None,
    }


@app.get("/audit/last")
async def audit_last():
    """Last executed action and last audit event of any kind (read-only)."""
    return {"log": str(audit.LOG_PATH), "last_executed": audit.last_executed(), "last_event": audit.last_event()}


@app.get("/actions")
async def list_actions():
    return act.catalog(ALLOWLIST)


@app.post("/actions")
async def run_action(req: ActionRequest, request: Request):
    source = audit.source_of(request)
    pre = {"source": source, "action": req.action, "target": req.target, "query": req.query}
    try:
        result = await asyncio.to_thread(act.execute, ALLOWLIST, req.action, req.target, req.query)
    except act.ActionError as exc:
        status = (
            404
            if exc.code in ("unknown_action", "unknown_target")
            else 422
            if exc.code in ("bad_query", "unexpected_query")
            else 401
            if exc.code == "not_logged_in"
            else 409
        )
        # Refused before touching the OS vs. accepted but failed while running.
        refused = exc.code in ("unknown_action", "unknown_target", "bad_allowlist", "unexpected_query", "bad_query")
        audit.record("action", **pre, accepted=not refused, executed=False, error=f"{exc.code}: {exc}")
        log.warning("action refused %s/%s: %s", req.action, req.target, exc)
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from exc
    except Exception as exc:
        audit.record("action", **pre, accepted=True, executed=False, error=f"{type(exc).__name__}: {exc}")
        raise
    audit.record("action", **pre, accepted=True, executed=True, result=result.detail)
    log.info("action ok %s/%s: %s", result.action, result.target, result.detail)
    return {"ok": True, "action": result.action, "target": result.target, "label": result.label, "detail": result.detail}


# ------------------------------------------------------------------- brain --


class BrainRequest(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    # When false the deterministic rules alone decide (fast, no Ollama call).
    allow_model: bool = True


@app.post("/brain")
async def brain_handle(req: BrainRequest, request: Request):
    """Free text → note / fact / reminder / list / recall. `handled=false` means: not for the brain, go on with chat."""
    source = audit.source_of(request)
    try:
        out = await asyncio.to_thread(BRAIN.handle, req.text, allow_model=req.allow_model)
    except BrainError as exc:
        audit.record("brain", source=source, accepted=False, executed=False, error=f"{exc.code}: {exc}")
        raise HTTPException(status_code=409, detail={"code": exc.code, "message": str(exc)}) from exc
    if out.handled and out.op not in ("show", "search", "summary", "names"):
        # Writes only: what kind of thing changed, never the content.
        audit.record("brain", source=source, action=out.kind, target=out.op, accepted=True, executed=out.executed,
                     result=f"#{out.item['id']}" if out.item else (f"{len(out.items)} items" if out.items else None),
                     parser=out.source, needs_confirm=out.needs_confirm or None)
    return {
        "handled": out.handled, "kind": out.kind, "op": out.op, "reply": out.reply, "needs_confirm": out.needs_confirm,
        "source": out.source, "item": out.item, "items": out.items[:10],
    }


@app.get("/brain/today")
async def brain_today():
    return await asyncio.to_thread(BRAIN.today)


@app.get("/brain/summary")
async def brain_summary(weather: bool = False):
    """Spoken briefing for the UI (startup, "buongiorno"). Read-only; `weather=1` adds today's forecast."""
    out = await asyncio.to_thread(BRAIN.summary, None, weather=weather)
    counts = BRAIN.store.count()
    reply = out.reply
    # Calendar and mail join the briefing once Google is connected (read-only; see google.py).
    google_lines = await asyncio.to_thread(GOOGLE.briefing_lines) if GOOGLE.logged_in else []
    if google_lines:
        reply += " " + ". ".join(l[0].upper() + l[1:] for l in google_lines) + "."
    # Worth announcing on its own only when there is something on the agenda.
    has_content = bool(out.items) or counts["reminder"] > 0 or any("calendario hai" in l or "mail" in l for l in google_lines)
    return {"reply": reply, "items": out.items, "has_content": has_content, "google": GOOGLE.logged_in}


@app.get("/brain/recall")
async def brain_recall(q: str = ""):
    """Memory hits (facts + notes) to inject into the chat prompt. Read-only."""
    q = q.strip()[:300]
    if len(q) < 3:
        return {"results": []}
    hits = await asyncio.to_thread(BRAIN.recall, q)
    return {"results": [{"content": h["text"], "kind": h["kind"], "created": h["created"], "id": h["id"]} for h in hits]}


@app.get("/brain/stats")
async def brain_stats():
    return {"backend": "sqlite", "path": str(BRAIN.store.path), "counts": BRAIN.store.count()}


# --------------------------------------------------------- brain: browse/edit --


class ItemPatch(BaseModel):
    text: str | None = Field(default=None, min_length=1, max_length=500)
    # ISO local datetime ("2026-10-05T09:00"); send clear_due=true to drop the time.
    due: str | None = Field(default=None, max_length=25)
    clear_due: bool = False
    done: bool | None = None
    list_name: str | None = Field(default=None, max_length=30)


@app.get("/brain/items")
async def brain_items(kind: Literal["note", "fact", "reminder", "list"], q: str = "", list: str | None = None, include_done: bool = False, limit: int = 200):
    """Everything Jarvis knows, browsable from the UI. `q` searches full-text within the kind."""
    q = q.strip()
    if q:
        items = await asyncio.to_thread(BRAIN.store.search, q, kinds=(kind,), limit=min(limit, 200))
        if list:
            items = [i for i in items if (i.get("list_name") or "").lower() == list.lower()]
        if not include_done:
            items = [i for i in items if not i["done"]]
    else:
        items = await asyncio.to_thread(BRAIN.store.list, kind, list_name=list, include_done=include_done, limit=min(limit, 500))
    return {"items": items, "lists": [{"name": n, "count": c} for n, c in BRAIN.store.list_names()] if kind == "list" else []}


@app.patch("/brain/items/{item_id}")
async def brain_item_patch(item_id: int, patch: ItemPatch, request: Request):
    try:
        item = await asyncio.to_thread(
            BRAIN.store.update, item_id, text=patch.text, due=patch.due, clear_due=patch.clear_due, done=patch.done, list_name=patch.list_name,
        )
    except BrainError as exc:
        raise HTTPException(status_code=404 if exc.code == "not_found" else 422, detail={"code": exc.code, "message": str(exc)}) from exc
    audit.record("brain", source=audit.source_of(request), action=item["kind"], target="edit", accepted=True, executed=True, result=f"#{item_id}")
    return item


@app.delete("/brain/items/{item_id}")
async def brain_item_delete(item_id: int, request: Request):
    try:
        item = await asyncio.to_thread(BRAIN.store.get, item_id)
    except BrainError as exc:
        raise HTTPException(status_code=404, detail={"code": exc.code, "message": str(exc)}) from exc
    await asyncio.to_thread(BRAIN.store.mark, item_id, deleted=1)
    audit.record("brain", source=audit.source_of(request), action=item["kind"], target="delete", accepted=True, executed=True, result=f"#{item_id}")
    return {"ok": True, "id": item_id}


# ------------------------------------------------------------------ google --


@app.get("/google/status")
async def google_status():
    return GOOGLE.status()


@app.get("/google/login")
async def google_login():
    """Opens Google's consent page (browser navigates here, then back to /google/callback)."""
    try:
        return RedirectResponse(GOOGLE.login_url(), status_code=302)
    except GoogleError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc


@app.get("/google/callback")
async def google_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state:
        return HTMLResponse(f"<h2>Google: accesso negato</h2><p>{error or 'parametri mancanti'}</p>", status_code=400)
    try:
        await asyncio.to_thread(GOOGLE.complete_login, code, state)
    except GoogleError as exc:
        return HTMLResponse(f"<h2>Google: errore</h2><p>{exc}</p>", status_code=400)
    audit.record("request", source="browser", method="GET", path="/google/callback", accepted=True, executed=True, result="google linked")
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><body style='font-family:system-ui;background:#0b1020;color:#dfe7ff;"
        "display:grid;place-items:center;height:100vh;margin:0'><div style='text-align:center'>"
        "<h2>Google collegato a Jarvis (sola lettura)</h2><p>Calendario e mail entrano nel briefing. Puoi chiudere questa scheda.</p></div></body>"
    )


@app.post("/google/logout")
async def google_logout():
    GOOGLE.logout()
    return GOOGLE.status()


@app.get("/google/today")
async def google_today():
    """Today's events + unread mail digest (subjects/senders only). Read-only."""
    if not GOOGLE.logged_in:
        raise HTTPException(status_code=401, detail={"code": "not_logged_in", "message": "Google non collegato"})
    try:
        return await asyncio.to_thread(GOOGLE.today)
    except GoogleError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.code, "message": str(exc)}) from exc


# -------------------------------------------------------------------- chat --


class ChatMessage(BaseModel):
    role: Literal["system", "user", "assistant"]
    content: str = Field(max_length=8000)


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(min_length=1, max_length=40)
    temperature: float = Field(default=0.7, ge=0, le=1.5)
    max_tokens: int = Field(default=350, ge=16, le=2000)


@app.get("/chat/status")
async def chat_status():
    return chat_mod.status()


@app.post("/chat")
async def chat_stream(req: ChatRequest):
    """Conversation via Ollama, streamed as SSE: `delta` {text}, then `done` {model, seconds, tokens}; `error` on failure."""
    msgs = [{"role": m.role, "content": m.content} for m in req.messages]
    queue: asyncio.Queue = asyncio.Queue()
    loop = asyncio.get_running_loop()

    def producer() -> None:
        for ev in chat_mod.stream(msgs, temperature=req.temperature, max_tokens=req.max_tokens):
            loop.call_soon_threadsafe(queue.put_nowait, ev)
        loop.call_soon_threadsafe(queue.put_nowait, None)

    threading.Thread(target=producer, daemon=True).start()

    async def gen():
        while True:
            ev = await queue.get()
            if ev is None:
                break
            name = "error" if "error" in ev else "done" if ev.get("done") else "delta"
            yield f"event: {name}\ndata: {json.dumps(ev, ensure_ascii=False)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ---------------------------------------------------------------- routines --


@app.get("/routines")
async def routines():
    """Phrase → sequence of allowlisted steps. The UI matches the phrase and runs steps via POST /actions."""
    return {"routines": ROUTINES}


# ---------------------------------------------------------------- telegram --


@app.get("/telegram/status")
async def telegram_status():
    return TELEGRAM.status()


# -------------------------------------------------------------------- wake --


@app.get("/wake/status")
async def wake_status():
    return WAKE.status().__dict__


@app.post("/wake/start")
async def wake_start():
    ok = await asyncio.to_thread(WAKE.start)
    if not ok:
        raise HTTPException(status_code=503, detail=WAKE.status().reason or "wake word unavailable")
    return WAKE.status().__dict__


@app.post("/wake/stop")
async def wake_stop():
    await asyncio.to_thread(WAKE.stop)
    return WAKE.status().__dict__


@app.get("/wake/events")
async def wake_events(request: Request):
    """Server-Sent Events: `wake` on detection, `status` on engine changes, `ping` keep-alive."""
    queue = WAKE.subscribe()

    async def gen():
        try:
            yield f"event: status\ndata: {json.dumps(WAKE.status().__dict__)}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                yield f"event: {event.get('type', 'message')}\ndata: {json.dumps(event)}\n\n"
        finally:
            WAKE.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# --------------------------------------------------------------------- stt --


@app.get("/stt/health")
async def stt_health():
    return STT.status()


@app.post("/stt")
async def stt_transcribe(file: UploadFile = File(...), language: str = Form("it")):
    """Transcribe a short recording with the command vocabulary as prompt. Audio stays local."""
    data = await file.read()
    if not data:
        raise HTTPException(status_code=422, detail={"code": "empty", "message": "file vuoto"})
    if len(data) > 25 * 1024 * 1024:
        raise HTTPException(status_code=413, detail={"code": "too_large", "message": "file troppo grande"})
    suffix = os.path.splitext(file.filename or "speech.wav")[1].lower() or ".wav"
    if suffix not in (".wav", ".webm", ".ogg", ".mp4", ".m4a", ".mp3", ".flac"):
        suffix = ".wav"
    try:
        return await asyncio.to_thread(STT.transcribe, data, suffix, language[:5])
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "stt_unavailable", "message": str(exc)[:200]}) from exc


# ------------------------------------------------------------------ intent --


class IntentRequest(BaseModel):
    text: str = Field(min_length=1, max_length=300)
    # Short description of the previous desktop command (UI-provided), used only as a hint.
    context: str | None = Field(default=None, max_length=120)


@app.get("/intent/status")
async def intent_status():
    return INTENT.status()


@app.post("/intent")
async def classify_intent(req: IntentRequest):
    """Free text → validated allowlisted intent (or null). Nothing is executed here."""
    try:
        res = await asyncio.to_thread(INTENT.classify, req.text, req.context)
    except Exception as exc:
        raise HTTPException(status_code=503, detail={"code": "intent_unavailable", "message": str(exc)[:200]}) from exc
    return {"intent": res.intent, "model": res.model, "trusted": res.trusted, "seconds": round(res.seconds, 1)}


# ------------------------------------------------------------------ events --


@app.get("/events")
async def events(request: Request):
    """SSE for things that happen on their own: `timer` (fired), `ping` keep-alive."""
    queue = BUS.subscribe()

    async def gen():
        try:
            yield "event: hello\ndata: {}\n\n"
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"
                    continue
                yield f"event: {event.get('type', 'message')}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            BUS.unsubscribe(queue)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


# ----------------------------------------------------------------- spotify --


@app.get("/spotify/status")
async def spotify_status():
    return SPOTIFY.status()


@app.get("/spotify/login")
async def spotify_login():
    """Opens the Spotify consent page (browser navigates here, then back to /callback)."""
    try:
        return RedirectResponse(SPOTIFY.login_url(), status_code=302)
    except SpotifyError as exc:
        raise HTTPException(status_code=503, detail={"code": exc.code, "message": str(exc)}) from exc


@app.get("/callback")
async def spotify_callback(code: str | None = None, state: str | None = None, error: str | None = None):
    if error or not code or not state:
        return HTMLResponse(f"<h2>Spotify: accesso negato</h2><p>{error or 'parametri mancanti'}</p>", status_code=400)
    try:
        await asyncio.to_thread(SPOTIFY.complete_login, code, state)
    except SpotifyError as exc:
        return HTMLResponse(f"<h2>Spotify: errore</h2><p>{exc}</p>", status_code=400)
    return HTMLResponse(
        "<!doctype html><meta charset='utf-8'><body style='font-family:system-ui;background:#0b1020;color:#dfe7ff;"
        "display:grid;place-items:center;height:100vh;margin:0'><div style='text-align:center'>"
        "<h2>Spotify collegato a Jarvis</h2><p>Puoi chiudere questa scheda e tornare alla UI.</p></div></body>"
    )


class IntroRequest(BaseModel):
    query: str = Field(min_length=1, max_length=80, pattern=r"^[\w '’\-.,&!?:/]+$")
    seconds: int = Field(default=17, ge=3, le=120)
    volume: int = Field(default=20, ge=0, le=100)
    # Fade-out length at the end (0 = hard stop). Starts at `seconds - fade`.
    fade: float = Field(default=2.0, ge=0, le=10)


def _run_intro(query: str, seconds: int, volume: int, fade: float = 2.0) -> dict:
    """Theme intro: play `query` quietly for `seconds`, fading out over the last `fade`
    seconds, then pause and restore the volume. Needs the user session (Premium)."""
    import time

    from . import actions as act_mod

    if not SPOTIFY.logged_in:
        raise SpotifyError("not_logged_in", "Spotify non collegato")
    entry = ALLOWLIST["open_app"]["spotify"]
    launched = False
    if not act_mod._spotify_running():  # noqa: SLF001 - same package
        act_mod._run_store_app(entry, minimized=True)  # noqa: SLF001
        launched = True
    track = SPOTIFY.search_track(act_mod.normalize_music_query(query))
    if not track:
        raise SpotifyError("not_found", f"non trovo «{query}»")

    def find_device() -> dict | None:
        devs = SPOTIFY.devices()
        return next((d for d in devs if "computer" in str(d.get("type", "")).lower()), None) or (devs[0] if devs else None)

    def tidy() -> None:
        if launched:
            # Belt and braces: if the client ignored --minimized, push its window to the taskbar.
            act_mod.minimize_spotify_windows()

    # Wait for the desktop client to show up as a device (a cold start takes several seconds).
    deadline = time.time() + (10 if launched else 6)
    device = None
    while time.time() < deadline and device is None:
        tidy()
        device = find_device()
        if device is None:
            time.sleep(1)

    playing = False
    if device is None and launched:
        # Slow cold start: handing the client the track URI makes it register *and* auto-play.
        try:
            os.startfile(track["uri"])  # noqa: S606 - spotify:track:<id> from the API
        except OSError:
            pass
        deadline = time.time() + 18
        while time.time() < deadline and device is None:
            time.sleep(1)
            tidy()
            device = find_device()
    if device is None:
        raise SpotifyError("no_device", "Spotify non risulta disponibile come dispositivo")
    previous = device.get("volume_percent")
    try:
        SPOTIFY.set_volume(volume)
    except SpotifyError:
        pass

    def wait_playing(tries: int) -> bool:
        for _ in range(tries):
            time.sleep(0.8)
            tidy()
            try:
                if SPOTIFY.is_playing():
                    return True
            except SpotifyError:
                pass
        return False

    # A freshly registered client sometimes answers 403/404 to the first play command even
    # though it then starts: tolerate those and trust the player state instead.
    play_err: SpotifyError | None = None
    try:
        SPOTIFY._call("PUT", "/me/player/play", body={"uris": [track["uri"]], "position_ms": 0}, params={"device_id": str(device["id"])})  # noqa: SLF001
    except SpotifyError as exc:
        if exc.code not in ("premium_required", "no_device"):
            raise
        play_err = exc
    playing = wait_playing(5)
    if not playing:
        try:
            os.startfile(track["uri"])  # noqa: S606 - spotify:track:<id> from the API
        except OSError:
            pass
        playing = wait_playing(5)
        if not playing and play_err is not None:
            raise play_err

    def end() -> None:
        # Fade: a few volume steps down to 0, then pause. Each API call takes ~0.2-0.4 s.
        if fade > 0:
            steps = 5
            for i in range(1, steps + 1):
                level = int(round(volume * (1 - i / steps)))
                try:
                    SPOTIFY.set_volume(level)
                except Exception:
                    break
                time.sleep(max(0.05, fade / steps - 0.25))
        try:
            SPOTIFY.pause()
        except Exception:
            pass
        try:
            if previous is not None:
                SPOTIFY.set_volume(int(previous))
        except Exception:
            pass
        BUS.publish({"type": "intro_end", "track": track["name"]})

    threading.Timer(max(0.0, seconds - fade), end).start()
    return {"ok": True, "playing": playing, "track": track["name"], "artist": track["artist"], "seconds": seconds, "volume": volume, "fade": fade}


@app.post("/spotify/intro")
async def spotify_intro(req: IntroRequest, request: Request):
    pre = {"source": audit.source_of(request), "action": "spotify_intro", "target": "spotify", "query": req.query}
    try:
        res = await asyncio.to_thread(_run_intro, req.query, req.seconds, req.volume, req.fade)
    except SpotifyError as exc:
        status = 401 if exc.code == "not_logged_in" else 409
        audit.record("action", **pre, accepted=exc.code != "not_logged_in", executed=False, error=f"{exc.code}: {exc}")
        raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)}) from exc
    except Exception as exc:
        audit.record("action", **pre, accepted=True, executed=False, error=f"{type(exc).__name__}: {exc}")
        raise
    audit.record("action", **pre, accepted=True, executed=True, result=f"intro {res['track']} ({res['seconds']}s, playing={res['playing']})")
    return res


@app.post("/spotify/logout")
async def spotify_logout():
    SPOTIFY.logout()
    return SPOTIFY.status()


@app.get("/spotify/now")
async def spotify_now():
    if not SPOTIFY.logged_in:
        raise HTTPException(status_code=401, detail={"code": "not_logged_in", "message": "Spotify non collegato"})
    try:
        return {"now_playing": await asyncio.to_thread(SPOTIFY.now_playing), "devices": await asyncio.to_thread(SPOTIFY.devices)}
    except SpotifyError as exc:
        raise HTTPException(status_code=502, detail={"code": exc.code, "message": str(exc)}) from exc


def run() -> None:
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")


if __name__ == "__main__":
    run()
