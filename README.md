# Jarvis Local Action Bridge

Small loopback-only service that gives the Jarvis UI two local capabilities:

1. **Allowlisted desktop actions** — `POST /actions` with `{ "action", "target" }`.
   Only ids present in `allowlist.json` are accepted. Nothing is ever run
   through a shell: each allowlist *kind* maps to a fixed launcher
   (`Code.exe <allowlisted path>`, the Spotify app-execution alias, the default
   browser, `os.startfile(<known folder>)`).
2. **Offline wake word** — "hey jarvis" via [openWakeWord](https://github.com/dscripka/openWakeWord)
   (ONNX Runtime, pre-trained `hey_jarvis` model). Audio never leaves the PC.
   Events are streamed to the UI with Server-Sent Events.

## Run

```powershell
uv sync                                   # once
.\.venv\Scripts\jarvis-bridge.exe         # listens on http://127.0.0.1:8765
```

`scripts/start-jarvis.ps1` in `jarvis-ui` starts it together with everything else.

## Endpoints

| Method | Path            | Purpose |
|--------|-----------------|---------|
| GET    | `/health`       | `{status, wake}` |
| GET    | `/actions`      | Catalogue: action ids + target ids/labels (no paths) |
| POST   | `/actions`      | Run `{action, target}`; 404/422 when not allowlisted |
| GET    | `/wake/status`  | Model availability, running flag, detections |
| POST   | `/wake/start`   | Open the microphone and start detecting |
| POST   | `/wake/stop`    | Release the microphone |
| GET    | `/wake/events`  | SSE: `status`, `wake` (with score), `error`, `ping` |

## Security model

- Binds to `127.0.0.1` only; requests from any other client address get 403.
- CORS restricted to the Vite dev/preview origins.
- Request schema: `action ∈ {open_app, open_folder, open_project}`,
  `target` matches `^[a-z0-9][a-z0-9_-]*$` (paths, slashes and spaces are rejected
  before the allowlist is even consulted).
- No `shell=True`, no `cmd /c`, no PowerShell. Child processes are detached.
- Adding an action = adding an allowlist entry **and** a kind handler in
  `jarvis_bridge/actions.py`. There is deliberately no "run this command" kind.

## Tests

```powershell
.\.venv\Scripts\python.exe tests\smoke.py          # refusals must be refused
.\.venv\Scripts\python.exe tests\smoke.py --real   # also opens Spotify and VS Code (jarvis-ui)
.\.venv\Scripts\python.exe tests\wake_offline.py hey_jarvis.wav other.wav
```

## Speech-to-text for commands (`POST /stt`)

Multipart `file` + `language`. Same faster-whisper `base` model as OpenJarvis, but
with an *initial prompt* built from the allowlist vocabulary (Jarvis, Spotify,
Visual Studio Code, U2…). Measured on synthetic Italian commands: 4/8 → 8/8 correct
at the same ~3 s per clip; the `small` model was 4-6× slower and still imperfect.
The UI tries this endpoint first and falls back to OpenJarvis. `GET /stt/health`.
Note: `av` is pinned `<16` (newer PyAV breaks faster-whisper 1.2).

## Intent classifier (`POST /intent`)

Free text → one allowlisted `{action, target, query}` or `null`. The bridge calls
Ollama directly (`/api/chat`, `think: false`, `format: json`) with a compact prompt
generated from the allowlist. Preferred model `JARVIS_INTENT_MODEL` (default
`qwen3.5:4b`, ~20 s on this CPU); `trusted` in the response tells the UI whether
the answer may run without a confirmation. Measured: the OpenAI tool schema via
OpenJarvis costs 40-80 s on the same hardware.

## Timers, weather, files, closing apps

- `timer/set` with `query` = "10 minuti", "1 ora e 30", "alle 7:30" (alarm). Fires a
  `timer` event on `GET /events` (SSE) plus three beeps; `timer/cancel`, `timer/list`.
- `weather/forecast`, optional `query` = city and/or "domani". Open-Meteo, no API key.
  Default city is `default_city` in `allowlist.json`.
- `find_file/documents`, `query` = part of the file name. Read-only search in the
  `roots` listed in the allowlist (Downloads, Documents, Desktop, OneDrive); the newest
  match is revealed in Explorer, nothing is opened or executed.
- `close_app` (spotify, vscode, browser): graceful close via `taskkill /IM <image>`
  without `/F`, so apps can still prompt to save.

## Web search

`web_search` / `browser` with `query` opens `https://www.google.com/search?q=<query>`
in the default browser. Same sanitisation as music queries; no other URL is ever opened.

## Music and media

- `play_music` / `spotify` takes a `query` (letters, digits, spaces, `'’-.,&!`, max 80).
  Without credentials it opens `spotify:search:<query>` in the desktop client
  (Spotify is launched first if it is not running). With `SPOTIFY_CLIENT_ID`
  and `SPOTIFY_CLIENT_SECRET` set in the bridge's environment it resolves the
  top track through the Spotify Web API (client-credentials flow, no login) and
  opens `spotify:track:<id>`, which auto-plays.
- `media` targets `play_pause`, `next`, `previous`, `volume_up`, `volume_down`,
  `mute` send the matching Windows media key (`keybd_event`), nothing else.
- **User login (optional, recommended):** open `http://127.0.0.1:8765/spotify/login`
  once and accept. The bridge stores a refresh token in
  `%USERPROFILE%\.jarvis-bridge\spotify_token.json` (PKCE flow, redirect URI
  `http://127.0.0.1:8765/callback` must be registered in the Spotify app). From
  then on playback goes through the Web API: exact track on the active device,
  play/pause, next/previous, volume steps, and `media/now_playing` ("cosa sto
  ascoltando?"). Playback control needs Spotify Premium; without it the bridge
  falls back to media keys and track URIs. `POST /spotify/logout` forgets the token.

## Wake-word notes

- Phrase is **"hey jarvis"** (the model is trained for that phrase, not bare "Jarvis").
- Threshold `JARVIS_WAKE_THRESHOLD` (default 0.5); cooldown 2 s between detections.
- Model licence: openWakeWord code is Apache-2.0, the pre-trained models are
  CC BY-NC-SA 4.0 (non-commercial). Fine for personal use.
- Port override: `JARVIS_BRIDGE_PORT` (the UI reads `VITE_JARVIS_BRIDGE_URL`).
