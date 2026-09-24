"""Offline tests: parsing, dedupe, search and routing. No API key needed (Claude is stubbed)."""
import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

os.environ["DATA_DIR"] = tempfile.mkdtemp()
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pulse import brain, commands, parsers, store  # noqa: E402

SAMPLES = Path(__file__).resolve().parent.parent / "sample_data"
CHAT = SAMPLES / "WhatsApp Chat with UniPods Cohort 1 (SAMPLE).txt"
VTT = SAMPLES / "Week 4 live session 2026-09-23 (SAMPLE).vtt"

store.init()


def test_whatsapp_android_parse():
    [src] = parsers.parse_file(CHAT, CHAT.name)
    assert src.kind == "chat" and src.channel == "UniPods Cohort 1 (SAMPLE)"
    assert len(src.messages) == 29
    first = src.messages[0]
    assert first.ts == datetime(2026, 9, 15, 8, 5) and first.author == "Programme Office"


def test_whatsapp_ios_and_ampm():
    text = ("[9/18/26, 2:05:10 PM] Amara: hello there\n"
            "[9/18/26, 2:06:00 PM] Kwame: multi\nline message\n"
            "[9/18/26, 12:01:00 AM] Sipho: midnight\n"
            "[9/18/26, 2:07:00 PM] ‎Kwame: ‎image omitted\n")
    src = parsers.parse_whatsapp(text, "WhatsApp Chat - Test.txt")
    assert [m.author for m in src.messages] == ["Amara", "Kwame", "Sipho"]
    assert src.messages[0].ts == datetime(2026, 9, 18, 14, 5, 10)
    assert src.messages[1].text == "multi\nline message"
    assert src.messages[2].ts.hour == 0


def test_vtt_parse():
    [src] = parsers.parse_file(VTT, VTT.name)
    assert src.kind == "meeting" and src.date == datetime(2026, 9, 23)
    assert src.segments[0].speaker == "Programme Office" and src.segments[0].label == "00:00:05"


def test_ingest_dedupe_and_search():
    [src] = parsers.parse_file(CHAT, CHAT.name)
    _, added = store.add_messages("whatsapp", src.channel, src.messages)
    _, again = store.add_messages("whatsapp", src.channel, src.messages)
    assert added == 29 and again == 0
    hits = store.search("how do I get cloud credits?", 5)
    assert any("credits form" in h for h in hits)


def test_router(monkeypatch):
    calls = []
    monkeypatch.setattr(brain, "_call", lambda system, messages, max_tokens=4000: calls.append(messages) or "stub")
    assert "catch me up" in commands.handle("help", "t")
    assert commands.handle("When is the hackathon deadline?", "t") == "stub"
    assert commands.handle("catch me up since 2026-09-20", "t") == "stub"
    assert "2026-09-2" in calls[-1][0]["content"]  # digest got the right slice of messages
    assert commands.parse_since("3 days").date() < datetime.now().date()
