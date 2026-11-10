"""Voice activity detection used for barge-in (caller talking over the bot).

Turn-ending is decided by the STT endpointer; this local VAD only needs to react fast
when the caller starts speaking so the bot can stop talking.
"""

import numpy as np
import webrtcvad

from .audio import FRAME_BYTES, FRAME_MS, SAMPLE_RATE


class SpeechDetector:
    def __init__(self, aggressiveness: int = 2, trigger_ms: int = 240, min_rms: int = 300):
        self._vad = webrtcvad.Vad(aggressiveness)
        self._trigger_frames = max(1, trigger_ms // FRAME_MS)
        self._min_rms = min_rms  # ignores line hiss / far-away noise the VAD flags as voice
        self._run = 0
        self._buf = b""

    def reset(self) -> None:
        self._run = 0
        self._buf = b""

    def feed(self, pcm: bytes) -> bool:
        """Feed PCM16 audio; return True once sustained speech is detected."""
        self._buf += pcm
        triggered = False
        while len(self._buf) >= FRAME_BYTES:
            frame, self._buf = self._buf[:FRAME_BYTES], self._buf[FRAME_BYTES:]
            if self._is_speech(frame):
                self._run += 1
                if self._run >= self._trigger_frames:
                    triggered = True
            else:
                self._run = 0
        return triggered

    def _is_speech(self, frame: bytes) -> bool:
        samples = np.frombuffer(frame, dtype=np.int16).astype(np.float32)
        if np.sqrt(np.mean(samples**2)) < self._min_rms:
            return False
        return self._vad.is_speech(frame, SAMPLE_RATE)
