"""Optional Telegram connector (long polling, no public URL needed).

Add the bot to a Telegram group and it records every message into the knowledge base (turn off
privacy mode in @BotFather: /setprivacy -> Disable). It replies when mentioned, when someone replies to
it, on /ask /catchup /lastmeeting commands, and to every direct message.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime

import httpx

from . import commands, config, store
from .parsers import Message

log = logging.getLogger("pulse.telegram")
API = "https://api.telegram.org/bot{token}/{method}"


def _api(method: str, **params):
    r = httpx.post(API.format(token=config.TELEGRAM_BOT_TOKEN, method=method), json=params, timeout=70)
    r.raise_for_status()
    return r.json()["result"]


def _send(chat_id: int, text: str, reply_to: int | None = None) -> None:
    for i in range(0, len(text), 4000):  # Telegram message limit is 4096 chars
        _api("sendMessage", chat_id=chat_id, text=text[i:i + 4000], reply_to_message_id=reply_to)


def _handle(update: dict, me: dict) -> None:
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return
    chat, text = msg["chat"], msg["text"]
    user = msg.get("from", {})
    author = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username", "someone")
    is_private = chat["type"] == "private"
    mention = f"@{me['username']}".lower()

    if not is_private:
        store.add_messages("telegram", chat.get("title", "Telegram group"),
                           [Message(ts=datetime.fromtimestamp(msg["date"]), author=author, text=text)])

    replied_to_bot = (msg.get("reply_to_message") or {}).get("from", {}).get("id") == me["id"]
    addressed = is_private or replied_to_bot or mention in text.lower() or text.startswith("/")
    if not addressed:
        return
    query = text.replace(mention, "").replace(f"@{me['username']}", "").strip()
    cmd = query.split()[0].lower() if query.split() else ""
    if cmd.startswith("/"):
        cmd = cmd.split("@")[0]
        rest = query[len(query.split()[0]):].strip()
        query = {"/ask": rest, "/catchup": f"catch me up {rest}", "/lastmeeting": "last meeting",
                 "/meetings": "meetings", "/help": "help", "/start": "help", "/reset": "reset"}.get(cmd, rest or "help")
    session = f"tg:{chat['id']}:{user.get('id')}"
    _api("sendChatAction", chat_id=chat["id"], action="typing")
    try:
        reply = commands.handle(query, session_id=session, asker=author)
    except Exception:
        log.exception("telegram answer failed")
        reply = "Sorry, something went wrong on my side. Please try again shortly."
    _send(chat["id"], reply, reply_to=None if is_private else msg["message_id"])


def _loop() -> None:
    me = _api("getMe")
    log.info("Telegram bot @%s running", me["username"])
    offset = None
    while True:
        try:
            updates = _api("getUpdates", timeout=60, offset=offset, allowed_updates=["message", "edited_message"])
            for u in updates:
                offset = u["update_id"] + 1
                threading.Thread(target=_handle, args=(u, me), daemon=True).start()
        except Exception:
            log.exception("telegram polling error")
            time.sleep(5)


def start() -> None:
    if config.TELEGRAM_BOT_TOKEN:
        threading.Thread(target=_loop, daemon=True, name="telegram").start()
