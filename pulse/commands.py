"""One command router shared by every channel (web, WhatsApp, Telegram)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import brain, config, store

HELP = f"""Hi, I'm *{config.BOT_NAME}* 👋 I've read the group chats, meeting transcripts and shared docs, so you don't have to.

- Just ask a question, e.g. _When is the pitch deadline?_
- *catch me up*: summary of the last 7 days
- *catch me up 3 days* / *catch me up since 2026-09-18* / *catch me up today*
- *last meeting*: recap of the most recent meeting
- *meetings*: list of meetings I know about
- *reset*: forget our conversation so far"""

_CATCHUP = re.compile(r"^[!/]?(catch ?(me )?up|catchup|digest|what did i miss|summary)\b(.*)$", re.I)


def parse_since(arg: str) -> datetime:
    arg = arg.strip().lower()
    now = datetime.now()
    if not arg:
        return now - timedelta(days=7)
    if "today" in arg:
        return now.replace(hour=0, minute=0, second=0, microsecond=0)
    if "yesterday" in arg:
        return (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if "week" in arg:
        return now - timedelta(days=7)
    if "month" in arg:
        return now - timedelta(days=30)
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", arg)
    if m:
        return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.search(r"(\d+)\s*(h|hour|hours|d|day|days|w|week|weeks)\b", arg)
    if m:
        n, unit = int(m.group(1)), m.group(2)[0]
        return now - {"h": timedelta(hours=n), "d": timedelta(days=n), "w": timedelta(weeks=n)}[unit]
    return now - timedelta(days=7)


def last_meeting() -> str:
    meetings = store.meetings_since(datetime(2000, 1, 1))
    if not meetings:
        return "I don't have any meeting transcripts or recordings yet."
    m = meetings[-1]
    if not m["summary"]:
        return f"I'm still summarising *{m['name']}*, try again in a minute."
    return f"*{m['name']}* ({m['doc_date'] or m['created_at'][:10]})\n\n{m['summary']}"


def list_meetings() -> str:
    meetings = store.meetings_since(datetime(2000, 1, 1))
    if not meetings:
        return "I don't have any meetings yet."
    rows = [f"- {m['doc_date'] or m['created_at'][:10]}: {m['name']}" for m in meetings[-15:]]
    return "Meetings I know about:\n" + "\n".join(rows) + "\n\nAsk me anything about them."


def handle(text: str, session_id: str, asker: str = "") -> str:
    text = (text or "").strip()
    low = text.lower().lstrip("!/")
    if not text or low in ("help", "start", "menu", "hi", "hello", "hey"):
        return HELP
    if low == "reset":
        brain.reset(session_id)
        return "Done, fresh start. 🧹"
    if low in ("last meeting", "latest meeting", "lastmeeting"):
        return last_meeting()
    if low in ("meetings", "list meetings"):
        return list_meetings()
    m = _CATCHUP.match(text)
    if m:
        return brain.digest(parse_since(m.group(3)))
    return brain.answer(text, session_id=session_id, asker=asker)["answer"]
