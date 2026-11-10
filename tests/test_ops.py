"""Call recording, admin dashboard, capacity limit and readiness, through the real app + test DB."""

import json
import re
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from app import calls
from app import session as session_mod
from app.audio import FRAME_BYTES
from tests.test_twilio_e2e import FakeSTT, FakeTTS, drain_until, media, voice_pcm

AUTH = ("admin", "test-admin-pw")


async def fake_respond(self, text):
    # Exercise the recorder's tool path as a real turn would.
    from app.tools.registry import run_tool

    await run_tool("get_recent_orders", {}, self._ctx)
    yield "Your phone arrives in two days."


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "DeepgramTTS", FakeTTS)
    monkeypatch.setattr(session_mod.VoiceAgent, "respond", fake_respond)
    from app.main import app

    try:
        with TestClient(app) as c:
            yield c
    except RuntimeError as e:
        pytest.skip(f"test database unavailable: {e}")


def one_turn_call(client) -> str:
    sid = f"CA{uuid.uuid4().hex}"
    twiml = client.post("/twilio/voice", data={"CallSid": sid, "From": "+15551234567"}).text
    token = re.search(r'name="token" value="([0-9a-f]+)"', twiml).group(1)
    with client.websocket_connect("/twilio/media") as ws:
        ws.send_text(json.dumps({"event": "start", "streamSid": "MZ1", "start": {
            "callSid": sid, "customParameters": {"caller": "+15551234567", "token": token}}}))
        drain_until(ws, "mark")
        drain_until(ws, "mark")
        for m in media(voice_pcm(0.5)):
            ws.send_text(m)
        for m in media(b"\x00" * FRAME_BYTES * 3):
            ws.send_text(m)
        drain_until(ws, "mark")
        drain_until(ws, "mark")
        ws.send_text(json.dumps({"event": "stop"}))
    return sid


def finished_call(client, sid: str) -> dict:
    """The server finalises the record just after the socket closes; wait for it."""
    for _ in range(50):
        call = client.get(f"/admin/api/calls/{sid}", auth=AUTH).json()["call"]
        if call["outcome"]:
            return call
        time.sleep(0.1)
    raise AssertionError("call record never finalised")


def test_call_is_recorded_with_transcript_tools_and_latency(client):
    sid = one_turn_call(client)
    call = finished_call(client, sid)
    assert call["transport"] == "twilio" and call["customer"] == "Priya Sharma"
    assert call["outcome"] == "caller_hung_up" and call["turns"] == 1 and call["avg_response_ms"] is not None

    detail = client.get(f"/admin/api/calls/{sid}", auth=AUTH).json()
    kinds = [e["kind"] for e in detail["events"]]
    assert kinds[0] == "bot"  # greeting
    assert {"caller", "tool", "metric"} <= set(kinds)
    tool = next(e for e in detail["events"] if e["kind"] == "tool")
    assert tool["text"] == "get_recent_orders" and tool["data"]["result"]["orders"]

    page = client.get("/admin", auth=AUTH)
    assert page.status_code == 200 and "Priya Sharma" in page.text and "+1555123••••" in page.text
    assert client.get(f"/admin/calls/{sid}", auth=AUTH).status_code == 200
    assert client.get("/admin/tickets", auth=AUTH).status_code == 200
    assert client.get("/admin/callbacks", auth=AUTH).status_code == 200


def test_admin_requires_password(client):
    assert client.get("/admin").status_code == 401
    assert client.get("/admin", auth=("admin", "wrong")).status_code == 401
    assert client.get("/admin/api/calls", auth=("nope", "test-admin-pw")).status_code == 401


def test_over_capacity_twilio_gets_busy_twiml(client, monkeypatch):
    monkeypatch.setattr(calls, "active_count", lambda: 10**6)
    twiml = client.post("/twilio/voice", data={"CallSid": "CA2", "From": "+15551234567"}).text
    assert "<Say>" in twiml and "busy" in twiml and "<Stream" not in twiml


def test_ready_reports_checks(client):
    body = client.get("/ready").json()
    assert body["checks"]["database"] is True and "llm_models" in body["checks"]
