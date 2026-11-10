"""Pretend to be Twilio: stream a WAV file into /twilio/media and record what the bot says.

    python -m scripts.simulate_call question.wav --out bot_reply.wav [--phone +15551234567]

Runs against a local server started *without* TWILIO_AUTH_TOKEN (the stream token is fetched
from /twilio/voice exactly as Twilio would).
"""

import argparse
import asyncio
import base64
import json
import re
import wave

import httpx
import numpy as np
import websockets

from app.audio import FRAME_BYTES, SAMPLE_RATE, chunk_frames, pcm16_to_ulaw, ulaw_to_pcm16


def load_wav_8k(path: str) -> bytes:
    with wave.open(path, "rb") as w:
        assert w.getsampwidth() == 2, "need 16-bit PCM wav"
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        if w.getnchannels() > 1:
            audio = audio.reshape(-1, w.getnchannels()).mean(axis=1).astype(np.int16)
        rate = w.getframerate()
    if rate != SAMPLE_RATE:
        n = int(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.int16)
    return audio.tobytes()


async def main(args) -> None:
    call_sid = "CAsimulated0000000000000000000000"
    twiml = httpx.post(f"{args.server}/twilio/voice", data={"CallSid": call_sid, "From": args.phone}).text
    token = re.search(r'name="token" value="([0-9a-f]+)"', twiml).group(1)
    ws_url = re.search(r'<Stream url="([^"]+)"', twiml).group(1)

    received = bytearray()
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"event": "connected"}))
        await ws.send(json.dumps({"event": "start", "streamSid": "MZsim", "start": {
            "callSid": call_sid, "customParameters": {"caller": args.phone, "token": token}}}))

        async def reader():
            async for raw in ws:
                msg = json.loads(raw)
                if msg["event"] == "media":
                    received.extend(ulaw_to_pcm16(base64.b64decode(msg["media"]["payload"])))
                elif msg["event"] == "mark":  # we "play" instantly, so ack immediately
                    await ws.send(json.dumps({"event": "mark", "streamSid": "MZsim", "mark": msg["mark"]}))
                elif msg["event"] == "clear":
                    print("[bot audio cleared: barge-in]")

        async def send_pcm(pcm: bytes):
            for frame in chunk_frames(pcm, FRAME_BYTES):
                payload = base64.b64encode(pcm16_to_ulaw(frame)).decode()
                await ws.send(json.dumps({"event": "media", "streamSid": "MZsim", "media": {"track": "inbound", "payload": payload}}))
                await asyncio.sleep(0.02)

        read_task = asyncio.create_task(reader())
        silence = b"\x00" * FRAME_BYTES * 50  # 1 s
        await send_pcm(silence * 4)  # let the greeting play
        await send_pcm(load_wav_8k(args.wav))
        await send_pcm(silence * args.wait)  # wait for the answer
        await ws.send(json.dumps({"event": "stop", "streamSid": "MZsim"}))
        read_task.cancel()

    with wave.open(args.out, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(received))
    print(f"Wrote {len(received) / 2 / SAMPLE_RATE:.1f}s of bot audio to {args.out}")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("wav")
    p.add_argument("--out", default="bot_reply.wav")
    p.add_argument("--phone", default="+15551234567")
    p.add_argument("--server", default="http://localhost:8000")
    p.add_argument("--wait", type=int, default=12, help="seconds of silence to send after the question")
    asyncio.run(main(p.parse_args()))
