"""Twilio Programmable Voice: TwiML webhook + bidirectional Media Streams WebSocket.

Call flow: caller dials your Twilio number -> Twilio POSTs /twilio/voice -> we answer with
<Connect><Stream> -> Twilio opens wss://.../twilio/media and streams 8 kHz mu-law both ways.
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
from xml.sax.saxutils import escape, quoteattr

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, WebSocket, WebSocketDisconnect

from ..audio import pcm16_to_ulaw, ulaw_to_pcm16
from .. import calls
from ..config import get_settings
from ..session import CallSession, Transport

log = logging.getLogger(__name__)
router = APIRouter(prefix="/twilio")

# Signs the per-call stream token so only streams we set up via TwiML are accepted.
_STREAM_SECRET = (get_settings().twilio_auth_token or secrets.token_hex(32)).encode()


def _stream_token(call_sid: str) -> str:
    return hmac.new(_STREAM_SECRET, call_sid.encode(), hashlib.sha256).hexdigest()


def _valid_twilio_signature(auth_token: str, url: str, params: dict, signature: str) -> bool:
    payload = url + "".join(f"{k}{params[k]}" for k in sorted(params))
    digest = hmac.new(auth_token.encode(), payload.encode(), hashlib.sha1).digest()
    return hmac.compare_digest(base64.b64encode(digest).decode(), signature)


@router.post("/voice")
async def incoming_call(request: Request) -> Response:
    s = get_settings()
    form = dict(await request.form())
    if s.twilio_auth_token:
        url = s.public_url.rstrip("/") + request.url.path
        if not _valid_twilio_signature(s.twilio_auth_token, url, form, request.headers.get("X-Twilio-Signature", "")):
            raise HTTPException(status_code=403, detail="invalid Twilio signature")

    call_sid = form.get("CallSid", "")
    caller = form.get("From", "")
    if calls.active_count() >= s.max_concurrent_calls:
        busy = escape(f"Thanks for calling {s.company_name}. All of our lines are busy. Please call back in a few minutes.")
        return Response(
            content=f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{busy}</Say><Hangup/></Response>',
            media_type="application/xml",
        )
    ws_url = s.public_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/twilio/media"
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?><Response><Connect>'
        f"<Stream url={quoteattr(ws_url)}>"
        f'<Parameter name="caller" value={quoteattr(caller)}/>'
        f'<Parameter name="token" value="{_stream_token(call_sid)}"/>'
        "</Stream></Connect></Response>"
    )
    return Response(content=twiml, media_type="application/xml")


class TwilioTransport(Transport):
    kind = "twilio"

    def __init__(self, ws: WebSocket, stream_sid: str, call_sid: str):
        super().__init__()
        self._ws = ws
        self._stream_sid = stream_sid
        self._call_sid = call_sid
        s = get_settings()
        self.supports_transfer = bool(s.transfer_number and s.twilio_account_sid and s.twilio_auth_token)

    async def _send(self, msg: dict) -> None:
        try:
            await self._ws.send_text(json.dumps({**msg, "streamSid": self._stream_sid}))
        except (WebSocketDisconnect, RuntimeError):
            pass  # caller already hung up

    async def play(self, pcm: bytes) -> None:
        payload = base64.b64encode(pcm16_to_ulaw(pcm)).decode()
        await self._send({"event": "media", "media": {"payload": payload}})

    async def mark(self, name: str) -> None:
        await self._send({"event": "mark", "mark": {"name": name}})

    async def clear(self) -> None:
        await self._send({"event": "clear"})

    async def hangup(self) -> None:
        # Closing the stream ends <Connect>; with no further TwiML verbs the call hangs up.
        await self._ws.close()

    async def transfer(self, reason: str) -> bool:
        s = get_settings()
        log.info("Transferring %s to %s (%s)", self._call_sid, s.transfer_number, reason)
        twiml = f"<Response><Dial>{escape(s.transfer_number)}</Dial></Response>"
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(
                f"https://api.twilio.com/2010-04-01/Accounts/{s.twilio_account_sid}/Calls/{self._call_sid}.json",
                data={"Twiml": twiml},
                auth=(s.twilio_account_sid, s.twilio_auth_token),
            )
        if resp.status_code >= 300:
            log.error("Twilio transfer failed: %s %s", resp.status_code, resp.text[:300])
            return False
        return True


@router.websocket("/media")
async def media_stream(ws: WebSocket) -> None:
    await ws.accept()
    session: CallSession | None = None
    transport: TwilioTransport | None = None
    try:
        while True:
            msg = json.loads(await ws.receive_text())
            event = msg.get("event")
            if event == "media" and session is not None:
                if msg["media"].get("track", "inbound") == "inbound":
                    await session.handle_audio(ulaw_to_pcm16(base64.b64decode(msg["media"]["payload"])))
            elif event == "mark" and transport is not None and transport.on_mark:
                await transport.on_mark(msg["mark"]["name"])
            elif event == "start":
                start = msg["start"]
                params = start.get("customParameters", {})
                call_sid = start.get("callSid", "")
                if not hmac.compare_digest(params.get("token", ""), _stream_token(call_sid)):
                    log.warning("Rejecting media stream with bad token (call %s)", call_sid)
                    await ws.close(code=1008)
                    return
                transport = TwilioTransport(ws, msg["streamSid"], call_sid)
                session = CallSession(get_settings(), transport, call_id=call_sid, caller_phone=params.get("caller", ""))
                await session.start()
            elif event == "stop":
                break
    except WebSocketDisconnect:
        pass
    except Exception:
        log.exception("Media stream error")
    finally:
        if session is not None:
            await session.close()
