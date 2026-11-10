"""CallSession behaviour with a fake transport: patience line during slow answers."""

import asyncio

from app import session as session_mod
from app.config import Settings
from app.session import CallSession, Transport
from tests.test_twilio_e2e import FakeSTT, FakeTTS


class FakeTransport(Transport):
    kind = "test"

    def __init__(self):
        super().__init__()
        self.lines = []

    async def play(self, pcm): ...

    async def mark(self, name):
        await self.on_mark(name)  # "played" instantly

    async def clear(self): ...

    async def hangup(self): ...

    async def transcript(self, speaker, text):
        self.lines.append((speaker, text))


async def test_slow_answer_gets_a_patience_line(monkeypatch):
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "DeepgramTTS", FakeTTS)
    monkeypatch.setattr(session_mod, "PATIENCE_AFTER_S", 0.3)

    class SlowAgent:
        async def respond(self, text):
            yield "One moment."
            await asyncio.sleep(1.2)  # slow lookup / rate-limited model
            yield "Your order arrives Friday."

    t = FakeTransport()
    s = CallSession(Settings(), t, call_id="t1", caller_phone="+1")
    s._agent = SlowAgent()
    await s._run_turn("where is my order")
    await s.close()
    bot = [text for who, text in t.lines if who == "bot"]
    assert bot[0] == "One moment." and bot[-1] == "Your order arrives Friday."
    assert session_mod.PATIENCE_LINES[0] in bot


async def test_fast_answer_has_no_patience_line(monkeypatch):
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "DeepgramTTS", FakeTTS)
    monkeypatch.setattr(session_mod, "PATIENCE_AFTER_S", 0.3)

    class FastAgent:
        async def respond(self, text):
            yield "It arrives Friday."

    t = FakeTransport()
    s = CallSession(Settings(), t, call_id="t2", caller_phone="+1")
    s._agent = FastAgent()
    await s._run_turn("where is my order")
    await s.close()
    assert [text for who, text in t.lines if who == "bot"] == ["It arrives Friday."]


async def test_speech_service_down_ends_call_politely(monkeypatch):
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "DeepgramTTS", FakeTTS)
    hung_up = asyncio.Event()

    class T(FakeTransport):
        async def hangup(self):
            hung_up.set()

    t = T()
    s = CallSession(Settings(), t, call_id="t3", caller_phone="+1")
    await s._speech_unavailable()
    await asyncio.wait_for(hung_up.wait(), 2)
    assert s.outcome == "error_speech" and "technical problem" in t.lines[-1][1]
    await s.close()
