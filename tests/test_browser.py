"""Drives GET /talk + the /talk/ws browser protocol with fake STT/TTS/LLM/CRM."""

import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app import session as session_mod
from app.audio import FRAME_BYTES, chunk_frames
from app.telephony import browser
from tests.test_twilio_e2e import FakeSTT, FakeTTS, fake_respond, voice_pcm

# "status" is read by VoiceAgent's call context (added with the Postgres CRM).
PRIYA = {"id": 1, "name": "Priya Sharma", "plan": "Plus", "status": "active", "balance_due": 0.0, "next_bill_date": "2026-11-15"}


class FakeCRM:
    async def find_customer_by_phone(self, phone):
        return dict(PRIYA) if phone == "+15551234567" else None

    async def recent_orders(self, customer_id, limit=5):
        return []


class FakeRecorder:
    """Stands in for calls.CallRecorder so these tests never touch the database."""

    instances: list["FakeRecorder"] = []

    def __init__(self, call_id, transport, caller_phone):
        self.call_id, self.transport, self.caller_phone = call_id, transport, caller_phone
        self.events, self.outcome, self.turns = [], None, 0
        FakeRecorder.instances.append(self)

    async def start(self, customer_id): ...
    async def event(self, kind, text, data=None):
        self.events.append((kind, text))

    def latency(self, ms): ...

    async def finish(self, outcome, verified, models_used):
        self.outcome = outcome


@pytest.fixture
def client(monkeypatch):
    FakeRecorder.instances.clear()
    if hasattr(session_mod, "CallRecorder"):
        monkeypatch.setattr(session_mod, "CallRecorder", FakeRecorder)
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "DeepgramTTS", FakeTTS)
    monkeypatch.setattr(session_mod.VoiceAgent, "respond", fake_respond)
    monkeypatch.setattr(session_mod, "get_crm", lambda: FakeCRM())
    app = FastAPI()
    app.include_router(browser.router)
    with TestClient(app) as c:
        yield c


def recv(ws):
    """Next message as ("audio", bytes) or (type, dict)."""
    msg = ws.receive()
    if msg["type"] == "websocket.close":
        raise WebSocketDisconnect(msg.get("code", 1000))
    if msg.get("bytes") is not None:
        return "audio", msg["bytes"]
    data = json.loads(msg["text"])
    return data["type"], data


def drain_until(ws, kind, ack_marks=True):
    seen = []
    while True:
        t, data = recv(ws)
        seen.append((t, data))
        if t == "mark" and ack_marks:
            ws.send_text(json.dumps({"type": "mark", "name": data["name"]}))
        if t == kind:
            return seen, data


def speak(ws, pcm):
    for frame in chunk_frames(pcm, FRAME_BYTES):
        ws.send_bytes(frame)


def start(ws, phone="+15551234567"):
    ws.send_text(json.dumps({"type": "start", "phone": phone}))


def finish_greeting(ws):
    seen, _ = drain_until(ws, "mark")  # the greeting sentence's mark
    drain_until(ws, "mark")  # end-of-greeting mark: acked, so the bot stops "speaking"
    return seen


def test_page_served(client):
    r = client.get("/talk")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "/talk/ws" in r.text and "Talk to Maya" in r.text


def test_greeting_transcript_audio_then_mark(client):
    with client.websocket_connect("/talk/ws") as ws:
        start(ws)
        t, first = recv(ws)
        assert t == "transcript" and first["speaker"] == "bot"
        assert first["text"].startswith("Hi Priya")
        seen, mark = drain_until(ws, "mark", ack_marks=False)
        assert any(t == "audio" and len(b) > 0 for t, b in seen)
        assert mark["name"]
        ws.send_text(json.dumps({"type": "mark", "name": mark["name"]}))
        _, end_mark = drain_until(ws, "mark")  # server accepted the ack and closed out the greeting
        assert end_mark["name"] != mark["name"]
        ws.send_text(json.dumps({"type": "hangup"}))
    if FakeRecorder.instances:
        rec = FakeRecorder.instances[-1]
        assert rec.transport == "browser" and rec.call_id.startswith("web-") and rec.caller_phone == "+15551234567"


def test_unknown_caller_gets_generic_greeting(client):
    with client.websocket_connect("/talk/ws") as ws:
        start(ws, "+15550001111")
        t, first = recv(ws)
        assert t == "transcript" and first["text"].startswith("Thanks for calling")
        ws.send_text(json.dumps({"type": "hangup"}))


def test_caller_speech_gets_transcript_and_reply_then_barge_in_clears(client):
    with client.websocket_connect("/talk/ws") as ws:
        start(ws)
        finish_greeting(ws)

        # Odd-sized chunks are re-framed server side.
        audio = voice_pcm(0.5) + b"\x00" * FRAME_BYTES * 3
        for i in range(0, len(audio), 500):
            ws.send_bytes(audio[i : i + 500])
        seen, caller = drain_until(ws, "transcript", ack_marks=False)
        assert caller["speaker"] == "caller" and caller["text"] == "where is my order"
        seen, mark = drain_until(ws, "mark", ack_marks=False)
        assert ("transcript", {"type": "transcript", "speaker": "bot", "text": "Your order shipped yesterday."}) in seen
        assert any(t == "audio" for t, _ in seen)

        # Talk over the bot while its marks are unacked (still "playing") -> clear.
        speak(ws, voice_pcm(0.6))
        drain_until(ws, "clear", ack_marks=False)
        ws.send_text(json.dumps({"type": "hangup"}))


@pytest.mark.parametrize("phone", ["abc", "+1555<script>", "1" * 40, "", None, 15551234567])
def test_invalid_phone_rejected(client, phone):
    with client.websocket_connect("/talk/ws") as ws:
        ws.send_text(json.dumps({"type": "start", "phone": phone}))
        with pytest.raises(WebSocketDisconnect) as exc:
            recv(ws)
        assert exc.value.code == 1008


def test_audio_before_start_is_a_protocol_error(client):
    with client.websocket_connect("/talk/ws") as ws:
        ws.send_text(json.dumps({"type": "mark", "name": "m1"}))
        with pytest.raises(WebSocketDisconnect) as exc:
            recv(ws)
        assert exc.value.code == 1008


def test_bot_hangup_sends_hangup_then_closes(client, monkeypatch):
    async def goodbye_respond(self, text):
        self._ctx.said_goodbye = True
        yield "Have a great day!"

    monkeypatch.setattr(session_mod.VoiceAgent, "respond", goodbye_respond)
    monkeypatch.setattr(session_mod, "GOODBYE_HANGUP_S", 0.3)
    with client.websocket_connect("/talk/ws") as ws:
        start(ws)
        finish_greeting(ws)
        speak(ws, voice_pcm(0.5) + b"\x00" * FRAME_BYTES * 3)
        drain_until(ws, "hangup")
        with pytest.raises(WebSocketDisconnect):
            recv(ws)
