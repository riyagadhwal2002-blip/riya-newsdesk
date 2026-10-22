"""Telegram bot: previews, alerts, and the remote pause switch. Free; optional but strongly recommended."""
from __future__ import annotations

from pathlib import Path

import requests

from .log import log

HELP = ("Commands:\n/pause – stop publishing now\n/resume – allow publishing again\n"
        "/status – what the bot is doing\n/live – switch to live publishing\n/dryrun – previews only\n/help")


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None, session=None):
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.http = session or requests.Session()

    @property
    def enabled(self) -> bool:
        return bool(self.token and self.chat_id)

    def _api(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.token}/{method}"

    def send(self, text: str) -> None:
        if not self.enabled:
            log.info("[telegram disabled] %s", text[:200])
            return
        try:
            self.http.post(self._api("sendMessage"), data={"chat_id": self.chat_id, "text": text[:4000],
                                                           "disable_web_page_preview": "true"}, timeout=20)
        except requests.RequestException as exc:
            log.warning("telegram send failed: %s", type(exc).__name__)

    def send_photo(self, path: Path, caption: str) -> None:
        if not self.enabled:
            log.info("[telegram disabled] photo %s", path)
            return
        try:
            with open(path, "rb") as fh:
                self.http.post(self._api("sendPhoto"), data={"chat_id": self.chat_id, "caption": caption[:1000]},
                               files={"photo": fh}, timeout=60)
        except (requests.RequestException, OSError) as exc:
            log.warning("telegram photo failed: %s", type(exc).__name__)

    def commands(self, offset: int) -> tuple[list[str], int]:
        """Returns commands sent from YOUR chat only, plus the next offset."""
        if not self.enabled:
            return [], offset
        try:
            r = self.http.get(self._api("getUpdates"), params={"offset": offset, "timeout": 0}, timeout=20)
            updates = r.json().get("result", [])
        except (requests.RequestException, ValueError):
            return [], offset
        cmds = []
        for u in updates:
            offset = max(offset, u["update_id"] + 1)
            msg = u.get("message") or {}
            if str(msg.get("chat", {}).get("id")) != self.chat_id:
                continue  # ignore strangers
            text = (msg.get("text") or "").strip().lower()
            if text.startswith("/"):
                cmds.append(text.split()[0].split("@")[0])
        return cmds, offset
