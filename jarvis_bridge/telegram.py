"""Telegram bot: reminders on the phone, and the second brain from anywhere.

Long polling from the bridge (no webhook, no public URL, nothing installed).
Inert until ``TELEGRAM_BOT_TOKEN`` is in ``.env``.

Setup (one-off):
1. On Telegram talk to @BotFather → /newbot → copy the token into .env as TELEGRAM_BOT_TOKEN.
2. Restart the bridge, open your bot and send /start: the bridge logs your chat id
   and, when TELEGRAM_CHAT_ID is empty, adopts the first chat that says /start.
   Put that id in .env as TELEGRAM_CHAT_ID to lock it (only that chat is ever answered).

Only text is read; the bot answers with the same brain the UI uses. Desktop
actions are deliberately *not* reachable from Telegram.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.parse
import urllib.request
from typing import Any, Callable

log = logging.getLogger("jarvis.telegram")


class Telegram:
    def __init__(self, handle: Callable[[str], Any]):
        self.handle = handle  # text -> brain Outcome
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._offset = 0
        self.last_error: str | None = None
        self.adopted_chat: str | None = None

    @property
    def token(self) -> str | None:
        return os.environ.get("TELEGRAM_BOT_TOKEN") or None

    @property
    def chat_id(self) -> str | None:
        return os.environ.get("TELEGRAM_CHAT_ID") or self.adopted_chat

    @property
    def configured(self) -> bool:
        return bool(self.token)

    def status(self) -> dict[str, Any]:
        return {
            "configured": self.configured, "running": bool(self._thread and self._thread.is_alive()),
            "chat_id": self.chat_id, "error": self.last_error,
            "setup": None if self.configured else "TELEGRAM_BOT_TOKEN mancante in jarvis-bridge/.env (vedi telegram.py)",
        }

    # ------------------------------------------------------------------ api
    def _call(self, method: str, **params: Any) -> Any:
        if not self.token:
            raise RuntimeError("telegram not configured")
        data = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None}).encode()
        req = urllib.request.Request(f"https://api.telegram.org/bot{self.token}/{method}", data=data)
        with urllib.request.urlopen(req, timeout=40) as r:
            out = json.load(r)
        if not out.get("ok"):
            raise RuntimeError(str(out.get("description")))
        return out.get("result")

    def send(self, text: str) -> bool:
        """Message to the owner's chat. Best effort, never raises."""
        if not self.configured or not self.chat_id:
            return False
        try:
            self._call("sendMessage", chat_id=self.chat_id, text=text[:4000])
            return True
        except Exception as exc:
            self.last_error = str(exc)[:200]
            log.warning("telegram send failed: %s", exc)
            return False

    # ----------------------------------------------------------------- loop
    def start(self) -> None:
        if not self.configured or self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="telegram", daemon=True)
        self._thread.start()
        log.info("telegram bot polling started")

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                updates = self._call("getUpdates", offset=self._offset, timeout=25, allowed_updates=json.dumps(["message"]))
                for u in updates or []:
                    self._offset = int(u["update_id"]) + 1
                    msg = u.get("message") or {}
                    chat = str(msg.get("chat", {}).get("id", ""))
                    text = (msg.get("text") or "").strip()
                    if not chat or not text:
                        continue
                    self._on_message(chat, text)
                self.last_error = None
            except Exception as exc:
                self.last_error = str(exc)[:200]
                log.warning("telegram poll: %s", exc)
                time.sleep(5)

    def _on_message(self, chat: str, text: str) -> None:
        if text.startswith("/start"):
            if not self.chat_id:
                self.adopted_chat = chat
                log.info("telegram: adopted chat %s (set TELEGRAM_CHAT_ID=%s in .env to lock it)", chat, chat)
            if chat == self.chat_id:
                self._call("sendMessage", chat_id=chat, text=f"Ciao, sono Jarvis. Questo chat id è {chat}. Scrivimi appunti, promemoria e liste come faresti a voce.")
            return
        if chat != self.chat_id:
            log.warning("telegram: ignored message from unknown chat %s", chat)
            return
        try:
            out = self.handle(text)
            reply = out.reply if getattr(out, "handled", False) else "Da qui gestisco appunti, promemoria, liste e ricordi. Esempio: «ricordami domani alle 9 di chiamare Marco»."
        except Exception as exc:
            reply = f"Errore: {str(exc)[:100]}"
        try:
            self._call("sendMessage", chat_id=chat, text=reply[:4000])
        except Exception as exc:
            self.last_error = str(exc)[:200]
