"""Streaming text-to-speech via Deepgram Aura. Returns raw PCM16 8 kHz chunks.

Latency matters more than anything here, so:
- one HTTP connection pool is shared by all calls (no TLS handshake per call or sentence);
- short phrases (fillers like "One moment.", fixed lines) are cached after first synthesis and
  can be pre-warmed at startup, so they play instantly.
"""

import logging
from collections import OrderedDict
from typing import AsyncIterator

import httpx

from ..audio import SAMPLE_RATE
from ..config import Settings

log = logging.getLogger(__name__)

DEEPGRAM_SPEAK_URL = "https://api.deepgram.com/v1/speak"
CACHE_MAX_CHARS = 60
CACHE_ENTRIES = 256

_client: httpx.AsyncClient | None = None
_cache: "OrderedDict[tuple[str, str], bytes]" = OrderedDict()


def _shared_client() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(15.0, read=30.0),
            limits=httpx.Limits(max_keepalive_connections=20, keepalive_expiry=60),
            http2=False,
        )
    return _client


async def close_shared() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


class DeepgramTTS:
    def __init__(self, settings: Settings):
        self._settings = settings

    async def synthesize(self, text: str) -> AsyncIterator[bytes]:
        key = (self._settings.tts_voice, text)
        if key in _cache:
            _cache.move_to_end(key)
            yield _cache[key]
            return

        cacheable = len(text) <= CACHE_MAX_CHARS
        collected = bytearray() if cacheable else None
        params = {
            "model": self._settings.tts_voice,
            "encoding": "linear16",
            "sample_rate": SAMPLE_RATE,
            "container": "none",
        }
        async with _shared_client().stream(
            "POST",
            DEEPGRAM_SPEAK_URL,
            params=params,
            headers={"Authorization": f"Token {self._settings.deepgram_api_key}"},
            json={"text": text},
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                log.error("TTS failed (%s): %s", resp.status_code, body[:300])
                return
            leftover = b""
            async for chunk in resp.aiter_bytes():
                # Keep sample alignment: PCM16 samples are 2 bytes.
                data = leftover + chunk
                cut = len(data) - (len(data) % 2)
                leftover = data[cut:]
                if cut:
                    if collected is not None:
                        collected += data[:cut]
                    yield data[:cut]
        if collected:
            _cache[key] = bytes(collected)
            while len(_cache) > CACHE_ENTRIES:
                _cache.popitem(last=False)

    async def prewarm(self, phrases: list[str]) -> None:
        """Synthesise fixed phrases ahead of time (best effort)."""
        for phrase in phrases:
            try:
                async for _ in self.synthesize(phrase):
                    pass
            except Exception as e:
                log.warning("TTS prewarm failed for %r: %s", phrase, e)
                return

    async def aclose(self) -> None:
        """Per-call cleanup; the shared pool stays open for other calls."""
