"""Streaming speech-to-text over Deepgram's live WebSocket API.

Audio goes in as PCM16 8 kHz; finished caller turns come out through `on_utterance`.
Deepgram's endpointer (silence after speech) decides when a turn is over.

The connection is kept healthy for the whole call: KeepAlive messages cover stretches with no
audio (Deepgram closes idle streams after ~10 s), and a dropped connection is reopened with
backoff instead of leaving the bot deaf for the rest of the call.
"""

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable
from urllib.parse import urlencode

import websockets

from ..audio import SAMPLE_RATE
from ..config import Settings

log = logging.getLogger(__name__)

DEEPGRAM_LISTEN_URL = "wss://api.deepgram.com/v1/listen"
KEEPALIVE_S = 4.0
RECONNECT_DELAYS = (0.25, 0.5, 1, 2, 4)


class DeepgramSTT:
    def __init__(
        self,
        settings: Settings,
        on_utterance: Callable[[str], Awaitable[None]],
        on_partial: Callable[[str], Awaitable[None]] | None = None,
        on_failure: Callable[[], Awaitable[None]] | None = None,
    ):
        self._settings = settings
        self._on_utterance = on_utterance
        self._on_partial = on_partial
        self._on_failure = on_failure
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._keepalive: asyncio.Task | None = None
        self._reconnecting: asyncio.Task | None = None
        self._finals: list[str] = []
        self._last_send = time.monotonic()
        self._closing = False

    async def start(self) -> None:
        await self._connect()
        self._keepalive = asyncio.create_task(self._keepalive_loop())

    async def _connect(self) -> None:
        params = {
            "model": self._settings.stt_model,
            "language": self._settings.stt_language,
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "channels": 1,
            "interim_results": "true",
            "smart_format": "true",
            "endpointing": self._settings.stt_endpointing_ms,
            "utterance_end_ms": 1000,
            "vad_events": "true",
        }
        self._ws = await websockets.connect(
            f"{DEEPGRAM_LISTEN_URL}?{urlencode(params)}",
            additional_headers={"Authorization": f"Token {self._settings.deepgram_api_key}"},
            open_timeout=5,
        )
        self._last_send = time.monotonic()
        self._reader = asyncio.create_task(self._read_loop(self._ws))

    async def send_audio(self, pcm: bytes) -> None:
        ws = self._ws
        if ws is None:
            return  # reconnecting; this audio is lost (the caller will be asked to repeat if needed)
        try:
            await ws.send(pcm)
            self._last_send = time.monotonic()
        except websockets.ConnectionClosed:
            self._connection_lost("closed while sending audio")

    async def close(self) -> None:
        self._closing = True
        for task in (self._keepalive, self._reconnecting):
            if task:
                task.cancel()
        if self._ws is not None:
            try:
                await self._ws.send(json.dumps({"type": "CloseStream"}))
                await self._ws.close()
            except websockets.ConnectionClosed:
                pass
        if self._reader is not None:
            self._reader.cancel()

    # -- connection health ----------------------------------------------------------------------

    def _connection_lost(self, why: str) -> None:
        if self._closing or (self._reconnecting and not self._reconnecting.done()):
            return
        log.warning("Deepgram connection lost (%s); reconnecting", why)
        self._ws = None
        self._reconnecting = asyncio.create_task(self._reconnect())

    async def _reconnect(self) -> None:
        await self._flush()  # don't lose what the caller already said
        for delay in RECONNECT_DELAYS:
            await asyncio.sleep(delay)
            if self._closing:
                return
            try:
                await self._connect()
                log.info("Deepgram reconnected")
                return
            except (OSError, websockets.WebSocketException, asyncio.TimeoutError) as e:
                log.warning("Deepgram reconnect failed: %s", e)
        log.error("Deepgram unavailable; giving up on speech recognition for this call")
        if self._on_failure is not None:
            await self._on_failure()

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_S / 2)
            ws = self._ws
            if ws is not None and time.monotonic() - self._last_send >= KEEPALIVE_S:
                try:
                    await ws.send(json.dumps({"type": "KeepAlive"}))
                    self._last_send = time.monotonic()
                except websockets.ConnectionClosed:
                    self._connection_lost("closed during keepalive")

    async def _read_loop(self, ws) -> None:
        try:
            async for raw in ws:
                msg = json.loads(raw)
                kind = msg.get("type")
                if kind == "Results":
                    await self._handle_result(msg)
                elif kind == "UtteranceEnd":
                    # Fallback when endpointing never fired (e.g. background noise).
                    await self._flush()
        except websockets.ConnectionClosed:
            pass
        except Exception:
            log.exception("Deepgram reader crashed")
        if ws is self._ws:
            self._connection_lost("stream ended")

    async def _handle_result(self, msg: dict) -> None:
        alternatives = msg.get("channel", {}).get("alternatives") or [{}]
        text = alternatives[0].get("transcript", "").strip()
        if msg.get("is_final"):
            if text:
                self._finals.append(text)
            if msg.get("speech_final"):
                await self._flush()
        elif text and self._on_partial:
            await self._on_partial(text)

    async def _flush(self) -> None:
        if not self._finals:
            return
        utterance = " ".join(self._finals)
        self._finals.clear()
        await self._on_utterance(utterance)
