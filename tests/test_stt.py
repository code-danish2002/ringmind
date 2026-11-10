"""DeepgramSTT against a local fake Deepgram server: keepalives and reconnect after a drop."""

import asyncio
import json

import websockets

from app.config import Settings
from app.speech import stt as stt_mod


def result(text, final=True, speech_final=True):
    return json.dumps({"type": "Results", "is_final": final, "speech_final": speech_final,
                       "channel": {"alternatives": [{"transcript": text}]}})


async def test_keepalive_and_reconnect(monkeypatch):
    connections, received = [], []

    async def fake_deepgram(ws):
        connections.append(ws)
        n = len(connections)
        async for msg in ws:
            received.append((n, msg if isinstance(msg, str) else "audio"))
            if n == 1 and msg == b"drop":
                await ws.send(result("first part"))
                await ws.close()  # simulate Deepgram dropping the stream
                return
            if n == 2 and msg == b"hello":
                await ws.send(result("still hearing you"))

    async with websockets.serve(fake_deepgram, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        monkeypatch.setattr(stt_mod, "DEEPGRAM_LISTEN_URL", f"ws://127.0.0.1:{port}")
        monkeypatch.setattr(stt_mod, "KEEPALIVE_S", 0.2)
        monkeypatch.setattr(stt_mod, "RECONNECT_DELAYS", (0.05, 0.05))

        heard = []

        async def on_utterance(text):
            heard.append(text)

        stt = stt_mod.DeepgramSTT(Settings(deepgram_api_key="x"), on_utterance)
        await stt.start()
        await asyncio.sleep(0.5)  # idle: keepalives should flow
        assert any(m == json.dumps({"type": "KeepAlive"}) for _, m in received)

        await stt.send_audio(b"drop")
        for _ in range(50):
            await asyncio.sleep(0.05)
            if len(connections) == 2 and stt._ws is not None:
                break
        assert len(connections) == 2, "did not reconnect"
        await stt.send_audio(b"hello")
        await asyncio.sleep(0.2)
        await stt.close()

    assert heard == ["first part", "still hearing you"]
