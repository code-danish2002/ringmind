"""Scripted voice call against a running bot: each caller line is spoken with real TTS and
streamed in real time over the browser websocket; the bot's replies are printed as it says them.

    python -m scripts.call_test                                   # default scenario, localhost:8080
    python -m scripts.call_test --server ws://localhost:8000 --phone +15559876543 \\
        "Hi, when is my store appointment?" "Thanks, bye"

Exercises the whole pipeline (STT, LLM pool, tools, database, TTS) and leaves a call record in
the admin dashboard. Needs DEEPGRAM_API_KEY in .env to synthesise the caller's voice.
"""

import argparse
import asyncio
import json
import time

import websockets

from app.audio import FRAME_BYTES, chunk_frames
from app.config import get_settings
from app.speech.tts import DeepgramTTS

DEFAULT_SCRIPT = [
    "Hi, I need to book a store appointment for tomorrow to fix my cracked screen.",
    "The downtown store, the earliest time you have please.",
    "My PIN is one two three four.",
    "Yes, please book it.",
    "Also, how long does a screen repair usually take?",
    "Great, that's all. Thanks, bye!",
]


async def speak(tts: DeepgramTTS, text: str) -> bytes:
    pcm = b""
    async for chunk in tts.synthesize(text):
        pcm += chunk
    return pcm


async def main(server: str, phone: str, lines: list[str], quiet_s: float) -> None:
    tts = DeepgramTTS(get_settings())
    audio = [await speak(tts, line) for line in lines]
    await tts.aclose()

    last_bot_activity = time.monotonic()
    bot_lines_this_turn = 0
    last_bot_line = ""
    ended = asyncio.Event()

    async with websockets.connect(f"{server}/talk/ws", max_size=None) as ws:
        await ws.send(json.dumps({"type": "start", "phone": phone}))

        async def reader():
            nonlocal last_bot_activity, bot_lines_this_turn, last_bot_line
            try:
                async for msg in ws:
                    if isinstance(msg, bytes):
                        last_bot_activity = time.monotonic()
                        continue
                    data = json.loads(msg)
                    if data["type"] == "transcript":
                        who = "Maya  " if data["speaker"] == "bot" else "Caller"
                        if data["speaker"] == "bot":
                            bot_lines_this_turn += 1
                            last_bot_line = data["text"]
                            last_bot_activity = time.monotonic()
                        print(f"{who}> {data['text']}", flush=True)
                    elif data["type"] == "mark":  # we don't play audio, so it has "played" already
                        await ws.send(json.dumps({"type": "mark", "name": data["name"]}))
                    elif data["type"] == "hangup":
                        print("[bot hung up]")
                        ended.set()
            except websockets.ConnectionClosed:
                ended.set()

        async def wait_for_bot():
            nonlocal bot_lines_this_turn
            bot_lines_this_turn = 0
            start = time.monotonic()
            while not ended.is_set():
                await asyncio.sleep(0.2)
                idle = time.monotonic() - last_bot_activity
                # Like a person: a question means "your turn"; after a filler ("One moment."), keep waiting.
                if bot_lines_this_turn and (
                    (idle > quiet_s and last_bot_line.rstrip().endswith("?")) or idle > max(quiet_s, 6.0)
                ):
                    return
                if time.monotonic() - start > 30:
                    print("[no reply within 30 s]")
                    return

        talking = False

        async def send_pcm(pcm: bytes):
            nonlocal talking
            talking = True
            for frame in chunk_frames(pcm, FRAME_BYTES):
                await ws.send(frame)
                await asyncio.sleep(0.02)
            talking = False

        async def line_noise():
            # Like a real phone line: audio (silence) keeps flowing between utterances.
            while not ended.is_set():
                if not talking:
                    await ws.send(b"\x00" * FRAME_BYTES)
                await asyncio.sleep(0.02)

        read_task = asyncio.create_task(reader())
        line_task = asyncio.create_task(line_noise())
        await wait_for_bot()  # greeting
        silence = b"\x00" * FRAME_BYTES * 50
        for pcm in audio:
            if ended.is_set():
                break
            await send_pcm(pcm + silence)
            await wait_for_bot()
        try:  # give the bot a chance to hang up by itself after the goodbye
            await asyncio.wait_for(ended.wait(), timeout=8)
        except asyncio.TimeoutError:
            print("[caller hung up]")
            await ws.send(json.dumps({"type": "hangup"}))
        read_task.cancel()
        line_task.cancel()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("lines", nargs="*", help="What the caller says, one argument per turn")
    p.add_argument("--server", default="ws://localhost:8080")
    p.add_argument("--phone", default="+15551234567")
    p.add_argument("--quiet", type=float, default=2.5, help="Seconds of bot silence that end its turn")
    a = p.parse_args()
    asyncio.run(main(a.server, a.phone, a.lines or DEFAULT_SCRIPT, a.quiet))
