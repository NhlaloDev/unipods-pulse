"""SQLite knowledge base: sources, chat messages and searchable chunks (FTS5 / BM25)."""
from __future__ import annotations

import hashlib
import re
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from . import config
from .parsers import Message, ParsedSource

_lock = threading.RLock()
CHUNK_CHARS = 3000

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    kind TEXT NOT NULL,              -- chat | meeting | doc
    platform TEXT DEFAULT '',        -- whatsapp | telegram | upload
    channel TEXT DEFAULT '',
    doc_date TEXT,
    summary TEXT,
    status TEXT DEFAULT 'ready',
    created_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    ts TEXT NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL,
    dedup TEXT UNIQUE
);
CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
CREATE TABLE IF NOT EXISTS chunks (
    id INTEGER PRIMARY KEY,
    source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    day TEXT,
    label TEXT,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_source_day ON chunks(source_id, day);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(text, tokenize='porter unicode61');
"""


@contextmanager
def db():
    with _lock:
        con = sqlite3.connect(config.DB_PATH)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        try:
            yield con
            con.commit()
        finally:
            con.close()


def init() -> None:
    with db() as con:
        con.executescript(SCHEMA)


# ---------------------------------------------------------------- writing

def _insert_chunk(con, source_id: int, day: str | None, label: str, text: str) -> None:
    cur = con.execute(
        "INSERT INTO chunks(source_id, day, label, text) VALUES (?,?,?,?)", (source_id, day, label, text)
    )
    con.execute("INSERT INTO chunks_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, text))


def _delete_chunks(con, where: str, args: tuple) -> None:
    ids = [r[0] for r in con.execute(f"SELECT id FROM chunks WHERE {where}", args)]
    for i in ids:
        con.execute("DELETE FROM chunks_fts WHERE rowid = ?", (i,))
    con.execute(f"DELETE FROM chunks WHERE {where}", args)


def _chat_source(con, platform: str, channel: str) -> int:
    row = con.execute(
        "SELECT id FROM sources WHERE kind='chat' AND platform=? AND channel=?", (platform, channel)
    ).fetchone()
    if row:
        return row[0]
    label = {"whatsapp": "WhatsApp", "telegram": "Telegram"}.get(platform, platform.title())
    return con.execute(
        "INSERT INTO sources(name, kind, platform, channel) VALUES (?,?,?,?)",
        (f"{label}: {channel}", "chat", platform, channel),
    ).lastrowid


def _rebuild_chat_day(con, source_id: int, day: str) -> None:
    """Chat chunks are one (or more) per group per day, rebuilt whenever that day changes."""
    src = con.execute("SELECT name FROM sources WHERE id=?", (source_id,)).fetchone()
    rows = con.execute(
        "SELECT ts, author, text FROM messages WHERE source_id=? AND substr(ts,1,10)=? ORDER BY ts, id",
        (source_id, day),
    ).fetchall()
    _delete_chunks(con, "source_id=? AND day=?", (source_id, day))
    header = f"[{src['name']} · {day}]"
    buf: list[str] = []
    for r in rows:
        line = f"{r['ts'][11:16]} {r['author']}: {r['text']}"
        if buf and sum(len(b) for b in buf) + len(line) > CHUNK_CHARS:
            _insert_chunk(con, source_id, day, day, header + "\n" + "\n".join(buf))
            buf = []
        buf.append(line)
    if buf:
        _insert_chunk(con, source_id, day, day, header + "\n" + "\n".join(buf))


def add_messages(platform: str, channel: str, messages: list[Message]) -> tuple[int, int]:
    """Add chat messages, skipping ones we already have (exports overlap). Returns (source_id, added)."""
    with db() as con:
        sid = _chat_source(con, platform, channel)
        days, added = set(), 0
        for m in messages:
            ts = m.ts.strftime("%Y-%m-%d %H:%M:%S")
            # Exports only have minute precision, so dedupe on the minute.
            key = hashlib.sha1(f"{sid}|{ts[:16]}|{m.author}|{m.text.strip()}".encode()).hexdigest()
            cur = con.execute(
                "INSERT OR IGNORE INTO messages(source_id, ts, author, text, dedup) VALUES (?,?,?,?,?)",
                (sid, ts, m.author, m.text.strip(), key),
            )
            if cur.rowcount:
                added += 1
                days.add(ts[:10])
        for day in sorted(days):
            _rebuild_chat_day(con, sid, day)
        return sid, added


def add_document(src: ParsedSource) -> int:
    """Meetings and documents: chunk segments in order, keeping timestamps / speakers."""
    with db() as con:
        day = src.date.strftime("%Y-%m-%d") if src.date else None
        sid = con.execute(
            "INSERT INTO sources(name, kind, platform, doc_date, status) VALUES (?,?,?,?,?)",
            (src.name, src.kind, "upload", day, "summarising" if src.kind == "meeting" else "ready"),
        ).lastrowid
        kind_label = "Meeting" if src.kind == "meeting" else "Document"
        header = f"[{kind_label}: {src.name}{' · ' + day if day else ''}]"
        buf: list[str] = []
        start = ""
        for seg in src.segments:
            line = seg.text
            if seg.speaker:
                line = f"{seg.speaker}: {line}"
            if seg.label:
                line = f"({seg.label}) {line}"
            if buf and sum(len(b) for b in buf) + len(line) > CHUNK_CHARS:
                _insert_chunk(con, sid, day, start, header + "\n" + "\n".join(buf))
                buf = []
            if not buf:
                start = seg.label
            buf.append(line)
        if buf:
            _insert_chunk(con, sid, day, start, header + "\n" + "\n".join(buf))
        return sid


def set_summary(source_id: int, summary: str, status: str = "ready") -> None:
    with db() as con:
        src = con.execute("SELECT name, doc_date FROM sources WHERE id=?", (source_id,)).fetchone()
        if not src:
            return
        con.execute("UPDATE sources SET summary=?, status=? WHERE id=?", (summary, status, source_id))
        _delete_chunks(con, "source_id=? AND label='summary'", (source_id,))
        if summary:
            text = f"[Meeting summary: {src['name']}{' · ' + src['doc_date'] if src['doc_date'] else ''}]\n{summary}"
            _insert_chunk(con, source_id, src["doc_date"], "summary", text)


def set_status(source_id: int, status: str) -> None:
    with db() as con:
        con.execute("UPDATE sources SET status=? WHERE id=?", (status, source_id))


def delete_source(source_id: int) -> None:
    with db() as con:
        _delete_chunks(con, "source_id=?", (source_id,))
        con.execute("DELETE FROM sources WHERE id=?", (source_id,))


# ---------------------------------------------------------------- reading

def document_text(source_id: int) -> str:
    with db() as con:
        rows = con.execute(
            "SELECT text FROM chunks WHERE source_id=? AND label != 'summary' ORDER BY id", (source_id,)
        ).fetchall()
    return "\n".join(r[0] for r in rows)


def corpus_chars() -> int:
    with db() as con:
        return con.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks").fetchone()[0]


def all_chunks() -> list[str]:
    """Whole knowledge base in a stable order (documents first, then by date) for prompt caching."""
    with db() as con:
        rows = con.execute(
            "SELECT text FROM chunks ORDER BY COALESCE(day,'0000'), source_id, id"
        ).fetchall()
    return [r[0] for r in rows]


_STOP = set(
    "a an and are as at be but by can did do does for from has have how i if in is it its me my of on or "
    "our so that the their them there these they this to was we were what when where which who why will "
    "with you your about any anyone someone please tell know could would should been being just also get".split()
)


def search(query: str, k: int) -> list[str]:
    words = [w for w in re.findall(r"[\w']+", query.lower()) if w not in _STOP and len(w) > 1]
    if not words:
        return []
    fts = " OR ".join(f'"{w}"' for w in dict.fromkeys(words))
    with db() as con:
        rows = con.execute(
            "SELECT c.text, c.day FROM chunks_fts f JOIN chunks c ON c.id = f.rowid "
            "WHERE chunks_fts MATCH ? ORDER BY bm25(chunks_fts) LIMIT ?",
            (fts, k),
        ).fetchall()
    # Present hits chronologically so Claude can see how answers evolved over time.
    return [r[0] for r in sorted(rows, key=lambda r: r[1] or "")]


def recent_chunks(days: int = 3, limit: int = 15) -> list[str]:
    with db() as con:
        rows = con.execute(
            "SELECT text FROM chunks WHERE day >= date('now', ?) ORDER BY day DESC, id DESC LIMIT ?",
            (f"-{days} days", limit),
        ).fetchall()
    return [r[0] for r in reversed(rows)]


def messages_since(since: datetime, until: datetime | None = None) -> list[sqlite3.Row]:
    until = until or datetime(9999, 1, 1)
    with db() as con:
        return con.execute(
            "SELECT m.ts, m.author, m.text, s.name AS source FROM messages m JOIN sources s ON s.id=m.source_id "
            "WHERE m.ts >= ? AND m.ts < ? ORDER BY m.ts",
            (since.strftime("%Y-%m-%d %H:%M:%S"), until.strftime("%Y-%m-%d %H:%M:%S")),
        ).fetchall()


def meetings_since(since: datetime) -> list[sqlite3.Row]:
    with db() as con:
        return con.execute(
            "SELECT id, name, doc_date, summary, created_at FROM sources WHERE kind='meeting' "
            "AND COALESCE(doc_date, substr(created_at,1,10)) >= ? ORDER BY COALESCE(doc_date, created_at)",
            (since.strftime("%Y-%m-%d"),),
        ).fetchall()


def list_sources() -> list[dict]:
    with db() as con:
        rows = con.execute(
            "SELECT s.id, s.name, s.kind, s.platform, s.doc_date, s.status, s.created_at, "
            "(SELECT COUNT(*) FROM messages m WHERE m.source_id=s.id) AS n_messages, "
            "(SELECT MAX(ts) FROM messages m WHERE m.source_id=s.id) AS last_message, "
            "(SELECT COUNT(*) FROM chunks c WHERE c.source_id=s.id) AS n_chunks, "
            "s.summary IS NOT NULL AND s.summary != '' AS has_summary "
            "FROM sources s ORDER BY s.created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_source(source_id: int) -> dict | None:
    with db() as con:
        row = con.execute("SELECT * FROM sources WHERE id=?", (source_id,)).fetchone()
    return dict(row) if row else None


def stats() -> dict:
    with db() as con:
        one = lambda q: con.execute(q).fetchone()[0]  # noqa: E731
        return {
            "messages": one("SELECT COUNT(*) FROM messages"),
            "meetings": one("SELECT COUNT(*) FROM sources WHERE kind='meeting'"),
            "documents": one("SELECT COUNT(*) FROM sources WHERE kind='doc'"),
            "chats": one("SELECT COUNT(*) FROM sources WHERE kind='chat'"),
            "first": one("SELECT MIN(ts) FROM messages"),
            "last": one("SELECT MAX(ts) FROM messages"),
            "chars": one("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks"),
        }
