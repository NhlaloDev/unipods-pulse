# Pulse: the UniPods group memory chatbot

**METI UniPods AI Innovation Programme, Cohort 1 · Chatbot Hackathon submission (team Amajor)**

The group is big, the chat moves fast, and people miss meetings. Pulse reads **everything the group produces**:
WhatsApp chats, meeting transcripts and recordings, and shared documents. It then **answers people directly**, with
citations to the original message or meeting timestamp.

| You ask… | Pulse does… |
|---|---|
| "When is the hackathon deadline?" | Answers from the records, cites *who said it and when*, and gives the **latest** version if it changed |
| "catch me up" / "catch me up since 2026-09-18" | Briefing: TL;DR, decisions, action items & deadlines (⏰ if due within 7 days), meeting recaps, **unanswered questions**, links |
| "last meeting" | Recap of the latest call: summary, decisions, action items, and timestamps to jump to |
| Anything already answered | Tells you who answered it and when, so nobody has to repeat themselves |

## Where people can use it

1. **Web app** (`http://<host>:8000`): chat, *Catch me up* date picker, and an admin page for uploads.
2. **Inside the WhatsApp group** (`bridge/`): the bot joins the group with its own number, **records every message live**,
   and replies when someone @mentions it, replies to it, or types `!pulse <question>` / `!catchup`. You can also DM it privately.
3. **WhatsApp official API via Twilio**: people message the bot's number; Pulse replies.
4. **Telegram** (optional): add the bot to a group; it records messages and answers mentions and DMs.

## Quick start (5 minutes)

Requires Python 3.10+ and an Anthropic API key.

```bash
git clone <this repo> && cd unipods-pulse
python -m venv .venv
.venv\Scripts\activate          # Windows   (macOS/Linux: source .venv/bin/activate)
pip install -r requirements.txt
copy .env.example .env          # macOS/Linux: cp .env.example .env
# edit .env: set ANTHROPIC_API_KEY and ADMIN_TOKEN
uvicorn pulse.main:app --host 0.0.0.0 --port 8000
```

Open http://localhost:8000 → **Knowledge base** → enter the admin token → drop in the files from `sample_data/`
(fictional demo data), then ask *"When is the hackathon deadline?"* or tap **Catch me up**.

### Loading the real group data
- **WhatsApp chat:** open the group → ⋮ → More → *Export chat* → *Without media* → upload the `.txt`/`.zip`.
  Upload a fresh export any time; messages already stored are skipped (no duplicates).
- **Meetings:** upload the Teams/Zoom/Google Meet transcript (`.vtt`, `.srt`, `.docx`, `.txt`). Pulse writes a summary
  automatically. Only have the recording? Run `pip install faster-whisper` once, then upload the `.mp4/.m4a/.mp3`
  and it is transcribed locally in the background.
- **Documents:** `.pdf`, `.docx`, `.txt`, `.md` (programme handbook, slides exported as PDF, etc.).
- Put a date in meeting file names (e.g. `Week 4 call 2026-09-23.vtt`) so catch-ups place them correctly.

## Connect it to WhatsApp

### Option A: bot inside the group (recommended for this cohort)
Uses a spare/dedicated WhatsApp number (not a personal one) via WhatsApp Web. Needs Node 18+.
```bash
cd bridge
npm install
npm start          # scan the QR code with the bot phone: WhatsApp → Linked devices
```
The bridge reads `PULSE_URL`, `ADMIN_TOKEN` and `WATCH_GROUPS` (comma-separated group names; empty = all groups)
from the root `.env`. Add the bot number to the cohort group. From then on the chat is ingested live, so no more
exports are needed. Note: `whatsapp-web.js` is unofficial; for a fully official route use Option B.

### Option B: official WhatsApp Business API through Twilio
1. Create a Twilio account → Messaging → *Try WhatsApp* (sandbox) or register a number.
2. Set the "When a message comes in" webhook to `https://<your-host>/webhooks/twilio`.
3. Put `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` in `.env` (replies are then sent asynchronously, so long
   answers never time out). People message the number directly; it cannot sit inside a normal group.

### Telegram (optional)
Create a bot with @BotFather, set `/setprivacy` → Disable, put the token in `TELEGRAM_BOT_TOKEN`, restart, and add the bot
to the group. Commands: `/ask`, `/catchup 3 days`, `/lastmeeting`, `/meetings`, `/reset`.

## Deploy

```bash
docker build -t pulse .
docker run -d -p 8000:8000 --env-file .env -v pulse-data:/data pulse
```
Works on any VM or on Render/Railway/Fly.io (mount a persistent volume at `/data`). Everything lives in one
SQLite file (`/data/pulse.db`). **Back it up** by copying that file.

## How it works

```
 WhatsApp export / live bridge / Telegram ─┐
 Teams·Zoom·Meet transcripts, recordings ──┼─► parsers ─► SQLite (messages + FTS5 search index) ─┐
 PDFs, docs ───────────────────────────────┘        meeting ─► Claude summary (auto)              │
                                                                                                   ▼
 Web · WhatsApp · Telegram ─► command router ─► catch-up digest | last meeting | Q&A ─► Claude ─► reply with citations
```
- **Small/medium knowledge base** (< `FULL_CONTEXT_CHARS`, ~50k tokens by default): the *entire* history is given to
  Claude, using prompt caching, so answers never miss anything. Older records sit in one cached block and the last
  2 days in another, so live chat doesn't invalidate the whole cache.
- **Large knowledge base:** BM25 full-text search picks the top chunks, plus the last few days of chat, and results are
  put in chronological order so Claude can tell which answer is the latest.
- Chat is chunked per group per day; meetings are chunked with timestamps and speakers; re-uploads are de-duplicated.
- Model: `claude-opus-5` with adaptive thinking (change `CLAUDE_MODEL` / `CLAUDE_EFFORT` in `.env`).

## Code map
| Path | What |
|---|---|
| `pulse/parsers.py` | WhatsApp (Android/iOS, 12/24h, dmy/mdy auto-detect), VTT/SRT, PDF, DOCX, audio (Whisper) |
| `pulse/store.py` | SQLite schema, de-duplication, chunking, FTS5 search |
| `pulse/brain.py` | Claude prompts: Q&A, catch-up digest, meeting summaries |
| `pulse/commands.py` | Shared command router for every channel |
| `pulse/main.py` | FastAPI app: web UI, admin API, Twilio webhook |
| `pulse/telegram.py` | Telegram long-polling connector |
| `bridge/` | WhatsApp group bridge (Node, whatsapp-web.js) |
| `tests/` | Offline tests (`pip install pytest && pytest`), no API key needed |

## API (for integrations)
- `POST /api/chat` `{text, session_id, asker}` → `{reply}`: questions and commands
- `POST /api/digest` `{since: "2026-09-18" | "3 days", until?}` → `{reply}`
- `POST /api/upload` (multipart `files`, optional `kind` = chat|meeting|doc), header `X-Admin-Token`
- `POST /api/ingest/message` `{platform, channel, author, text, ts}`, header `X-Admin-Token`
- `GET /api/sources`, `DELETE /api/sources/{id}`, `GET /api/info`

## Cost estimate
Claude Opus 5 is $5 / $25 per million input/output tokens. A typical question costs roughly **$0.05 to $0.30**
(cheaper when the prompt cache is warm), and a weekly catch-up costs $0.10 to $0.50. For a cohort asking ~30
questions a day, that's roughly **$50 to $150/month**. To cut this by ~2.5×, set `CLAUDE_MODEL=claude-sonnet-5`;
`CLAUDE_EFFORT=low` also helps. Hosting: a $5 to $7/month VM, or a free tier.

## Privacy & maintenance
- Only admins (with `ADMIN_TOKEN`) can add or remove sources, and removing a source deletes it from the index immediately.
- Tell the group the bot records the chat (pin a message). Data stays in your own SQLite file; only the relevant
  text is sent to the Anthropic API to answer a question.
- Keep secrets in `.env` (never commit it). Health check: `GET /api/info`.
