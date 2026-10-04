"""Jarvis' second brain: notes, facts, reminders and lists, all local.

Storage is one SQLite file (``jarvis-bridge/data/brain.db``, excluded from Git)
with a full-text index, so nothing here depends on OpenJarvis.

Understanding is deterministic first (``understand``): a wide set of Italian
phrasings maps free text to one ``Command``. When the sentence clearly talks
about notes/reminders/lists but no rule matches, ``LocalExtractor`` asks the
local Ollama model to *extract a structure* (kind, text, when, list) that is
then validated and re-parsed here. The model never decides what to execute.

Reminders are checked by a background thread: when one is due it is marked as
fired, published on the event bus (the UI speaks it) and shown as a Windows
toast. Nothing else ever runs on its own.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import threading
import time
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import when as W
from .bus import EventBus

log = logging.getLogger("jarvis.brain")

DB_PATH = Path(os.environ.get("JARVIS_BRAIN_DB") or Path(__file__).resolve().parent.parent / "data" / "brain.db")
KINDS = ("note", "fact", "reminder", "list")
CONFIRM_TTL = 90  # seconds a destructive request waits for "sì"


class BrainError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


# --------------------------------------------------------------------------- #
# Storage
# --------------------------------------------------------------------------- #

class Embedder:
    """Sentence embeddings from Ollama (nomic-embed-text, ~270 MB, CPU-fast). Optional: any failure → None."""

    def __init__(self, model: str | None = None):
        self.model = model or os.environ.get("JARVIS_EMBED_MODEL", "nomic-embed-text")
        self.url = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
        self._ok: bool | None = None

    def embed(self, text: str) -> Any:
        if self._ok is False:
            return None
        try:
            import numpy as np

            req = urllib.request.Request(f"{self.url}/api/embeddings", data=json.dumps({"model": self.model, "prompt": text[:1000], "keep_alive": "30m"}).encode(),
                                         headers={"content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=30) as r:
                vec = np.asarray(json.load(r).get("embedding") or [], dtype=np.float32)
            if vec.size == 0:
                return None
            self._ok = True
            return vec / (float(np.linalg.norm(vec)) or 1.0)
        except Exception as exc:
            if self._ok is None:
                log.info("embeddings unavailable (%s): semantic search off, full-text only", str(exc)[:80])
            self._ok = False
            return None


class Store:
    def __init__(self, path: Path = DB_PATH, embedder: Embedder | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.embedder = embedder
        self._lock = threading.RLock()
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._init()

    def _init(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY,
                    kind TEXT NOT NULL,
                    text TEXT NOT NULL,
                    list_name TEXT,
                    due TEXT,
                    created TEXT NOT NULL,
                    done INTEGER NOT NULL DEFAULT 0,
                    fired INTEGER NOT NULL DEFAULT 0,
                    deleted INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_items_kind ON items(kind, deleted);
                CREATE INDEX IF NOT EXISTS idx_items_due ON items(due) WHERE kind='reminder' AND fired=0 AND deleted=0;
                CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(text, content='items', content_rowid='id', tokenize='unicode61 remove_diacritics 2');
                CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
                    INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text);
                END;
                CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
                    INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text);
                END;
                CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE OF text ON items BEGIN
                    INSERT INTO items_fts(items_fts, rowid, text) VALUES ('delete', old.id, old.text);
                    INSERT INTO items_fts(rowid, text) VALUES (new.id, new.text);
                END;
                """
            )
            # Migrations (additive only): recurrence, priority, semantic embedding.
            cols = {r["name"] for r in self._db.execute("PRAGMA table_info(items)")}
            for name, decl in (("repeat", "TEXT"), ("priority", "INTEGER NOT NULL DEFAULT 0"), ("embedding", "BLOB")):
                if name not in cols:
                    self._db.execute(f"ALTER TABLE items ADD COLUMN {name} {decl}")

    @staticmethod
    def _row(r: sqlite3.Row) -> dict[str, Any]:
        d = dict(r)
        d.pop("embedding", None)  # binary, never serialised
        d.pop("rank", None)
        return d

    def add(self, kind: str, text: str, *, list_name: str | None = None, due: datetime | None = None,
            repeat: str | None = None, priority: int = 0) -> dict[str, Any]:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO items(kind, text, list_name, due, created, repeat, priority) VALUES (?,?,?,?,?,?,?)",
                (kind, text, list_name, due.isoformat(timespec="minutes") if due else None,
                 datetime.now().isoformat(timespec="seconds"), repeat, int(priority)),
            )
            item_id = int(cur.lastrowid)
        if self.embedder is not None and kind in ("note", "fact", "reminder"):
            threading.Thread(target=self._embed_later, args=(item_id, text), daemon=True).start()
        return self.get(item_id)

    def _embed_later(self, item_id: int, text: str) -> None:
        vec = self.embedder.embed(text) if self.embedder else None
        if vec is None:
            return
        with self._lock, self._db:
            self._db.execute("UPDATE items SET embedding=? WHERE id=?", (vec.tobytes(), item_id))

    def snooze(self, item_id: int, until: datetime) -> dict[str, Any]:
        """Move a (fired) reminder forward and re-arm it."""
        with self._lock, self._db:
            self._db.execute("UPDATE items SET due=?, fired=0, done=0 WHERE id=?", (until.isoformat(timespec="minutes"), item_id))
        return self.get(item_id)

    def last_fired(self) -> dict[str, Any] | None:
        with self._lock:
            r = self._db.execute(
                "SELECT * FROM items WHERE kind='reminder' AND deleted=0 AND done=0 AND fired=1 ORDER BY due DESC LIMIT 1"
            ).fetchone()
        return self._row(r) if r else None

    def get(self, item_id: int) -> dict[str, Any]:
        with self._lock:
            r = self._db.execute("SELECT * FROM items WHERE id=? AND deleted=0", (item_id,)).fetchone()
        if not r:
            raise BrainError("not_found", "elemento non trovato")
        return self._row(r)

    def list(self, kind: str, *, list_name: str | None = None, include_done: bool = False, limit: int = 50, since: datetime | None = None) -> list[dict[str, Any]]:
        q = "SELECT * FROM items WHERE kind=? AND deleted=0"
        args: list[Any] = [kind]
        if list_name is not None:
            q += " AND lower(list_name)=lower(?)"
            args.append(list_name)
        if not include_done:
            q += " AND done=0"
        if since is not None:
            q += " AND created>=?"
            args.append(since.isoformat(timespec="seconds"))
        q += " ORDER BY " + ("due ASC" if kind == "reminder" else "created DESC") + " LIMIT ?"
        args.append(limit)
        with self._lock:
            return [self._row(r) for r in self._db.execute(q, args)]

    def list_names(self) -> list[tuple[str, int]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT list_name, COUNT(*) AS n FROM items WHERE kind='list' AND deleted=0 AND done=0 GROUP BY lower(list_name) ORDER BY n DESC"
            ).fetchall()
        return [(r["list_name"], r["n"]) for r in rows]

    def upcoming(self, *, until: datetime | None = None, include_overdue: bool = True, limit: int = 20) -> list[dict[str, Any]]:
        q = "SELECT * FROM items WHERE kind='reminder' AND deleted=0 AND done=0"
        args: list[Any] = []
        if until is not None:
            q += " AND due<=?"
            args.append(until.isoformat(timespec="minutes"))
        if not include_overdue:
            q += " AND fired=0"
        q += " ORDER BY due ASC LIMIT ?"
        args.append(limit)
        with self._lock:
            return [self._row(r) for r in self._db.execute(q, args)]

    def due_now(self) -> list[dict[str, Any]]:
        now = datetime.now().isoformat(timespec="minutes")
        with self._lock:
            return [self._row(r) for r in self._db.execute(
                "SELECT * FROM items WHERE kind='reminder' AND deleted=0 AND done=0 AND fired=0 AND due<=? ORDER BY due", (now,))]

    def update(self, item_id: int, *, text: str | None = None, due: str | None = None, clear_due: bool = False,
               done: bool | None = None, list_name: str | None = None) -> dict[str, Any]:
        """Edit from the UI. `due` is an ISO local datetime; `clear_due` removes it. Re-arms a fired reminder when moved."""
        self.get(item_id)
        sets: list[str] = []
        args: list[Any] = []
        if text is not None and text.strip():
            sets.append("text=?"); args.append(text.strip()[:500])
        if clear_due:
            sets.append("due=NULL"); sets.append("fired=0")
        elif due is not None:
            try:
                when = datetime.fromisoformat(due)
            except ValueError as exc:
                raise BrainError("bad_due", "data non valida") from exc
            sets.append("due=?"); args.append(when.isoformat(timespec="minutes"))
            sets.append("fired=?"); args.append(1 if when <= datetime.now() else 0)
        if done is not None:
            sets.append("done=?"); args.append(1 if done else 0)
        if list_name is not None:
            sets.append("list_name=?"); args.append(list_name.strip().lower()[:30] or None)
        if sets:
            with self._lock, self._db:
                self._db.execute(f"UPDATE items SET {', '.join(sets)} WHERE id=?", (*args, item_id))
        return self.get(item_id)

    def mark(self, item_id: int, **flags: int) -> None:
        cols = ", ".join(f"{k}=?" for k in flags)
        with self._lock, self._db:
            self._db.execute(f"UPDATE items SET {cols} WHERE id=?", (*flags.values(), item_id))

    def search(self, query: str, *, kinds: tuple[str, ...] = KINDS, limit: int = 8) -> list[dict[str, Any]]:
        """Full-text search (prefix matching on every token), newest first among equal ranks."""
        tokens = [t for t in re.findall(r"\w+", query.lower()) if len(t) >= 3 and t not in _STOP]
        placeholders = ",".join("?" for _ in kinds)
        rows: list[sqlite3.Row] = []
        if tokens:
            fts = " OR ".join(f'"{t}"*' for t in tokens)
            with self._lock:
                try:
                    rows = self._db.execute(
                        f"SELECT i.*, bm25(items_fts) AS rank FROM items_fts JOIN items i ON i.id=items_fts.rowid "
                        f"WHERE items_fts MATCH ? AND i.deleted=0 AND i.kind IN ({placeholders}) ORDER BY rank, i.created DESC LIMIT ?",
                        (fts, *kinds, limit),
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    log.warning("fts query failed: %s", exc)
        out = [self._row(r) for r in rows]
        for o in out:
            o.pop("rank", None)
            o.pop("embedding", None)
        # Semantic complement: "quella cosa sul fornitore" finds "il nuovo grossista si chiama Rossi".
        if self.embedder is not None and len(out) < limit:
            out = self._semantic(query, kinds, limit, out)
        return out

    def _semantic(self, query: str, kinds: tuple[str, ...], limit: int, have: list[dict[str, Any]]) -> list[dict[str, Any]]:
        q = self.embedder.embed(query) if self.embedder else None
        if q is None:
            return have
        import numpy as np

        placeholders = ",".join("?" for _ in kinds)
        with self._lock:
            rows = self._db.execute(
                f"SELECT * FROM items WHERE deleted=0 AND embedding IS NOT NULL AND kind IN ({placeholders}) ORDER BY created DESC LIMIT 2000", kinds
            ).fetchall()
        seen = {h["id"] for h in have}
        scored = []
        for r in rows:
            if r["id"] in seen:
                continue
            v = np.frombuffer(r["embedding"], dtype=np.float32)
            if v.size != q.size:
                continue
            s = float(np.dot(v, q))
            if s >= 0.62:
                scored.append((s, r))
        scored.sort(key=lambda t: -t[0])
        for s, r in scored[: limit - len(have)]:
            d = self._row(r)
            d.pop("embedding", None)
            d["similarity"] = round(s, 3)
            have.append(d)
        return have

    def count(self) -> dict[str, int]:
        with self._lock:
            rows = self._db.execute("SELECT kind, COUNT(*) AS n FROM items WHERE deleted=0 AND done=0 GROUP BY kind").fetchall()
        out = {k: 0 for k in KINDS}
        for r in rows:
            out[r["kind"]] = r["n"]
        return out


_STOP = {"che", "con", "per", "del", "della", "dei", "delle", "degli", "nel", "nella", "sul", "sulla", "una", "uno", "gli", "le",
         "cosa", "come", "dove", "quando", "sai", "ricordi", "hai", "sono", "mio", "mia", "miei", "mie", "questo", "quella"}


# --------------------------------------------------------------------------- #
# Understanding
# --------------------------------------------------------------------------- #

@dataclass
class Command:
    kind: str          # note | fact | reminder | list | recall | today | forget | none
    op: str            # add | show | remove | clear | cancel | done | search | summary
    text: str = ""
    list_name: str | None = None
    when: W.When | None = None
    repeat: str | None = None   # daily | weekly:<0-6> | monthly:<dom> | weekdays
    priority: int = 0           # 0 normal, 1 important, 2 urgent
    confirm: bool = False   # destructive: needs an explicit yes
    source: str = "rules"   # rules | model
    debug: dict[str, Any] = field(default_factory=dict)


_LEAD = re.compile(r"^\s*(?:ehi|hey|ok|okay|ciao|senti|allora|jarvis|per\s+favore|per\s+cortesia|puoi|potresti)[\s,:!.]*", re.IGNORECASE)
_TAIL = re.compile(r"[\s,.!?]*(?:per\s+favore|grazie|ok|jarvis)?[\s,.!?]*$", re.IGNORECASE)
_QUOTES = re.compile(r"^[\"«»“”']|[\"«»“”']$")

# Words that make a sentence "about the second brain" even when no rule matches → model fallback.
BRAIN_HINT = re.compile(
    r"\b(?:ricord\w*|promemoria|appunt\w*|annot\w*|nota|note|segn\w*|lista|liste|spesa|memorizz\w*|dimentic\w*|avvis\w*|avvert\w*|"
    r"scadenz\w*|impegn\w*|programma|agenda|cosa\s+devo|cosa\s+ho|tieni\s+a\s+mente|sappi|tieni\s+presente)\b",
    re.IGNORECASE,
)

# --- list handling -----------------------------------------------------------
_LIST_NAME = r"(?:lista\s+(?:della\s+|dello\s+|dei\s+|delle\s+|di\s+|del\s+)?(?P<name>[\w' ]{2,30}?)|(?P<spesa>spesa))"
_LIST_ADD = re.compile(
    rf"^(?:aggiungi|metti|inserisci|segna|appunta|scrivi|aggiungimi|mettimi|segnami)\s+(?P<items>.+?)\s+(?:alla|nella|sulla|in|a|su)\s+(?:la\s+)?{_LIST_NAME}\s*$",
    re.IGNORECASE,
)
_LIST_ADD2 = re.compile(
    rf"^(?:(?:alla|nella|sulla|in|per)\s+(?:la\s+)?{_LIST_NAME}\s*[:,]?\s+)(?:aggiungi|metti|inserisci|segna|ci\s+serve|serve|servono|manca|mancano)?\s*(?P<items>.+)$",
    re.IGNORECASE,
)
_LIST_ADD3 = re.compile(rf"^{_LIST_NAME}\s*[:]\s*(?P<items>.+)$", re.IGNORECASE)
_LIST_NEED = re.compile(r"^(?:ci\s+serve|ci\s+servono|serve|servono|manca|mancano|dobbiamo\s+comprare|devo\s+comprare|compra(?:re)?|da\s+comprare)[:\s]+(?P<items>.+)$", re.IGNORECASE)
_LIST_REMOVE = re.compile(
    rf"^(?:togli|rimuovi|cancella|elimina|leva|depenna|spunta|ho\s+(?:comprato|preso|fatto))\s+(?P<items>.+?)\s+(?:dalla|dalle|da|della|nella|dal)\s+(?:la\s+)?{_LIST_NAME}\s*$",
    re.IGNORECASE,
)
_LIST_SHOW = re.compile(
    r"^(?:(?:cosa|che\s+cosa|che)\s+(?:c['’]è|c['’]e|ho|abbiamo|serve|manca|devo\s+comprare)\s+(?:nella|sulla|in|per|alla)\s+(?:la\s+)?|"
    r"(?:leggi(?:mi)?|mostra(?:mi)?|dimmi|apri|fammi\s+vedere|vediamo|dammi)\s+(?:la\s+)?|)"
    rf"{_LIST_NAME}\s*\??$",
    re.IGNORECASE,
)
_LIST_CLEAR = re.compile(rf"^(?:svuota|azzera|pulisci|cancella\s+(?:tutta\s+)?|elimina\s+(?:tutta\s+)?|resetta)\s*(?:la\s+)?{_LIST_NAME}\s*$", re.IGNORECASE)
_LISTS_SHOW = re.compile(r"^(?:quali|che|quante)\s+liste\s+(?:ho|abbiamo|ci\s+sono)|^(?:le\s+mie\s+liste|mostrami\s+le\s+liste|elenca\s+le\s+liste)\s*\??$", re.IGNORECASE)

# --- notes -------------------------------------------------------------------
_NOTE_ADD = re.compile(
    r"^(?:prendi\s+(?:una\s+)?nota|prendi\s+appunti|appunta(?:ti|mi)?|annota(?:ti|mi)?|nota|segna(?:ti|mi)?|scrivi(?:ti|mi)?|"
    r"aggiungi\s+(?:una\s+)?nota|nuova\s+nota|metti\s+(?:per\s+iscritto|nelle\s+note|una\s+nota)|salva\s+(?:una\s+)?nota|"
    r"butta\s+giù|tieni\s+traccia)\s*(?:che|di|:|,|-|–)?\s*(?P<text>.+)$",
    re.IGNORECASE,
)
_NOTE_SHOW = re.compile(
    r"^(?:(?:cosa|che\s+cosa|che|quali)\s+(?:note|appunti)\s+(?:ho|abbiamo|ci\s+sono)|(?:cosa|che\s+cosa|che)\s+(?:ho|avevo|mi\s+ero)\s+(?:annotato|appuntato|segnato|scritto)|"
    r"(?:leggi(?:mi)?|mostra(?:mi)?|dimmi|rileggi(?:mi)?|fammi\s+vedere|apri|dammi)\s+(?:le\s+|gli\s+|i\s+|le\s+mie\s+|i\s+miei\s+)?(?:note|appunti|ultime\s+note|ultimi\s+appunti))"
    r"(?:\s+(?:di|su|sul|sulla|riguardo\s+a?|a\s+proposito\s+di|che\s+parlano\s+di)\s+(?P<topic>.+?))?\s*\??$",
    re.IGNORECASE,
)
_NOTE_SHOW_TODAY = re.compile(r"\b(?:di\s+oggi|oggi|di\s+ieri|ieri|recenti|ultime|ultimi|della\s+settimana)\b", re.IGNORECASE)

# --- facts (long-term memory) ----------------------------------------------
_FACT_ADD = re.compile(
    r"^(?:ricordati|ricorda|memorizza|tieni\s+a\s+mente|tieni\s+presente|sappi|impara|non\s+dimenticare|segnati\s+in\s+memoria|salva\s+(?:in|nella)\s+memoria)"
    r"\s*(?:che|:|,|-|–|di\s+ricordare\s+che)?\s*(?P<text>.+)$",
    re.IGNORECASE,
)
_RECALL = re.compile(
    r"^(?:(?:ti\s+)?ricordi|(?:cosa|che\s+cosa|che)\s+(?:sai|ricordi|ti\s+ho\s+detto|ti\s+avevo\s+detto|ne\s+sai)|cosa\s+ti\s+ricordi|"
    r"(?:che|quale|qual['’]?è|quali|quanto|quanti|chi|dove|quando|come)\s+(?:.*?)|hai\s+(?:qualcosa|appunti|note|memoria))"
    r"\s*(?:di|su|sul|sulla|riguardo\s+a?|a\s+proposito\s+di|che)?\s*(?P<topic>.+?)\s*\??$",
    re.IGNORECASE,
)
_RECALL_TRIGGER = re.compile(r"^(?:ti\s+ricordi|ricordi|cosa\s+sai|che\s+sai|cosa\s+ti\s+ho\s+detto|cosa\s+ti\s+avevo\s+detto|cosa\s+ti\s+ricordi|hai\s+(?:qualcosa|appunti|note|memoria|in\s+memoria))", re.IGNORECASE)
_FORGET = re.compile(r"^(?:dimentica(?:ti)?|scordati|cancella\s+(?:dalla\s+memoria|il\s+ricordo|la\s+memoria)|rimuovi\s+dalla\s+memoria|non\s+ricordare\s+più)\s*(?:che|:|,|di)?\s*(?P<text>.+)$", re.IGNORECASE)

# --- reminders ---------------------------------------------------------------
_REM_ADD = re.compile(
    r"^(?:ricordami|ricordamelo|ricorda(?:ti)?\s+di\s+ricordarmi|promemoria|metti(?:mi)?\s+(?:un\s+)?promemoria|crea\s+(?:un\s+)?promemoria|"
    r"aggiungi\s+(?:un\s+)?promemoria|imposta\s+(?:un\s+)?promemoria|avvisami|avvertimi|chiamami|segnami|segna(?:ti)?|appunta(?:ti)?|"
    r"devo|dovrei|ho\s+da|non\s+(?:devo|far(?:mi)?)\s+dimenticare|mi\s+devi\s+ricordare|devi\s+ricordar(?:mi|e)|fammi\s+ricordare|ricordamelo)"
    r"\s*(?:che|di|:|,|-|–)?\s*(?P<text>.+)$",
    re.IGNORECASE,
)
_REM_SHOW = re.compile(
    r"^(?:(?:cosa|che\s+cosa|che|quali|quanti)\s+(?:promemoria|impegni|scadenze|appuntamenti)\s*(?:ho|abbiamo|ci\s+sono)?|"
    r"(?:cosa|che\s+cosa|che)\s+(?:devo\s+fare|ho\s+da\s+fare|ho\s+in\s+programma|mi\s+aspetta|c['’]è\s+in\s+programma|ho\s+in\s+agenda)|"
    r"(?:leggi(?:mi)?|mostra(?:mi)?|dimmi|elenca|dammi)\s+(?:i\s+|gli\s+|le\s+|i\s+miei\s+|le\s+mie\s+)?(?:promemoria|impegni|scadenze|appuntamenti|agenda)|"
    r"(?:i\s+miei\s+)?(?:promemoria|impegni|agenda))"
    r"(?:\s+(?:di|per))?\s*(?P<scope>oggi|domani|dopodomani|questa\s+settimana|della\s+settimana|settimana|stasera|tutti|attivi)?\s*\??$",
    re.IGNORECASE,
)
_REM_CANCEL = re.compile(
    r"^(?:cancella|annulla|elimina|togli|rimuovi|disattiva)\s+(?:il\s+|la\s+|lo\s+)?(?:promemoria|ricordo|avviso|impegno|scadenza)?\s*(?:di|del|della|per|su|:)?\s*(?P<text>.+)$",
    re.IGNORECASE,
)
_REM_DONE = re.compile(r"^(?:fatto|ho\s+fatto|completato|ho\s+completato|segna\s+come\s+fatto|fatto\s+il\s+promemoria)[\s,:]*(?:di|il|la)?\s*(?P<text>.*)$", re.IGNORECASE)

# --- day summary -------------------------------------------------------------
_TODAY = re.compile(
    r"^(?:buongiorno|buon\s+giorno|riepilogo|riassunto|briefing|fammi\s+il\s+punto|facciamo\s+il\s+punto|com['’]è\s+la\s+giornata|"
    r"cosa\s+(?:c['’]è|ho|abbiamo)\s+(?:oggi|in\s+programma\s+oggi|per\s+oggi)|programma\s+di\s+oggi|la\s+mia\s+giornata|cosa\s+mi\s+aspetta(?:\s+oggi)?)\s*[!?.]*$",
    re.IGNORECASE,
)

# --- recurrence / priority / snooze / evening -------------------------------
_REPEAT = re.compile(
    r"\b(?:(?P<daily>ogni\s+giorno|tutti\s+i\s+giorni|ogni\s+mattina|tutte\s+le\s+mattine|ogni\s+sera|tutte\s+le\s+sere|quotidian\w+)|"
    r"(?P<weekdays>(?:ogni|tutti\s+i)\s+giorn[oi]\s+(?:feriali|lavorativ[oi])|dal\s+luned[iì]\s+al\s+venerd[iì])|"
    r"(?P<weekly>ogni\s+settimana|tutte\s+le\s+settimane|settimanal\w+)|"
    r"(?:ogni|tutti\s+i|tutte\s+le)\s+(?P<wd>luned[iì]|marted[iì]|mercoled[iì]|gioved[iì]|venerd[iì]|sabat[oi]|domenic[ae])|"
    r"(?P<monthly>ogni\s+mese|tutti\s+i\s+mesi|mensil\w+)(?:\s+il\s+(?P<dom>\d{1,2}))?)\b",
    re.IGNORECASE,
)
_PRIORITY = re.compile(r"[\s,]*\b(?:(?P<urgent>urgente|urgentissim[oa]|priorit[àa]\s+massima|assolutamente)|(?P<important>importante|priorit[àa]\s+alta|da\s+non\s+dimenticare))\b[\s,!]*", re.IGNORECASE)
_SNOOZE = re.compile(
    r"^(?:rimanda(?:lo|la|melo)?|posticipa(?:lo|la|melo)?|rinvia(?:lo|la|melo)?|ricordamelo\s+(?:più\s+tardi|dopo|di\s+nuovo)|più\s+tardi|dopo|non\s+ora|adesso\s+no|snooze)"
    r"(?:\s+(?P<text>.+?))?(?:\s+(?:di|a|tra|fra|per)\s+(?P<when>.+))?\s*$",
    re.IGNORECASE,
)
_EVENING = re.compile(r"^(?:buonanotte|buona\s+notte|chiudiamo\s+la\s+giornata|riepilogo\s+serale|com['’]?è\s+andata(?:\s+oggi)?|cosa\s+(?:resta|rimane)(?:\s+da\s+fare)?|cosa\s+c['’]è\s+domani|programma\s+di\s+domani)\s*[!?.]*$", re.IGNORECASE)
_FIRST = re.compile(r"^(?:da\s+dove\s+(?:partiamo|parto|comincio|inizio)|cosa\s+faccio\s+(?:per\s+)?prim[ao]|qual['’]?è\s+la\s+cosa\s+più\s+(?:urgente|importante)|cosa\s+(?:è\s+)?più\s+urgente|priorit[àa](?:\s+di\s+oggi)?)\s*[!?.]*$", re.IGNORECASE)
_WD_INDEX = {"luned": 0, "marted": 1, "mercoled": 2, "gioved": 3, "venerd": 4, "sabat": 5, "domenic": 6}


def _parse_repeat(text: str) -> tuple[str | None, str]:
    """Returns (repeat code, text without the recurrence words)."""
    m = _REPEAT.search(text)
    if not m:
        return None, text
    if m.group("daily"):
        code = "daily"
    elif m.group("weekdays"):
        code = "weekdays"
    elif m.group("weekly"):
        code = "weekly"
    elif m.group("wd"):
        key = next(k for k in _WD_INDEX if m.group("wd").lower().startswith(k))
        code = f"weekly:{_WD_INDEX[key]}"
    else:
        code = f"monthly:{m.group('dom')}" if m.group("dom") else "monthly"
    rest = (text[: m.start()] + " " + text[m.end():]).strip()
    # "ogni mattina / ogni sera" carry a default hour when none is given.
    low = m.group(0).lower()
    if "mattin" in low and not re.search(r"\balle\b|\d", rest):
        rest += " alle 8"
    elif "ser" in low and "sera" in low and not re.search(r"\balle\b|\d", rest):
        rest += " alle 20"
    return code, re.sub(r"\s+", " ", rest)


def _parse_priority(text: str) -> tuple[int, str]:
    m = _PRIORITY.search(text)
    if not m:
        return 0, text
    level = 2 if m.group("urgent") else 1
    return level, re.sub(r"\s+", " ", (text[: m.start()] + " " + text[m.end():])).strip(" ,.")


def next_occurrence(due: datetime, repeat: str, now: datetime | None = None) -> datetime | None:
    """Next due after `due` (and after now) for a recurrence code."""
    now = now or datetime.now()
    nxt = due
    for _ in range(400):
        if repeat == "daily":
            nxt += timedelta(days=1)
        elif repeat == "weekdays":
            nxt += timedelta(days=1)
            while nxt.weekday() >= 5:
                nxt += timedelta(days=1)
        elif repeat.startswith("weekly"):
            nxt += timedelta(days=7)
        elif repeat.startswith("monthly"):
            month = nxt.month % 12 + 1
            year = nxt.year + (1 if month == 1 else 0)
            dom = int(repeat.split(":")[1]) if ":" in repeat else nxt.day
            for d in range(dom, 27, -1):
                try:
                    nxt = nxt.replace(year=year, month=month, day=d)
                    break
                except ValueError:
                    continue
        else:
            return None
        if nxt > now:
            return nxt
    return None


def describe_repeat(repeat: str | None) -> str:
    if not repeat:
        return ""
    if repeat == "daily":
        return "ogni giorno"
    if repeat == "weekdays":
        return "nei giorni feriali"
    if repeat.startswith("weekly:"):
        return "ogni " + W._WEEKDAY_NAMES[int(repeat.split(":")[1])]
    if repeat == "weekly":
        return "ogni settimana"
    if repeat.startswith("monthly:"):
        return f"ogni mese il {repeat.split(':')[1]}"
    return "ogni mese"


_DAY_WORD = re.compile(r"\b(?:oggi|domani|dopodomani|stasera|stamattina|stanotte|luned|marted|mercoled|gioved|venerd|sabato|domenica|settimana|\d{1,2}/\d{1,2}|il\s+\d{1,2})", re.IGNORECASE)
_SPLIT_ITEMS = re.compile(r"\s*(?:,|;|\be\b|\bpoi\b|\banche\b|\be\s+anche\b|\+)\s*", re.IGNORECASE)
_DIRECT_FACT = re.compile(r"^(?:il\s+mio|la\s+mia|i\s+miei|le\s+mie|mia\s+|mio\s+)\s*\w+\s+(?:è|e['’]|sono|si\s+chiama|ha|compie|nasce)\b", re.IGNORECASE)


def _norm(text: str) -> str:
    t = _LEAD.sub("", text.strip())
    t = _TAIL.sub("", t)
    t = _QUOTES.sub("", t.strip())
    return re.sub(r"\s+", " ", t).strip()


def _list_name(m: re.Match[str]) -> str:
    if m.groupdict().get("spesa"):
        return "spesa"
    name = (m.group("name") or "").strip().lower()
    return name or "spesa"


def _items(raw: str) -> list[str]:
    parts = [p.strip(" .") for p in _SPLIT_ITEMS.split(raw) if p and p.strip(" .")]
    out: list[str] = []
    for p in parts:
        p = re.sub(r"^(?:il|lo|la|i|gli|le|un|una|uno|del|della|dei|delle|dello)\s+", "", p, flags=re.IGNORECASE).strip()
        if p and p.lower() not in ("e", "anche"):
            out.append(p)
    return out[:20]


def understand(text: str, now: datetime | None = None) -> Command:
    """Free text → Command. Returns kind='none' when the sentence is not for the brain."""
    now = now or datetime.now()
    t = _norm(text)
    if not t:
        return Command("none", "none")
    low = t.lower()

    # --- day summary
    if _TODAY.match(t):
        return Command("today", "summary")

    # --- lists (before notes: "segna il latte nella spesa" is a list, not a note)
    if _LISTS_SHOW.match(t):
        return Command("list", "names")
    m = _LIST_CLEAR.match(t)
    if m:
        return Command("list", "clear", list_name=_list_name(m), confirm=True)
    m = _LIST_REMOVE.match(t)
    if m:
        return Command("list", "remove", text=m.group("items"), list_name=_list_name(m))
    m = _LIST_SHOW.match(t)
    if m:
        return Command("list", "show", list_name=_list_name(m))
    for rx in (_LIST_ADD, _LIST_ADD2, _LIST_ADD3):
        m = rx.match(t)
        if m and m.group("items"):
            return Command("list", "add", text=m.group("items"), list_name=_list_name(m))
    m = _LIST_NEED.match(t)
    if m:
        return Command("list", "add", text=m.group("items"), list_name="spesa")

    # --- forget / recall (memory questions)
    m = _FORGET.match(t)
    if m:
        return Command("forget", "remove", text=m.group("text"), confirm=True)
    if _RECALL_TRIGGER.match(t):
        m = _RECALL.match(t)
        topic = (m.group("topic") if m else t).strip(" ?")
        topic = re.sub(r"^(?:qualcosa\s+)?(?:di|su|sul|sulla|riguardo\s+a?|a\s+proposito\s+di|che|se)\s+", "", topic, flags=re.IGNORECASE)
        return Command("recall", "search", text=topic)

    # --- evening wrap-up, "where do I start", snooze of the last fired reminder
    if _EVENING.match(t):
        return Command("today", "evening")
    if _FIRST.match(t):
        return Command("today", "first")
    m = _SNOOZE.match(t)
    if m and len(t.split()) <= 12:
        when_text = m.group("when") or ""
        subject = (m.group("text") or "").strip()
        # "rimandalo di un'ora" → when = "tra un'ora"; "ricordamelo domani" → day word in subject
        w = W.parse("tra " + when_text, now) if when_text else None
        if w is None and subject:
            w = W.parse(subject, now)
            if w is not None:
                subject = w.rest
        if w is None and when_text:
            w = W.parse(when_text, now)
        if w is None:
            w = W.parse("tra 30 minuti", now)
        return Command("reminder", "snooze", text=re.sub(r"^(?:il|la|lo|quello|quella|questo|questa)\s+", "", subject), when=w)

    # --- reminders: shows / cancel / done
    m = _REM_SHOW.match(t)
    if m and not _NOTE_SHOW.match(t):
        scope = (m.group("scope") or "").strip().lower()
        return Command("reminder", "show", text=scope)
    m = _REM_DONE.match(t)
    if m and re.sub(r"[^\w]", "", low.split()[0]) in ("fatto", "ho", "completato", "segna"):
        return Command("reminder", "done", text=m.group("text").strip())
    m = _REM_CANCEL.match(t)
    if m and re.search(r"\b(?:promemoria|ricordo|avviso|impegno|scadenza)\b", low):
        return Command("reminder", "cancel", text=m.group("text").strip())

    # --- notes: show
    m = _NOTE_SHOW.match(t)
    if m:
        topic = (m.group("topic") or "").strip()
        if _NOTE_SHOW_TODAY.search(topic) or _NOTE_SHOW_TODAY.search(low) and not topic:
            return Command("note", "show", text="recent")
        return Command("note", "show", text=topic)

    # --- reminders: add. Anything with a reminder verb *and* a recognisable moment.
    m = _REM_ADD.match(t)
    if m:
        body = m.group("text")
        priority, body = _parse_priority(body)
        repeat, body = _parse_repeat(body)
        w = W.parse(body, now)
        verb = re.sub(r"[^\w]", "", low.split()[0])
        note_verb = verb.startswith(("segn", "appunt"))
        # For note-like verbs only a *clear* moment (clock, relative, or day word) makes it a reminder:
        # "segnati che il wifi è lento la mattina" is a note, "segnati che domani ho il dentista" a reminder.
        strong = w is not None and (w.explicit_time or bool(_DAY_WORD.search(w.matched)))
        if repeat and w is None:
            w = W.parse(body + " alle 9", now)  # "ogni lunedì portare fuori il vetro" → 9:00 by default
        if w is not None and (strong or not note_verb or repeat):
            subject = _subject(w.rest) or _subject(body)
            return Command("reminder", "add", text=subject, when=w, repeat=repeat, priority=priority)
        if verb in ("ricordami", "ricordamelo", "promemoria", "avvisami", "avvertimi", "chiamami", "devo", "dovrei", "ho", "mi", "devi", "fammi", "non") or "promemoria" in low:
            # A reminder without a time: keep it as an open task (due unknown).
            return Command("reminder", "add", text=_subject(body), when=None, priority=priority)
        # "segna / appunta <something>" without a time is a note (below).

    # --- facts: "ricordati che …", "il mio X è Y"
    m = _FACT_ADD.match(t)
    if m:
        body = m.group("text")
        # "ricordati di chiamare Marco domani" is a reminder, not a fact.
        w = W.parse(body, now)
        if w is not None and re.match(r"^(?:di\s+)?\w+(?:re|are|ire|ere)\b", body.strip(), re.IGNORECASE):
            return Command("reminder", "add", text=_subject(w.rest), when=w)
        if w is not None and re.match(r"^di\s+", body.strip(), re.IGNORECASE):
            return Command("reminder", "add", text=_subject(w.rest), when=w)
        return Command("fact", "add", text=body.strip())
    if _DIRECT_FACT.match(t) and len(t.split()) <= 14:
        return Command("fact", "add", text=t)

    # --- notes: add
    m = _NOTE_ADD.match(t)
    if m:
        body = m.group("text").strip()
        w = W.parse(body, now)
        # "segnati che domani ho il dentista alle 16" → that is a reminder.
        if w is not None and w.explicit_time and low.split()[0].startswith(("segn", "appunt")):
            return Command("reminder", "add", text=_subject(w.rest), when=w)
        return Command("note", "add", text=body)

    return Command("none", "none")


def _subject(text: str) -> str:
    s = re.sub(r"^(?:di|che|a|per|il|la|lo|le|i|gli)\s+", "", text.strip(" ,.-:"), flags=re.IGNORECASE)
    s = re.sub(r"^(?:devo|dovrei|ho\s+da|che\s+devo|di\s+dover)\s+", "", s, flags=re.IGNORECASE)
    return s.strip(" ,.-:")


# --------------------------------------------------------------------------- #
# Model fallback: extraction only
# --------------------------------------------------------------------------- #

class LocalExtractor:
    """Asks the local Ollama model to extract {kind, op, text, when, list} as JSON; everything is re-validated."""

    PROMPT = (
        "Sei il parser dell'assistente personale di Alessio. Estrai dalla frase UNA struttura JSON e nient'altro:\n"
        '{"kind": "note|fact|reminder|list|recall|today|none", "op": "add|show|remove|clear|cancel|done|search|summary", '
        '"text": "contenuto o argomento, senza la parte temporale", "when": "espressione temporale in italiano così com\'è, oppure null", "list": "nome lista oppure null"}\n'
        "Regole: note=appunto senza scadenza; fact=informazione personale da ricordare per sempre; reminder=cosa da fare, con o senza orario; "
        "list=aggiungere/togliere/leggere elementi di una lista (es. spesa); recall=domanda su cosa sai/ricordi; today=riepilogo della giornata; "
        'none=non riguarda appunti, promemoria, liste o memoria. Non inventare testo. Rispondi solo con il JSON.'
    )
    OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
    MODEL = property(lambda self: os.environ.get("JARVIS_INTENT_MODEL", "qwen3.5:4b"))

    def extract(self, text: str, now: datetime | None = None) -> Command | None:
        body = {
            "model": self.MODEL, "stream": False, "keep_alive": "30m", "think": False, "format": "json",
            "options": {"temperature": 0, "num_predict": 120},
            "messages": [{"role": "system", "content": self.PROMPT}, {"role": "user", "content": text[:300]}],
        }
        req = urllib.request.Request(f"{self.OLLAMA_URL}/api/chat", data=json.dumps(body).encode(), headers={"content-type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.load(r)
        except Exception as exc:
            if "400" in str(exc) and "think" in body:
                body.pop("think")
                req = urllib.request.Request(f"{self.OLLAMA_URL}/api/chat", data=json.dumps(body).encode(), headers={"content-type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as r:
                    data = json.load(r)
            else:
                log.warning("extractor unavailable: %s", exc)
                return None
        raw = str(data.get("message", {}).get("content", ""))
        m = re.search(r"\{[\s\S]*\}", raw)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except ValueError:
            return None
        kind = str(parsed.get("kind") or "none").lower()
        op = str(parsed.get("op") or "add").lower()
        if kind not in ("note", "fact", "reminder", "list", "recall", "today"):
            return None
        subject = re.sub(r"\s+", " ", str(parsed.get("text") or "")).strip()[:300]
        when_s = parsed.get("when")
        w = W.parse(str(when_s), now) if isinstance(when_s, str) and when_s.strip() and when_s.lower() != "null" else None
        if w is None and kind == "reminder" and subject:
            w = W.parse(subject, now)
            if w:
                subject = w.rest or subject
        list_name = str(parsed.get("list") or "").strip().lower()[:30] or None
        if kind == "list" and not list_name:
            list_name = "spesa"
        if kind in ("note", "fact", "reminder") and op == "add" and not subject:
            return None
        if op not in ("add", "show", "remove", "clear", "cancel", "done", "search", "summary", "names"):
            op = "add"
        if kind == "recall":
            op = "search"
        if kind == "today":
            op = "summary"
        cmd = Command(kind, op, text=subject, list_name=list_name, when=w, source="model",
                      confirm=op in ("clear",) or kind == "forget", debug={"seconds": round(time.time() - t0, 1), "raw": raw[:200]})
        log.info("extractor %.1fs %r -> %s/%s", time.time() - t0, text[:60], cmd.kind, cmd.op)
        return cmd


# --------------------------------------------------------------------------- #
# Notifications
# --------------------------------------------------------------------------- #

def toast(title: str, body: str) -> bool:
    """Windows toast via WinRT (no shell). Best effort."""
    try:
        from windows_toasts import Toast, WindowsToaster  # type: ignore

        t = Toast()
        t.text_fields = [title, body]
        WindowsToaster("Jarvis").show_toast(t)
        return True
    except Exception as exc:
        log.warning("toast failed: %s", exc)
        return False


# --------------------------------------------------------------------------- #
# Brain: executes Commands, speaks Italian
# --------------------------------------------------------------------------- #

@dataclass
class Outcome:
    handled: bool
    reply: str
    kind: str = "none"
    op: str = "none"
    item: dict[str, Any] | None = None
    items: list[dict[str, Any]] = field(default_factory=list)
    needs_confirm: bool = False
    source: str = "rules"
    executed: bool = False


class Brain:
    def __init__(self, bus: EventBus, store: Store | None = None, extractor: LocalExtractor | None = None):
        self.bus = bus
        self.store = store or Store(embedder=Embedder())
        self.extractor = extractor if extractor is not None else LocalExtractor()
        self._pending: tuple[Command, float] | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_event: Any = None

    # ---------------------------------------------------------------- loop --
    weather_city: str | None = None

    def start(self) -> None:
        if self._thread:
            return
        # Reminders that came due while the bridge was off: no burst of toasts, they are
        # marked as fired and show up as "in sospeso" in the briefing and the HUD.
        try:
            missed = self.store.due_now()
            for item in missed:
                self.store.mark(item["id"], fired=1)
            if missed:
                log.info("%d reminder(s) came due while offline: kept as overdue", len(missed))
        except Exception as exc:
            log.error("missed reminders: %s", exc)
        self._thread = threading.Thread(target=self._loop, name="brain-reminders", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        last_backup_day: str | None = None
        while not self._stop.wait(5):
            try:
                for item in self.store.due_now():
                    self.store.mark(item["id"], fired=1)
                    self.fire(item)
            except Exception as exc:
                log.error("reminder loop: %s", exc)
            day = datetime.now().strftime("%Y-%m-%d")
            if day != last_backup_day:
                last_backup_day = day
                try:
                    self.backup()
                except Exception as exc:
                    log.error("backup: %s", exc)

    def backup(self, keep: int = 14) -> Path | None:
        """Daily copy of the database (SQLite online backup, safe while in use). JARVIS_BACKUP_DIR overrides the folder."""
        target_dir = Path(os.environ.get("JARVIS_BACKUP_DIR") or self.store.path.parent / "backups")
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"brain-{datetime.now():%Y-%m-%d}.db"
        if target.exists():
            return target
        dest = sqlite3.connect(str(target))
        try:
            with self.store._lock:  # noqa: SLF001 - same module
                self.store._db.backup(dest)  # noqa: SLF001
        finally:
            dest.close()
        old = sorted(target_dir.glob("brain-*.db"))[:-keep]
        for p in old:
            p.unlink(missing_ok=True)
        log.info("backup written: %s", target)
        return target

    def fire(self, item: dict[str, Any]) -> None:
        log.info("reminder due: #%s", item["id"])
        self.bus.publish({"type": "reminder", "id": item["id"], "text": item["text"], "due": item["due"], "priority": item.get("priority", 0)})
        toast("Jarvis · promemoria" + (" (urgente)" if item.get("priority") == 2 else ""), item["text"])
        # Recurring: this occurrence stays as "in sospeso" until done; the next one is scheduled now.
        if item.get("repeat") and item.get("due"):
            nxt = next_occurrence(datetime.fromisoformat(item["due"]), item["repeat"])
            if nxt:
                self.store.add("reminder", item["text"], due=nxt, repeat=item["repeat"], priority=int(item.get("priority") or 0))
        if self._on_event:
            try:
                self._on_event("reminder_fired", item)
            except Exception:
                pass

    def on_event(self, fn: Any) -> None:
        self._on_event = fn

    # --------------------------------------------------------------- entry --
    def handle(self, text: str, *, allow_model: bool = True, now: datetime | None = None) -> Outcome:
        now = now or datetime.now()
        # A pending destructive request waits for yes/no.
        if self._pending:
            cmd, t0 = self._pending
            if time.time() - t0 <= CONFIRM_TTL:
                low = _norm(text).lower()
                if re.fullmatch(r"(?:s[iì]|sì|certo|ok|okay|vai|procedi|conferma|confermo|esatto|fallo|yes|sure)[.!]*", low):
                    self._pending = None
                    return self._run(cmd, now, confirmed=True)
                if re.fullmatch(r"(?:no|non|annulla|lascia\s+stare|fermo|niente|cancel|nope)[.!]*", low):
                    self._pending = None
                    return Outcome(True, "Ok, lascio tutto com'è.", cmd.kind, "cancelled", executed=False)
            self._pending = None

        cmd = understand(text, now)
        if cmd.kind == "none" and allow_model and BRAIN_HINT.search(text) and self.extractor is not None:
            ex = self.extractor.extract(text, now)
            if ex is not None and ex.kind != "none":
                cmd = ex
        if cmd.kind == "none":
            return Outcome(False, "", source=cmd.source)
        if cmd.confirm:
            self._pending = (cmd, time.time())
            return Outcome(True, self._confirm_question(cmd), cmd.kind, cmd.op, needs_confirm=True, source=cmd.source)
        return self._run(cmd, now)

    def _confirm_question(self, cmd: Command) -> str:
        if cmd.kind == "list" and cmd.op == "clear":
            n = len(self.store.list("list", list_name=cmd.list_name))
            return f"Svuoto la lista {cmd.list_name} ({n} element{'o' if n == 1 else 'i'})? Dimmi sì o no."
        if cmd.kind == "forget":
            hits = self.store.search(cmd.text, kinds=("fact", "note"), limit=3)
            if not hits:
                self._pending = None
                return f"Non ho niente in memoria su «{cmd.text}»."
            return "Dimentico " + ("questo" if len(hits) == 1 else f"questi {len(hits)} ricordi") + ": " + "; ".join(f"«{h['text']}»" for h in hits) + ". Confermi?"
        return "Confermi?"

    # ----------------------------------------------------------------- run --
    def _run(self, cmd: Command, now: datetime, *, confirmed: bool = False) -> Outcome:
        k, op = cmd.kind, cmd.op
        if k == "today" and op == "evening":
            return self.summary(now, mode="evening")
        if k == "today" and op == "first":
            return self.first(now)
        if k == "today":
            return self.summary(now, weather=True)
        if k == "reminder" and op == "snooze":
            target = None
            if cmd.text:
                target = _best_match(cmd.text, self.store.list("reminder", limit=50))
            target = target or self.store.last_fired()
            if target is None:
                return Outcome(True, "Non c'è nessun promemoria da rimandare.", k, op, source=cmd.source)
            until = cmd.when.at if cmd.when else now + timedelta(minutes=30)
            item = self.store.snooze(target["id"], until)
            return Outcome(True, f"Ok, {target['text']}: te lo ricordo {W.describe(until, now)}.", k, op, item=item, source=cmd.source, executed=True)
        if k == "note":
            return self._note(cmd, now)
        if k == "fact":
            item = self.store.add("fact", cmd.text)
            return Outcome(True, f"Me lo ricorderò: {cmd.text}.", k, op, item=item, source=cmd.source, executed=True)
        if k == "recall":
            return self._recall(cmd)
        if k == "forget":
            hits = self.store.search(cmd.text, kinds=("fact", "note"), limit=3)
            for h in hits:
                self.store.mark(h["id"], deleted=1)
            return Outcome(True, "Dimenticato." if hits else f"Non avevo niente su «{cmd.text}».", k, op, items=hits, source=cmd.source, executed=bool(hits))
        if k == "reminder":
            return self._reminder(cmd, now)
        if k == "list":
            return self._list(cmd, confirmed)
        return Outcome(False, "")

    # --- notes
    def _note(self, cmd: Command, now: datetime) -> Outcome:
        if cmd.op == "add":
            item = self.store.add("note", cmd.text)
            return Outcome(True, f"Annotato: {cmd.text}.", "note", "add", item=item, source=cmd.source, executed=True)
        if cmd.text == "recent" or not cmd.text:
            since = now - timedelta(days=1) if cmd.text == "recent" else None
            notes = self.store.list("note", since=since, limit=8)
            if not notes:
                return Outcome(True, "Nessuna nota" + (" recente." if since else "."), "note", "show", source=cmd.source)
            lines = "; ".join(n["text"] for n in notes)
            head = f"{len(notes)} not{'a' if len(notes) == 1 else 'e'}" + (" recenti" if since else "")
            return Outcome(True, f"{head}: {lines}.", "note", "show", items=notes, source=cmd.source)
        hits = self.store.search(cmd.text, kinds=("note", "fact"), limit=6)
        if not hits:
            return Outcome(True, f"Non ho appunti su {cmd.text}.", "note", "show", source=cmd.source)
        return Outcome(True, f"Su {cmd.text} ho: " + "; ".join(h["text"] for h in hits) + ".", "note", "show", items=hits, source=cmd.source)

    # --- recall
    def _recall(self, cmd: Command) -> Outcome:
        hits = self.store.search(cmd.text, kinds=("fact", "note", "reminder"), limit=5)
        if not hits:
            return Outcome(True, f"Non ho niente in memoria su {cmd.text}.", "recall", "search", source=cmd.source)
        return Outcome(True, "Sì: " + "; ".join(h["text"] for h in hits) + ".", "recall", "search", items=hits, source=cmd.source)

    # --- reminders
    def _reminder(self, cmd: Command, now: datetime) -> Outcome:
        if cmd.op == "add":
            if not cmd.text:
                return Outcome(True, "Cosa devo ricordarti?", "reminder", "add", source=cmd.source)
            due = cmd.when.at if cmd.when else None
            item = self.store.add("reminder", cmd.text, due=due, repeat=cmd.repeat, priority=cmd.priority)
            tag = " (urgente)" if cmd.priority == 2 else " (importante)" if cmd.priority == 1 else ""
            if due is None:
                reply = f"Segnato tra le cose da fare{tag}: {cmd.text}. Se vuoi un orario, dimmelo."
            elif cmd.repeat:
                reply = f"Ok, {describe_repeat(cmd.repeat)} alle {due.hour}:{due.minute:02d} ti ricordo {cmd.text}{tag}. La prima volta {W.describe(due, now)}."
            else:
                reply = f"Ok, ti ricordo {cmd.text}{tag} {W.describe(due, now)}."
                if cmd.when and not cmd.when.explicit_time:
                    reply += f" Ho messo le {due.hour}:{due.minute:02d}, se preferisci un altro orario dimmelo."
            return Outcome(True, reply, "reminder", "add", item=item, source=cmd.source, executed=True)
        if cmd.op == "show":
            scope = cmd.text
            if scope in ("", "oggi", "stasera"):
                until = now.replace(hour=23, minute=59)
                items = [r for r in self.store.upcoming(until=until)] + [r for r in self.store.list("reminder") if r["due"] is None]
                label = "oggi"
            elif scope == "domani":
                s = (now + timedelta(days=1)).replace(hour=0, minute=0)
                items = [r for r in self.store.upcoming(until=s + timedelta(days=1)) if r["due"] and r["due"] >= s.isoformat(timespec="minutes")]
                label = "domani"
            elif "settimana" in scope:
                items = self.store.upcoming(until=now + timedelta(days=7))
                label = "questa settimana"
            else:
                items = self.store.list("reminder", limit=20)
                label = "in tutto"
            if not items:
                return Outcome(True, f"Niente in programma {label}." if label != "in tutto" else "Nessun promemoria attivo.", "reminder", "show", source=cmd.source)
            parts = []
            for r in items:
                due = datetime.fromisoformat(r["due"]) if r["due"] else None
                tag = " (scaduto)" if r["fired"] else ""
                parts.append(f"{r['text']} {W.describe(due, now) if due else 'senza orario'}{tag}")
            return Outcome(True, f"{len(items)} promemoria {label}: " + "; ".join(parts) + ".", "reminder", "show", items=items, source=cmd.source)
        if cmd.op in ("cancel", "done"):
            active = self.store.list("reminder", limit=50)
            target = _best_match(cmd.text, active) if cmd.text else (active[0] if len(active) == 1 else None)
            if target is None:
                if not active:
                    return Outcome(True, "Non ci sono promemoria attivi.", "reminder", cmd.op, source=cmd.source)
                return Outcome(True, "Quale promemoria? " + "; ".join(r["text"] for r in active[:5]) + ".", "reminder", cmd.op, items=active[:5], source=cmd.source)
            self.store.mark(target["id"], done=1) if cmd.op == "done" else self.store.mark(target["id"], deleted=1)
            return Outcome(True, ("Fatto, tolgo " if cmd.op == "done" else "Annullato il promemoria ") + f"{target['text']}.", "reminder", cmd.op, item=target, source=cmd.source, executed=True)
        return Outcome(False, "")

    # --- lists
    def _list(self, cmd: Command, confirmed: bool) -> Outcome:
        name = cmd.list_name or "spesa"
        if cmd.op == "names":
            names = self.store.list_names()
            if not names:
                return Outcome(True, "Non hai ancora liste. Dimmi ad esempio: aggiungi il latte alla spesa.", "list", "names", source=cmd.source)
            return Outcome(True, "Le tue liste: " + ", ".join(f"{n} ({c})" for n, c in names) + ".", "list", "names", source=cmd.source)
        if cmd.op == "add":
            items = _items(cmd.text)
            if not items:
                return Outcome(True, f"Cosa aggiungo alla lista {name}?", "list", "add", source=cmd.source)
            existing = {i["text"].lower() for i in self.store.list("list", list_name=name)}
            added = [self.store.add("list", it, list_name=name) for it in items if it.lower() not in existing]
            dup = [it for it in items if it.lower() in existing]
            reply = (f"Aggiunto {', '.join(a['text'] for a in added)} alla lista {name}." if added else "") + (f" {', '.join(dup)} già {'c' if len(dup) == 1 else 'c'}'era." if dup else "")
            return Outcome(True, reply.strip(), "list", "add", items=added, source=cmd.source, executed=bool(added))
        if cmd.op == "remove":
            current = self.store.list("list", list_name=name)
            removed = []
            for it in _items(cmd.text):
                hit = _best_match(it, current)
                if hit:
                    self.store.mark(hit["id"], done=1)
                    removed.append(hit["text"])
                    current = [c for c in current if c["id"] != hit["id"]]
            if not removed:
                return Outcome(True, f"Non trovo quell'elemento nella lista {name}.", "list", "remove", source=cmd.source)
            return Outcome(True, f"Tolto {', '.join(removed)} dalla lista {name}.", "list", "remove", source=cmd.source, executed=True)
        if cmd.op == "show":
            current = self.store.list("list", list_name=name)
            if not current:
                return Outcome(True, f"La lista {name} è vuota.", "list", "show", source=cmd.source)
            return Outcome(True, f"Lista {name}, {len(current)} element{'o' if len(current) == 1 else 'i'}: " + ", ".join(i["text"] for i in current) + ".", "list", "show", items=current, source=cmd.source)
        if cmd.op == "clear":
            current = self.store.list("list", list_name=name)
            for i in current:
                self.store.mark(i["id"], done=1)
            return Outcome(True, f"Lista {name} svuotata." if current else f"La lista {name} era già vuota.", "list", "clear", source=cmd.source, executed=bool(current))
        return Outcome(False, "")

    # --- summary
    def first(self, now: datetime | None = None) -> Outcome:
        """"Da dove partiamo?": the most pressing item, by priority then by time."""
        now = now or datetime.now()
        cands = self.store.upcoming(until=now.replace(hour=23, minute=59)) + [r for r in self.store.list("reminder", limit=50) if r["due"] is None]
        if not cands:
            return Outcome(True, "Niente in lista: giornata libera, o dimmi cosa aggiungere.", "today", "first")
        cands.sort(key=lambda r: (-int(r.get("priority") or 0), 0 if r["fired"] else 1, r["due"] or "9999"))
        top = cands[0]
        why = "è urgente" if top.get("priority") == 2 else "è importante" if top.get("priority") == 1 else "è in sospeso" if top["fired"] else "è il primo in ordine di tempo"
        when = f", {W.describe(datetime.fromisoformat(top['due']), now)}" if top["due"] else ""
        rest = f" Poi: {'; '.join(r['text'] for r in cands[1:3])}." if len(cands) > 1 else ""
        return Outcome(True, f"Partirei da {top['text']}{when}: {why}.{rest}", "today", "first", items=cands[:3])

    def summary(self, now: datetime | None = None, *, weather: bool = False, mode: str = "morning") -> Outcome:
        """Spoken briefing. `weather` adds today's forecast for `weather_city` (one network call, best effort).
        `mode='evening'`: what is left today and what tomorrow brings."""
        now = now or datetime.now()
        if mode == "evening":
            left = [r for r in self.store.upcoming(until=now.replace(hour=23, minute=59)) if not r["done"]]
            tmr = (now + timedelta(days=1)).replace(hour=0, minute=0)
            tomorrow = [r for r in self.store.upcoming(until=tmr + timedelta(days=1)) if r["due"] and r["due"] >= tmr.isoformat(timespec="minutes")]
            open_tasks = [r for r in self.store.list("reminder", limit=50) if r["due"] is None]
            parts = []
            parts.append("oggi resta in sospeso: " + "; ".join(r["text"] for r in left) if left else "oggi hai chiuso tutto")
            if open_tasks:
                parts.append(f"{len(open_tasks)} cos{'a' if len(open_tasks) == 1 else 'e'} da fare senza data: " + "; ".join(r["text"] for r in open_tasks[:4]))
            parts.append("domani hai " + "; ".join(f"{r['text']} alle {datetime.fromisoformat(r['due']).strftime('%H:%M')}" for r in tomorrow[:5]) if tomorrow else "domani per ora è libero")
            parts.append("buonanotte")
            return Outcome(True, ". ".join(p[0].upper() + p[1:] for p in parts) + ".", "today", "evening", items=left + tomorrow)
        end = now.replace(hour=23, minute=59)
        today = self.store.upcoming(until=end)
        today.sort(key=lambda r: (-int(r.get("priority") or 0), r["due"] or ""))
        overdue = [r for r in today if r["fired"]]
        ahead = [r for r in today if not r["fired"]]
        open_tasks = [r for r in self.store.list("reminder", limit=50) if r["due"] is None]
        notes = self.store.list("note", since=now - timedelta(days=1), limit=5)
        lists = self.store.list_names()
        parts: list[str] = []
        if weather and self.weather_city:
            try:
                from .extras import weather as forecast

                parts.append("meteo " + forecast("", self.weather_city))
            except Exception as exc:
                log.info("briefing weather skipped: %s", exc)
        if ahead:
            parts.append("oggi hai " + "; ".join(f"{r['text']} {W.describe(datetime.fromisoformat(r['due']), now).replace('oggi ', '')}" for r in ahead))
        else:
            parts.append("oggi non hai promemoria con orario")
        if overdue:
            parts.append("in sospeso: " + "; ".join(r["text"] for r in overdue))
        if open_tasks:
            parts.append(f"{len(open_tasks)} cos{'a' if len(open_tasks) == 1 else 'e'} da fare senza orario: " + "; ".join(r["text"] for r in open_tasks[:4]))
        if notes:
            parts.append(f"{len(notes)} not{'a' if len(notes) == 1 else 'e'} recent{'e' if len(notes) == 1 else 'i'}")
        if lists:
            parts.append("liste: " + ", ".join(f"{n} con {c} element{'o' if c == 1 else 'i'}" for n, c in lists[:3]))
        reply = ". ".join(p[0].upper() + p[1:] for p in parts) + "."
        return Outcome(True, reply, "today", "summary", items=today, source="rules")

    def today(self, now: datetime | None = None) -> dict[str, Any]:
        now = now or datetime.now()
        upcoming = self.store.upcoming(until=now + timedelta(days=7), limit=10)
        nxt = next((r for r in upcoming if not r["fired"]), None)
        return {
            "now": now.isoformat(timespec="minutes"),
            "counts": self.store.count(),
            "next": nxt,
            "upcoming": upcoming,
            "overdue": [r for r in upcoming if r["fired"]],
            "lists": [{"name": n, "count": c} for n, c in self.store.list_names()],
            "notes_recent": self.store.list("note", since=now - timedelta(days=1), limit=5),
        }

    def recall(self, query: str, limit: int = 5) -> list[dict[str, Any]]:
        """Memory hits for the chat prompt: facts and notes only, best first."""
        return self.store.search(query, kinds=("fact", "note"), limit=limit)


def _best_match(needle: str, items: list[dict[str, Any]]) -> dict[str, Any] | None:
    n = needle.lower().strip()
    if not n or not items:
        return None
    toks = {t for t in re.findall(r"\w+", n) if len(t) >= 3}
    best, score = None, 0.0
    for it in items:
        txt = it["text"].lower()
        if txt == n:
            return it
        s = 1.0 if n in txt or txt in n else 0.0
        if toks:
            s += sum(1 for t in toks if t in txt) / len(toks)
        if s > score:
            best, score = it, s
    return best if score >= 0.5 else None
