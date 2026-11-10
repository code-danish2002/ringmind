"""Drives /twilio/voice + /twilio/media like Twilio would, with fake STT/TTS/LLM."""

import base64
import json
import re

import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import session as session_mod
from app.audio import FRAME_BYTES, chunk_frames, pcm16_to_ulaw

LOUD = None


def voice_pcm(seconds: float) -> bytes:
    t = np.arange(int(8000 * seconds)) / 8000
    v = sum(np.sin(2 * np.pi * f * t) / k for k, f in enumerate([140, 280, 420, 560, 700], 1))
    return (v * (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)) * 6000).astype(np.int16).tobytes()


class FakeSTT:
    """Emits one utterance when loud audio is followed by silence."""

    def __init__(self, settings, on_utterance, on_partial=None, on_failure=None):
        self._cb = on_utterance
        self._loud = 0

    async def start(self): ...
    async def close(self): ...

    async def send_audio(self, pcm):
        rms = np.sqrt(np.mean(np.frombuffer(pcm, dtype=np.int16).astype(np.float32) ** 2))
        if rms > 500:
            self._loud += 1
        elif self._loud > 10:
            self._loud = 0
            await self._cb("where is my order")


class FakeTTS:
    def __init__(self, settings): ...
    async def start(self): ...
    async def aclose(self): ...

    async def synthesize(self, text):
        for _ in range(5):
            yield b"\x10\x00" * 800  # 0.1 s


async def fake_respond(self, text):
    self.messages.append({"role": "user", "content": text})
    yield "Your order shipped yesterday."
    yield "It should arrive Friday."


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "DeepgramTTS", FakeTTS)
    monkeypatch.setattr(session_mod.VoiceAgent, "respond", fake_respond)
    from app.main import app

    with TestClient(app) as c:
        yield c


def media(pcm):
    for f in chunk_frames(pcm, FRAME_BYTES):
        yield json.dumps({"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(pcm16_to_ulaw(f)).decode()}})


def start_call(client, ws, token=None):
    twiml = client.post("/twilio/voice", data={"CallSid": "CA1", "From": "+15551234567"}).text
    assert "<Connect><Stream" in twiml
    tok = token or re.search(r'name="token" value="([0-9a-f]+)"', twiml).group(1)
    ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1", "start": {"callSid": "CA1", "customParameters": {"caller": "+15551234567", "token": tok}}}))


def drain_until(ws, kind, ack_marks=True):
    seen = []
    while True:
        msg = json.loads(ws.receive_text())
        seen.append(msg["event"])
        if msg["event"] == "mark" and ack_marks:
            ws.send_text(json.dumps({"event": "mark", "mark": msg["mark"]}))
        if msg["event"] == kind:
            return seen, msg


def test_rejects_bad_stream_token(client):
    with client.websocket_connect("/twilio/media") as ws:
        start_call(client, ws, token="deadbeef")
        with pytest.raises(Exception):
            ws.receive_text()


def test_greeting_turn_and_barge_in(client):
    with client.websocket_connect("/twilio/media") as ws:
        start_call(client, ws)
        # Greeting: audio then mark(s); acked so the bot finishes speaking.
        seen, _ = drain_until(ws, "mark")
        assert "media" in seen
        drain_until(ws, "mark")  # end-of-greeting mark

        # Caller asks something, then goes quiet -> bot answers.
        for m in media(voice_pcm(0.5)):
            ws.send_text(m)
        for m in media(b"\x00" * FRAME_BYTES * 3):
            ws.send_text(m)
        seen, _ = drain_until(ws, "mark", ack_marks=False)
        assert "media" in seen

        # Caller talks over the bot (marks unacked => still "playing") -> clear.
        for m in media(voice_pcm(0.6)):
            ws.send_text(m)
        seen, clear = drain_until(ws, "clear", ack_marks=False)
        assert clear["streamSid"] == "MZ1"
        ws.send_text(json.dumps({"event": "stop"}))


def test_silent_caller_after_goodbye_is_hung_up(client, monkeypatch):
    from starlette.websockets import WebSocketDisconnect

    async def goodbye_respond(self, text):
        self._ctx.said_goodbye = True
        yield "Have a great day!"

    monkeypatch.setattr(session_mod.VoiceAgent, "respond", goodbye_respond)
    monkeypatch.setattr(session_mod, "GOODBYE_HANGUP_S", 0.3)
    with client.websocket_connect("/twilio/media") as ws:
        start_call(client, ws)
        drain_until(ws, "mark")
        drain_until(ws, "mark")
        for m in media(voice_pcm(0.5)):
            ws.send_text(m)
        for m in media(b"\x00" * FRAME_BYTES * 3):
            ws.send_text(m)
        with pytest.raises(WebSocketDisconnect):
            while True:  # acks the goodbye's marks, then the server closes the stream
                drain_until(ws, "mark")
