"""Asterisk via AudioSocket (TCP, 8 kHz slin16 frames).

Dialplan (extensions.conf) — register the caller ID, then hand the channel to the bot:

    exten => 100,1,Answer()
     same => n,Set(BOT_UUID=${SHELL(uuidgen | tr -d '\\n')})
     same => n,Set(CURL_RESULT=${CURL(http://BOT_HOST:8000/asterisk/register?uuid=${BOT_UUID}&caller=${URIENCODE(${CALLERID(num)})})})
     same => n,AudioSocket(${BOT_UUID},BOT_HOST:9092)
     same => n,Hangup()

Frame format: 1 byte kind, 2 bytes big-endian length, payload.
"""

import asyncio
import logging
import socket
import struct
import time
import uuid

from fastapi import APIRouter, HTTPException, Request

from ..audio import FRAME_BYTES, FRAME_MS, chunk_frames
from ..config import get_settings
from ..session import CallSession, Transport

log = logging.getLogger(__name__)
router = APIRouter(prefix="/asterisk")

KIND_HANGUP = 0x00
KIND_UUID = 0x01
KIND_DTMF = 0x03
KIND_AUDIO = 0x10
KIND_ERROR = 0xFF

_callers: dict[str, tuple[str, float]] = {}  # uuid -> (caller number, registered at)


@router.get("/register")
async def register(request: Request, uuid: str, caller: str = "") -> dict:
    # Only reachable from your Asterisk box: block it at the network level or proxy.
    if request.client and request.client.host not in _allowed_hosts():
        raise HTTPException(status_code=403)
    now = time.monotonic()
    for key in [k for k, (_, t) in _callers.items() if now - t > 300]:
        _callers.pop(key, None)
    _callers[uuid.lower()] = (caller, now)
    return {"ok": True}


def _allowed_hosts() -> set[str]:
    """Loopback plus ASTERISK_HOSTS, which may hold IPs or hostnames (e.g. the compose service)."""
    allowed = {"127.0.0.1", "::1"}
    for entry in filter(None, (h.strip() for h in get_settings().asterisk_hosts.split(","))):
        allowed.add(entry)
        try:
            allowed.update(info[4][0] for info in socket.getaddrinfo(entry, None))
        except socket.gaierror:
            pass
    return allowed


class AudioSocketTransport(Transport):
    """Paces outbound audio at real time (Asterisk doesn't buffer) and fires marks in order."""

    kind = "asterisk"

    def __init__(self, writer: asyncio.StreamWriter):
        super().__init__()
        self._writer = writer
        self._queue: asyncio.Queue[tuple[str, bytes | str]] = asyncio.Queue()
        self._pending = b""
        self._pacer = asyncio.create_task(self._pace())

    async def play(self, pcm: bytes) -> None:
        data = self._pending + pcm
        whole = len(data) - (len(data) % FRAME_BYTES)
        self._pending = data[whole:]
        for frame in chunk_frames(data[:whole]):
            self._queue.put_nowait(("audio", frame))

    async def mark(self, name: str) -> None:
        if self._pending:
            for frame in chunk_frames(self._pending):
                self._queue.put_nowait(("audio", frame))
            self._pending = b""
        self._queue.put_nowait(("mark", name))

    async def clear(self) -> None:
        # Unplayed marks are dropped, not acknowledged: an ack means "the caller heard this".
        self._pending = b""
        while not self._queue.empty():
            self._queue.get_nowait()

    async def hangup(self) -> None:
        self._write(KIND_HANGUP, b"")
        self._writer.close()

    def _write(self, kind: int, payload: bytes) -> None:
        if not self._writer.is_closing():
            self._writer.write(struct.pack(">BH", kind, len(payload)) + payload)

    async def _pace(self) -> None:
        next_at = time.monotonic()
        while True:
            kind, item = await self._queue.get()
            if kind == "mark":
                if self.on_mark:
                    await self.on_mark(item)
                continue
            now = time.monotonic()
            if next_at < now:
                next_at = now
            await asyncio.sleep(max(0.0, next_at - now))
            self._write(KIND_AUDIO, item)
            next_at += FRAME_MS / 1000

    def stop(self) -> None:
        self._pacer.cancel()


async def _handle_connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    transport = AudioSocketTransport(writer)
    session: CallSession | None = None
    try:
        while True:
            kind, length = struct.unpack(">BH", await reader.readexactly(3))
            payload = await reader.readexactly(length) if length else b""
            if kind == KIND_AUDIO and session is not None:
                await session.handle_audio(payload)
            elif kind == KIND_UUID:
                call_id = str(uuid.UUID(bytes=payload))
                caller, _ = _callers.pop(call_id, ("", 0))
                session = CallSession(get_settings(), transport, call_id=call_id, caller_phone=caller)
                await session.start()
            elif kind == KIND_HANGUP:
                break
            elif kind == KIND_ERROR:
                log.warning("AudioSocket error frame: %r", payload)
    except (asyncio.IncompleteReadError, ConnectionResetError):
        pass
    except Exception:
        log.exception("AudioSocket connection error")
    finally:
        transport.stop()
        if session is not None:
            await session.close()
        writer.close()


async def start_audiosocket_server(port: int) -> asyncio.base_events.Server:
    server = await asyncio.start_server(_handle_connection, host=get_settings().host, port=port)
    log.info("AudioSocket server listening on :%d", port)
    return server
