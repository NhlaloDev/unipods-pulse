"""Turn raw files (WhatsApp exports, meeting transcripts, recordings, documents) into records.

Every parser returns a ParsedSource. Chat exports produce timestamped messages; everything else
produces "segments" (a block of text with an optional timestamp label such as 00:12:30).
"""
from __future__ import annotations

import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from . import config

AUDIO_EXT = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".mp4", ".webm", ".mkv", ".mov", ".aac", ".flac"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log"}


@dataclass
class Message:
    ts: datetime
    author: str
    text: str


@dataclass
class Segment:
    text: str
    label: str = ""  # e.g. "00:12:30" or "page 3"
    speaker: str = ""


@dataclass
class ParsedSource:
    name: str
    kind: str  # chat | meeting | doc
    channel: str = ""
    messages: list[Message] = field(default_factory=list)
    segments: list[Segment] = field(default_factory=list)
    date: datetime | None = None  # meeting / document date if known


# ---------------------------------------------------------------- WhatsApp

_INVISIBLE = re.compile(r"[‎‏‪-‮﻿]")
# Android: 24/09/2026, 14:05 - Name: text        iOS: [24/09/2026, 14:05:33] Name: text
_WA_LINE = re.compile(
    r"^\[?(?P<date>\d{1,4}[./-]\d{1,2}[./-]\d{1,4}),?\s+"
    r"(?P<time>\d{1,2}[:.]\d{2}(?:[:.]\d{2})?)(?:\s?(?P<ampm>[AaPp]\.?\s?[Mm]\.?))?\]?"
    r"\s*(?:-|–)?\s*(?P<rest>.*)$"
)
_WA_SKIP = (
    "messages and calls are end-to-end encrypted",
    "<media omitted>",
    "image omitted",
    "video omitted",
    "sticker omitted",
    "audio omitted",
    "gif omitted",
    "document omitted",
    "this message was deleted",
    "you deleted this message",
    "null",
)


def _date_order(dates: list[str]) -> str:
    for d in dates:
        parts = re.split(r"[./-]", d)
        if len(parts[0]) == 4:
            return "ymd"
        a, b = int(parts[0]), int(parts[1])
        if a > 12:
            return "dmy"
        if b > 12:
            return "mdy"
    return config.DEFAULT_DATE_ORDER


def _to_datetime(date: str, time: str, ampm: str | None, order: str) -> datetime | None:
    p = [int(x) for x in re.split(r"[./-]", date)]
    if order == "ymd":
        y, m, d = p
    elif order == "mdy":
        m, d, y = p
    else:
        d, m, y = p
    if y < 100:
        y += 2000
    t = [int(x) for x in re.split(r"[:.]", time)]
    hh, mm, ss = t[0], t[1], (t[2] if len(t) > 2 else 0)
    if ampm:
        pm = ampm.strip().lower().startswith("p")
        if pm and hh < 12:
            hh += 12
        if not pm and hh == 12:
            hh = 0
    try:
        return datetime(y, m, d, hh, mm, ss)
    except ValueError:
        return None


def parse_whatsapp(text: str, name: str) -> ParsedSource:
    text = _INVISIBLE.sub("", text).replace(" ", " ").replace(" ", " ")
    lines = text.splitlines()
    heads = [(_WA_LINE.match(line), line) for line in lines]
    order = _date_order([m.group("date") for m, _ in heads if m][:500])

    channel = re.sub(r"(?i)^whatsapp chat (with|-)\s*", "", Path(name).stem).strip() or "WhatsApp group"
    src = ParsedSource(name=name, kind="chat", channel=channel)
    current: Message | None = None
    for m, line in heads:
        if m:
            ts = _to_datetime(m.group("date"), m.group("time"), m.group("ampm"), order)
            rest = m.group("rest")
            if ts and ": " in rest:
                author, body = rest.split(": ", 1)
                current = Message(ts=ts, author=author.strip(), text=body.strip())
                src.messages.append(current)
                continue
            if ts:  # system line (joined, left, changed subject...)
                current = None
                continue
        if current is not None and line.strip():
            current.text += "\n" + line.strip()

    src.messages = [
        msg for msg in src.messages if msg.text and msg.text.strip().lower() not in _WA_SKIP
    ]
    return src


def looks_like_whatsapp(text: str) -> bool:
    sample = _INVISIBLE.sub("", text[:5000]).splitlines()[:40]
    hits = sum(1 for line in sample if _WA_LINE.match(line) and ": " in line)
    return hits >= 3


# ---------------------------------------------------------------- transcripts

_CUE_TIME = re.compile(r"(\d{1,2}:)?\d{1,2}:\d{2}[.,]\d{3}\s*-->\s*")
_VTT_VOICE = re.compile(r"<v\s+([^>]+)>")
_TAGS = re.compile(r"<[^>]+>")
_SPEAKER_PREFIX = re.compile(r"^([A-Z][\w .'-]{1,40}):\s+(.*)$")


def parse_subtitles(text: str, name: str) -> ParsedSource:
    """WebVTT (Teams / Zoom / Google Meet) and SRT transcripts."""
    src = ParsedSource(name=name, kind="meeting")
    label, rows = "", []
    for block in re.split(r"\n\s*\n", text.replace("\r", "")):
        lines = [line for line in block.split("\n") if line.strip()]
        cue_idx = next((i for i, line in enumerate(lines) if _CUE_TIME.search(line)), None)
        if cue_idx is None:
            continue
        label = lines[cue_idx].split("-->")[0].strip().split(".")[0].split(",")[0]
        body = " ".join(lines[cue_idx + 1:])
        speaker = ""
        v = _VTT_VOICE.search(body)
        if v:
            speaker = v.group(1).strip()
        body = _TAGS.sub("", body).strip()
        sp = _SPEAKER_PREFIX.match(body)
        if not speaker and sp:
            speaker, body = sp.group(1), sp.group(2)
        if body:
            rows.append(Segment(text=body, label=label, speaker=speaker))

    # Merge consecutive lines from the same speaker so the transcript reads naturally.
    for row in rows:
        last = src.segments[-1] if src.segments else None
        if last and last.speaker == row.speaker and len(last.text) < 800:
            last.text += " " + row.text
        else:
            src.segments.append(row)
    return src


def parse_plain(text: str, name: str, kind: str) -> ParsedSource:
    src = ParsedSource(name=name, kind=kind)
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for p in paras or [text]:
        sp = _SPEAKER_PREFIX.match(p)
        if sp and kind == "meeting":
            src.segments.append(Segment(text=sp.group(2), speaker=sp.group(1)))
        else:
            src.segments.append(Segment(text=p))
    return src


# ---------------------------------------------------------------- documents & audio

def parse_pdf(data: bytes, name: str) -> ParsedSource:
    from pypdf import PdfReader

    src = ParsedSource(name=name, kind="doc")
    for i, page in enumerate(PdfReader(io.BytesIO(data)).pages, start=1):
        txt = (page.extract_text() or "").strip()
        if txt:
            src.segments.append(Segment(text=txt, label=f"page {i}"))
    return src


def parse_docx(data: bytes, name: str, kind: str) -> ParsedSource:
    import docx

    d = docx.Document(io.BytesIO(data))
    text = "\n\n".join(p.text for p in d.paragraphs if p.text.strip())
    return parse_plain(text, name, kind)


def transcribe_audio(path: Path, name: str) -> ParsedSource:
    try:
        from faster_whisper import WhisperModel
    except ImportError as e:  # pragma: no cover - optional dependency
        raise ValueError(
            "Audio/video transcription needs the optional 'faster-whisper' package: "
            "pip install faster-whisper (or upload the Teams/Zoom transcript file instead)."
        ) from e
    model = WhisperModel(config.WHISPER_MODEL, device="auto", compute_type="int8")
    segments, _ = model.transcribe(str(path), vad_filter=True)
    src = ParsedSource(name=name, kind="meeting")
    for s in segments:
        h, rem = divmod(int(s.start), 3600)
        m, sec = divmod(rem, 60)
        src.segments.append(Segment(text=s.text.strip(), label=f"{h:02d}:{m:02d}:{sec:02d}"))
    return src


# ---------------------------------------------------------------- dispatcher

def _guess_meeting(name: str, text: str) -> bool:
    n = name.lower()
    return any(k in n for k in ("meeting", "call", "transcript", "session", "zoom", "teams", "meet")) or bool(
        _CUE_TIME.search(text[:3000])
    )


def _date_from_name(name: str) -> datetime | None:
    m = re.search(r"(20\d{2})[-_. ]?(\d{2})[-_. ]?(\d{2})", name)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
    return None


def parse_file(path: Path, name: str, kind_hint: str = "") -> list[ParsedSource]:
    """Parse one uploaded file. Zip files (e.g. WhatsApp 'Export chat' with media) are unpacked."""
    ext = Path(name).suffix.lower()
    data = path.read_bytes()

    if ext == ".zip":
        out: list[ParsedSource] = []
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            for info in z.infolist():
                inner = Path(info.filename).name
                if Path(inner).suffix.lower() in TEXT_EXT | {".vtt", ".srt", ".pdf", ".docx"}:
                    tmp = config.UPLOAD_DIR / f"_zip_{inner}"
                    tmp.write_bytes(z.read(info))
                    # WhatsApp zips contain "_chat.txt"; name the source after the zip instead.
                    label = name if inner == "_chat.txt" else inner
                    out += parse_file(tmp, label if label.endswith(".txt") else inner, kind_hint)
                    tmp.unlink(missing_ok=True)
        return out

    if ext in AUDIO_EXT:
        src = transcribe_audio(path, name)
    elif ext == ".pdf":
        src = parse_pdf(data, name)
        if kind_hint:
            src.kind = kind_hint
    elif ext == ".docx":
        text_kind = kind_hint or ("meeting" if _guess_meeting(name, "") else "doc")
        src = parse_docx(data, name, text_kind)
    else:
        text = data.decode("utf-8-sig", errors="replace")
        if ext in (".vtt", ".srt"):
            src = parse_subtitles(text, name)
        elif kind_hint == "chat" or (kind_hint in ("", "chat") and looks_like_whatsapp(text)):
            src = parse_whatsapp(text, name)
        else:
            kind = kind_hint or ("meeting" if _guess_meeting(name, text) else "doc")
            src = parse_plain(text, name, kind)

    src.date = src.date or _date_from_name(name)
    return [src]
