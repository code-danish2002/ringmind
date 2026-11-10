"""Per-call orchestrator: VAD -> STT -> LLM (+tools) -> TTS, with barge-in.

Transport-agnostic: Twilio Media Streams, Asterisk AudioSocket and the browser test page all
implement `Transport`.
All audio crossing this boundary is PCM16 8 kHz mono.

Turn lifecycle:
  caller audio --> STT (endpointing) --> utterance --> new turn task
  turn task: the LLM streams sentences --> queue --> TTS --> transport (with a mark per sentence)
  caller speaks while bot is talking (VAD) --> cancel turn, clear far-end buffer, note what was heard
"""

import asyncio
import logging
import time
from abc import ABC, abstractmethod

from . import calls
from .agent.llm import VoiceAgent
from .audio import SAMPLE_RATE
from .calls import CallRecorder, Stopwatch
from .config import Settings
from .speech.stt import DeepgramSTT
from .speech.tts import DeepgramTTS
from .tools.crm import get_crm
from .tools.registry import CallContext, spoken_dates
from .vad import SpeechDetector

log = logging.getLogger(__name__)

IDLE_REPROMPT_S = 12.0
PATIENCE_AFTER_S = 3.0  # silence mid-turn (slow lookup, rate-limited model) before reassuring the caller
PATIENCE_LINES = ["Thanks for waiting, I'm still checking.", "Almost there, just a moment more."]
MAX_IDLE_REPROMPTS = 2
GOODBYE_HANGUP_S = 5.0  # bot said goodbye without end_call; hang up if the caller stays quiet


class Transport(ABC):
    """Telephony side of a call."""

    def __init__(self):
        self.on_mark = None  # set by CallSession: async callable(name)

    @abstractmethod
    async def play(self, pcm: bytes) -> None: ...

    @abstractmethod
    async def mark(self, name: str) -> None:
        """Ask the far end to report back (via on_mark) once audio queued so far has played."""

    @abstractmethod
    async def clear(self) -> None:
        """Drop audio that's queued but not yet played."""

    @abstractmethod
    async def hangup(self) -> None: ...

    async def transfer(self, reason: str) -> bool:
        return False

    async def transcript(self, speaker: str, text: str) -> None:
        """A line of the conversation ("caller" or "bot"). Optional; used for live captions."""

    supports_transfer = False
    kind = "unknown"  # "twilio", "asterisk", "browser": recorded on the call


class CallSession:
    def __init__(self, settings: Settings, transport: Transport, call_id: str, caller_phone: str):
        self._s = settings
        self._t = transport
        self._t.on_mark = self._handle_mark
        self.ctx = CallContext(call_id=call_id, caller_phone=caller_phone)
        if transport.supports_transfer:
            self.ctx.transfer = transport.transfer
        self.ctx.hangup = transport.hangup

        self._vad = SpeechDetector(settings.vad_aggressiveness, settings.barge_in_ms)
        self._stt = DeepgramSTT(settings, on_utterance=self._handle_utterance, on_failure=self._speech_unavailable)
        self._tts = DeepgramTTS(settings)
        self._agent: VoiceAgent | None = None

        self._turn: asyncio.Task | None = None
        self._speaking = False
        self._mark_seq = 0
        self._mark_waiters: dict[str, asyncio.Future] = {}
        self._spoken: list[tuple[str, str]] = []  # (mark name, sentence) sent this turn
        self._heard_upto = -1
        self._idle_task: asyncio.Task | None = None
        self._idle_count = 0
        self._closed = False
        self._watchdog: asyncio.Task | None = None
        self._close_task: asyncio.Future | None = None
        self._playback_end = 0.0

        # Outcome recorded when the call ends; set by whatever ends it.
        self.outcome = "caller_hung_up"
        self.recorder = CallRecorder(call_id, transport.kind, caller_phone)
        self.ctx.on_event = self.recorder.event
        self._stopwatch = Stopwatch()

    # -- lifecycle ---------------------------------------------------------------------------

    async def start(self) -> None:
        if calls.active_count() >= self._s.max_concurrent_calls:
            log.warning("[%s] rejected: %d calls already active", self.ctx.call_id, calls.active_count())
            self.outcome = "rejected_busy"
            await self.recorder.start(None)
            self._turn = asyncio.create_task(self._reject_busy())
            return
        calls.ACTIVE[self.ctx.call_id] = self

        try:
            crm = get_crm()
            self.ctx.customer = await crm.find_customer_by_phone(self.ctx.caller_phone)
            if self.ctx.customer:
                self.ctx.recent_orders = [
                    spoken_dates(o) for o in await crm.recent_orders(self.ctx.customer["id"], limit=3)
                ]
        except Exception:
            # The bot can still answer general questions and take callbacks without the CRM.
            log.exception("[%s] customer lookup failed; continuing as unknown caller", self.ctx.call_id)
        await self.recorder.start(self.ctx.customer["id"] if self.ctx.customer else None)
        self._agent = VoiceAgent(self._s, self.ctx)
        await self._stt.start()
        self._watchdog = asyncio.create_task(self._max_duration())
        log.info("[%s] %s call started (known customer: %s)", self.ctx.call_id, self._t.kind, bool(self.ctx.customer))

        first = self.ctx.customer["name"].split()[0] if self.ctx.customer else None
        greeting = (
            f"Hi {first}, thanks for calling {self._s.company_name}. This is {self._s.bot_name}. How can I help today?"
            if first
            else f"Thanks for calling {self._s.company_name}. This is {self._s.bot_name}. How can I help today?"
        )
        self._agent.note(f'Call connected. You already greeted the caller with: "{greeting}"')
        self._turn = asyncio.create_task(self._speak_static(greeting))

    async def close(self) -> None:
        # Shielded: the transport handler may be cancelled when the caller disconnects, and the
        # call record must still be finalised.
        if self._close_task is None:
            self._close_task = asyncio.ensure_future(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        if self._closed:
            return
        self._closed = True
        calls.ACTIVE.pop(self.ctx.call_id, None)
        for task in (self._turn, self._idle_task, self._watchdog):
            if task:
                task.cancel()
        await self._stt.close()
        await self._tts.aclose()
        await self.recorder.finish(self.outcome, self.ctx.verified, self.ctx.models_used)
        log.info("[%s] call ended: %s", self.ctx.call_id, self.outcome)

    # -- inbound -----------------------------------------------------------------------------

    async def handle_audio(self, pcm: bytes) -> None:
        await self._stt.send_audio(pcm)
        if self._vad.feed(pcm):
            self._cancel_idle()
            if self._speaking:
                await self._barge_in()

    async def _handle_utterance(self, text: str) -> None:
        log.info("[%s] caller said %d chars", self.ctx.call_id, len(text))
        self._stopwatch.start()
        await self._line("caller", text)
        self._cancel_idle()
        self._idle_count = 0
        if self._turn and not self._turn.done():
            # Caller kept talking before we answered: drop the in-flight answer, merge turns.
            await self._interrupt()
        self._turn = asyncio.create_task(self._run_turn(text))

    async def _handle_mark(self, name: str) -> None:
        for i, (mark, _) in enumerate(self._spoken):
            if mark == name:
                self._heard_upto = max(self._heard_upto, i)
        fut = self._mark_waiters.pop(name, None)
        if fut and not fut.done():
            fut.set_result(None)

    # -- barge-in ----------------------------------------------------------------------------

    async def _barge_in(self) -> None:
        log.info("[%s] barge-in", self.ctx.call_id)
        await self._interrupt()

    async def _interrupt(self) -> None:
        was_speaking = self._speaking  # the cancelled turn's `finally` resets the flag
        if self._turn and not self._turn.done():
            self._turn.cancel()
            try:
                await self._turn
            except asyncio.CancelledError:
                pass
        await self._t.clear()
        if was_speaking and self._spoken:
            heard = " ".join(s for _, s in self._spoken[: self._heard_upto + 1])
            unheard = " ".join(s for _, s in self._spoken[self._heard_upto + 1 :])
            if unheard:
                self._agent.note(
                    f'The caller interrupted you. They heard: "{heard}" but not: "{unheard}"'
                    if heard
                    else f'The caller interrupted you before hearing: "{unheard}"'
                )
        self._speaking = False
        self._vad.reset()

    # -- outbound ----------------------------------------------------------------------------

    async def _run_turn(self, text: str) -> None:
        sentences: asyncio.Queue[str | None] = asyncio.Queue()

        async def produce():
            try:
                async for sentence in self._agent.respond(text):
                    await sentences.put(sentence)
            finally:
                await sentences.put(None)

        # The patience clock starts now (or when the previous audio finishes playing).
        self._playback_end = max(self._playback_end, time.monotonic())

        async def reassure():
            # Never leave the caller in dead air: if the answer is slow, say so.
            for line in PATIENCE_LINES:
                while time.monotonic() - self._playback_end < PATIENCE_AFTER_S or not sentences.empty():
                    await asyncio.sleep(0.25)
                await sentences.put(line)
                await asyncio.sleep(PATIENCE_AFTER_S)

        producer = asyncio.create_task(produce())
        patience = asyncio.create_task(reassure())
        try:
            self._begin_speaking()
            while (sentence := await sentences.get()) is not None:
                await self._say(sentence)
            await producer
            await self._wait_played()
        finally:
            producer.cancel()
            patience.cancel()
            self._speaking = False
        await self._after_turn()

    async def _speak_static(self, text: str) -> None:
        self._begin_speaking()
        try:
            await self._say(text)
            await self._wait_played()
        finally:
            self._speaking = False
        self._arm_idle()

    def _begin_speaking(self) -> None:
        self._speaking = True
        self._spoken = []
        self._heard_upto = -1
        self._vad.reset()

    async def _line(self, speaker: str, text: str) -> None:
        await self._t.transcript(speaker, text)
        await self.recorder.event(speaker, text)

    async def _say(self, sentence: str) -> None:
        await self._line("bot", sentence)
        start = max(time.monotonic(), self._playback_end)
        sent = 0
        async for pcm in self._tts.synthesize(sentence):
            await self._t.play(pcm)
            sent += len(pcm)
            if (ms := self._stopwatch.stop()) is not None:
                self.recorder.latency(ms)
        self._playback_end = start + sent / (2 * SAMPLE_RATE)  # when the caller finishes hearing it
        name = self._next_mark()
        self._spoken.append((name, sentence))
        await self._t.mark(name)

    async def _wait_played(self) -> None:
        name = self._next_mark()
        fut = asyncio.get_running_loop().create_future()
        self._mark_waiters[name] = fut
        await self._t.mark(name)
        try:
            await asyncio.wait_for(fut, timeout=60)
        except asyncio.TimeoutError:
            log.warning("[%s] playback mark %s never acknowledged", self.ctx.call_id, name)

    def _next_mark(self) -> str:
        self._mark_seq += 1
        return f"m{self._mark_seq}"

    async def _after_turn(self) -> None:
        actions, self.ctx.pending_actions = self.ctx.pending_actions, []
        for action in actions:
            if action == "hangup":
                self.outcome = "completed"
                await self._t.hangup()
                return
            if action.startswith("transfer:"):
                if await self._t.transfer(action.split(":", 1)[1]):
                    self.outcome = "transferred"
                    return
                await self._speak_static("Sorry, I couldn't connect you right now. I can schedule a callback instead.")
        if self.ctx.said_goodbye:
            self._arm_idle(GOODBYE_HANGUP_S)
        else:
            self._arm_idle()

    # -- silence handling --------------------------------------------------------------------

    def _arm_idle(self, hangup_after: float | None = None) -> None:
        self._cancel_idle()
        if hangup_after is not None:
            self._idle_task = asyncio.create_task(self._hangup_after_silence(hangup_after))
        else:
            self._idle_task = asyncio.create_task(self._idle_timer())

    async def _hangup_after_silence(self, seconds: float) -> None:
        # Cancelled by _cancel_idle() as soon as the caller starts speaking again.
        await asyncio.sleep(seconds)
        log.info("[%s] goodbye said and caller silent; hanging up", self.ctx.call_id)
        self.outcome = "completed"
        await self._t.hangup()

    def _cancel_idle(self) -> None:
        if self._idle_task and not self._idle_task.done():
            self._idle_task.cancel()

    async def _idle_timer(self) -> None:
        await asyncio.sleep(IDLE_REPROMPT_S)
        self._idle_count += 1
        if self._idle_count > MAX_IDLE_REPROMPTS:
            self._turn = asyncio.create_task(self._goodbye())
            return
        self._agent.note("You asked the caller if they were still there after a long silence.")
        self._turn = asyncio.create_task(self._speak_static("Are you still there?"))

    async def _goodbye(self) -> None:
        await self._final_words("no_response", "I haven't heard anything for a while, so I'll end the call. Feel free to call back anytime. Goodbye!")

    async def _reject_busy(self) -> None:
        await self._final_words("rejected_busy", f"Thanks for calling {self._s.company_name}. All of our lines are busy right now. Please call back in a few minutes. Goodbye!")

    async def _max_duration(self) -> None:
        await asyncio.sleep(self._s.max_call_minutes * 60)
        await self._interrupt()
        self._cancel_idle()
        self._turn = asyncio.create_task(self._final_words(
            "max_duration", "We've reached the time limit for this call. If you need more help, I can have someone call you back, or you can call us again. Goodbye!"
        ))

    async def _speech_unavailable(self) -> None:
        """Speech recognition is down for good: say so instead of leaving the caller talking to nobody."""
        await self.recorder.event("system", "speech recognition unavailable", {})
        await self._interrupt()
        self._cancel_idle()
        self._turn = asyncio.create_task(self._final_words(
            "error_speech", "I'm sorry, I'm having trouble hearing you because of a technical problem. Please call us back in a few minutes. Goodbye."
        ))

    async def _final_words(self, outcome: str, text: str) -> None:
        self.outcome = outcome
        self._begin_speaking()
        try:
            await self._say(text)
            await self._wait_played()
        finally:
            self._speaking = False
        await self._t.hangup()
