"""Browser test page: talk to the bot from a laptop microphone, no phone line needed.

GET /talk serves a single-page client; it opens ws://.../talk/ws and speaks this protocol:

  browser -> server
    {"type": "start", "phone": "+15551234567"}   first message; the phone picks the CRM caller
    <binary>                                     PCM16 LE 8 kHz mono mic audio (20 ms = 320 bytes)
    {"type": "mark", "name": "m3"}               playback reached a mark the server sent
    {"type": "hangup"}                           caller ended the call

  server -> browser
    <binary>                                     PCM16 LE 8 kHz mono bot audio
    {"type": "mark", "name": "m3"}               ack this once everything queued before it has played
    {"type": "clear"}                            stop playback now; drop unacked marks
    {"type": "transcript", "speaker": "caller"|"bot", "text": "..."}
    {"type": "hangup"}                           bot ended the call (socket closes next)
"""

import json
import logging
import re
from pathlib import Path
from uuid import uuid4

from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse

from ..audio import FRAME_BYTES
from ..config import get_settings
from ..session import CallSession, Transport

log = logging.getLogger(__name__)
router = APIRouter()

PAGE = Path(__file__).resolve().parent.parent / "static" / "talk.html"
_PHONE_RE = re.compile(r"^\+?[0-9]{3,20}$")
_MAX_FRAME = 64 * 1024  # bytes of audio per websocket message; anything bigger is abuse
POLICY_VIOLATION = 1008


@router.get("/talk", include_in_schema=False)
async def talk_page() -> FileResponse:
    return FileResponse(PAGE, media_type="text/html")


class BrowserTransport(Transport):
    kind = "browser"

    def __init__(self, ws: WebSocket):
        super().__init__()
        self._ws = ws
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    async def _send_json(self, msg: dict) -> None:
        if self._closed:
            return
        try:
            await self._ws.send_text(json.dumps(msg))
        except (WebSocketDisconnect, RuntimeError, OSError):
            self._closed = True  # browser already gone

    async def play(self, pcm: bytes) -> None:
        if self._closed or not pcm:
            return
        try:
            await self._ws.send_bytes(pcm)
        except (WebSocketDisconnect, RuntimeError, OSError):
            self._closed = True

    async def mark(self, name: str) -> None:
        await self._send_json({"type": "mark", "name": name})

    async def clear(self) -> None:
        await self._send_json({"type": "clear"})

    async def transcript(self, speaker: str, text: str) -> None:
        await self._send_json({"type": "transcript", "speaker": speaker, "text": text})

    async def hangup(self) -> None:
        await self._send_json({"type": "hangup"})
        await self.close()

    async def close(self, code: int = 1000) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._ws.close(code=code)
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass


def _valid_phone(phone) -> bool:
    return isinstance(phone, str) and bool(_PHONE_RE.match(phone))


@router.websocket("/talk/ws")
async def talk_ws(ws: WebSocket) -> None:
    await ws.accept()
    transport = BrowserTransport(ws)
    session: CallSession | None = None
    pending = b""  # re-frames arbitrary-size mic chunks into 20 ms frames
    try:
        while True:
            msg = await ws.receive()
            if msg["type"] == "websocket.disconnect":
                break
            data = msg.get("bytes")
            if data is not None:
                if session is None or len(data) > _MAX_FRAME:
                    continue
                pending += data
                whole = len(pending) - len(pending) % FRAME_BYTES
                frames, pending = pending[:whole], pending[whole:]
                for i in range(0, whole, FRAME_BYTES):
                    await session.handle_audio(frames[i : i + FRAME_BYTES])
                continue

            text = msg.get("text")
            if text is None:
                continue
            try:
                event = json.loads(text)
            except ValueError:
                event = None
            if not isinstance(event, dict):
                await transport.close(POLICY_VIOLATION)
                break
            kind = event.get("type")

            if kind == "start":
                phone = event.get("phone")
                if session is not None or not _valid_phone(phone):
                    log.warning("Rejecting browser call: bad start message")
                    await transport.close(POLICY_VIOLATION)
                    break
                session = CallSession(get_settings(), transport, call_id=f"web-{uuid4().hex[:12]}", caller_phone=phone)
                await session.start()
            elif session is None:
                # Anything other than "start" first is a protocol error.
                await transport.close(POLICY_VIOLATION)
                break
            elif kind == "mark":
                name = event.get("name")
                if isinstance(name, str) and transport.on_mark:
                    await transport.on_mark(name)
            elif kind == "hangup":
                break
    except WebSocketDisconnect:
        pass
    except RuntimeError:
        if not transport.closed:  # otherwise: receive() after hangup() closed the socket
            log.exception("Browser call error")
    except Exception:
        log.exception("Browser call error")
    finally:
        if session is not None:
            await session.close()
        await transport.close()
