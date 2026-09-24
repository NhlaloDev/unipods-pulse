"""HTTP server: web chat UI, admin uploads, ingest API for the WhatsApp bridge, Twilio WhatsApp webhook."""
from __future__ import annotations

import logging
import os
import shutil
import threading
import uuid
from datetime import datetime
from pathlib import Path
from xml.sax.saxutils import escape

import httpx
from fastapi import BackgroundTasks, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel

from . import brain, commands, config, parsers, store, telegram
from .parsers import Message

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("pulse")

app = FastAPI(title=f"{config.BOT_NAME} - group memory chatbot")
STATIC = Path(__file__).parent / "static"


@app.on_event("startup")
def _startup() -> None:
    store.init()
    telegram.start()


def require_admin(x_admin_token: str = Header(default="")) -> None:
    if config.ADMIN_TOKEN and x_admin_token != config.ADMIN_TOKEN:
        raise HTTPException(401, "Admin token required")


# ---------------------------------------------------------------- web UI + chat

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/info")
def info():
    return {"bot": config.BOT_NAME, "group": config.GROUP_NAME, "admin_required": bool(config.ADMIN_TOKEN),
            "model": config.MODEL, **store.stats()}


class ChatIn(BaseModel):
    text: str
    session_id: str = "web"
    asker: str = ""


@app.post("/api/chat")
def chat(body: ChatIn):
    """Single entry point for all channels: commands (catch me up, last meeting...) or free questions."""
    try:
        return {"reply": commands.handle(body.text, session_id=body.session_id, asker=body.asker)}
    except Exception as e:
        log.exception("chat failed")
        raise HTTPException(500, f"Something went wrong: {e}") from e


class DigestIn(BaseModel):
    since: str  # YYYY-MM-DD or "3 days"
    until: str | None = None


@app.post("/api/digest")
def digest(body: DigestIn):
    until = datetime.fromisoformat(body.until) if body.until else None
    return {"reply": brain.digest(commands.parse_since(body.since), until)}


# ---------------------------------------------------------------- knowledge base admin

@app.get("/api/sources")
def sources():
    return store.list_sources()


@app.get("/api/sources/{source_id}")
def source(source_id: int):
    src = store.get_source(source_id)
    if not src:
        raise HTTPException(404)
    return src


@app.delete("/api/sources/{source_id}", dependencies=[Depends(require_admin)])
def delete(source_id: int):
    store.delete_source(source_id)
    return {"ok": True}


def ingest_path(path: Path, name: str, kind: str = "") -> list[dict]:
    results = []
    for src in parsers.parse_file(path, name, kind):
        if src.kind == "chat":
            sid, added = store.add_messages("whatsapp", src.channel, src.messages)
            results.append({"name": src.name, "kind": "chat", "source_id": sid,
                            "detail": f"{added} new of {len(src.messages)} messages"})
        elif src.segments:
            sid = store.add_document(src)
            if src.kind == "meeting":
                brain.summarise_meeting_async(sid)
            results.append({"name": src.name, "kind": src.kind, "source_id": sid,
                            "detail": f"{len(src.segments)} sections" + (" · summarising…" if src.kind == "meeting" else "")})
        else:
            results.append({"name": src.name, "kind": src.kind, "error": "No readable text found"})
    return results


def _ingest_audio_job(path: Path, name: str) -> None:
    try:
        ingest_path(path, name, "meeting")
    except Exception:
        log.exception("transcription failed for %s", name)


@app.post("/api/upload", dependencies=[Depends(require_admin)])
async def upload(background: BackgroundTasks, files: list[UploadFile] = File(...), kind: str = Form(default="")):
    """Upload WhatsApp exports (.txt/.zip), transcripts (.vtt/.srt/.txt/.docx), recordings or documents."""
    out = []
    for f in files:
        name = Path(f.filename or "upload").name
        dest = config.UPLOAD_DIR / f"{uuid.uuid4().hex[:8]}_{name}"
        with dest.open("wb") as fh:
            shutil.copyfileobj(f.file, fh)
        try:
            if Path(name).suffix.lower() in parsers.AUDIO_EXT:
                background.add_task(_ingest_audio_job, dest, name)  # transcription is slow; do it in the background
                out.append({"name": name, "kind": "meeting", "detail": "transcribing in the background…"})
            else:
                out += ingest_path(dest, name, kind)
        except ValueError as e:
            out.append({"name": name, "error": str(e)})
        except Exception as e:
            log.exception("ingest failed")
            out.append({"name": name, "error": f"Could not read file: {e}"})
    return {"results": out}


class LiveMessage(BaseModel):
    platform: str = "whatsapp"
    channel: str
    author: str
    text: str
    ts: datetime | None = None


@app.post("/api/ingest/message", dependencies=[Depends(require_admin)])
def ingest_message(m: LiveMessage):
    """Used by the WhatsApp bridge to stream group messages into the knowledge base as they happen."""
    store.add_messages(m.platform, m.channel, [Message(ts=m.ts or datetime.now(), author=m.author, text=m.text)])
    return {"ok": True}


@app.post("/api/meetings/{source_id}/resummarise", dependencies=[Depends(require_admin)])
def resummarise(source_id: int):
    store.set_status(source_id, "summarising")
    brain.summarise_meeting_async(source_id)
    return {"ok": True}


# ---------------------------------------------------------------- WhatsApp via Twilio (official API)

TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN", "")


def _twilio_send(to: str, from_: str, text: str) -> None:
    for i in range(0, len(text), 1500):  # WhatsApp via Twilio caps bodies at 1600 chars
        httpx.post(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            data={"To": to, "From": from_, "Body": text[i:i + 1500]},
            auth=(TWILIO_SID, TWILIO_TOKEN), timeout=30,
        ).raise_for_status()


def _twilio_job(body: str, to: str, from_: str, name: str) -> None:
    try:
        reply = commands.handle(body, session_id=f"wa:{to}", asker=name)
    except Exception:
        log.exception("twilio answer failed")
        reply = "Sorry, something went wrong on my side. Please try again shortly."
    _twilio_send(to, from_, reply)


@app.post("/webhooks/twilio")
async def twilio_webhook(request: Request):
    form = await request.form()
    body, sender, bot = form.get("Body", ""), form.get("From", ""), form.get("To", "")
    name = form.get("ProfileName", "")
    if TWILIO_SID and TWILIO_TOKEN:
        # Claude can take longer than Twilio's 15 s webhook timeout, so reply asynchronously.
        threading.Thread(target=_twilio_job, args=(body, sender, bot, name), daemon=True).start()
        return Response("<Response/>", media_type="application/xml")
    reply = commands.handle(body, session_id=f"wa:{sender}", asker=name)
    return Response(f"<Response><Message>{escape(reply[:1600])}</Message></Response>", media_type="application/xml")
