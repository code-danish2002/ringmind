import asyncio
import struct
import uuid

from app import session as session_mod
from app.telephony import asterisk
from tests.test_twilio_e2e import FakeSTT, FakeTTS


async def test_audiosocket_greets_with_paced_frames(monkeypatch):
    monkeypatch.setattr(session_mod, "DeepgramSTT", FakeSTT)
    monkeypatch.setattr(session_mod, "DeepgramTTS", FakeTTS)
    call_id = uuid.uuid4()
    asterisk._callers[str(call_id)] = ("+15551234567", 0)

    server = await asterisk.start_audiosocket_server(0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(struct.pack(">BH", asterisk.KIND_UUID, 16) + call_id.bytes)

    loop = asyncio.get_running_loop()
    t0, frames = loop.time(), 0
    while frames < 10:
        kind, length = struct.unpack(">BH", await reader.readexactly(3))
        await reader.readexactly(length)
        assert kind == asterisk.KIND_AUDIO and length == 320
        frames += 1
    assert loop.time() - t0 > 0.15  # ~20 ms per frame, not a burst

    writer.write(struct.pack(">BH", asterisk.KIND_HANGUP, 0))
    await writer.drain()
    writer.close()
    server.close()
