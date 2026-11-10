import numpy as np

from app.audio import FRAME_BYTES, chunk_frames, pcm16_to_ulaw, ulaw_to_pcm16
from app.vad import SpeechDetector


def test_ulaw_roundtrip_is_close():
    pcm = (np.sin(np.linspace(0, 200 * np.pi, 8000)) * 12000).astype(np.int16)
    back = np.frombuffer(ulaw_to_pcm16(pcm16_to_ulaw(pcm.tobytes())), dtype=np.int16)
    err = np.abs(back.astype(np.int32) - pcm.astype(np.int32))
    assert err.max() < 0.04 * 12000 + 16  # mu-law is ~2-3% relative error


def test_ulaw_known_values():
    assert pcm16_to_ulaw(np.array([0], dtype=np.int16).tobytes()) == b"\xff"
    assert ulaw_to_pcm16(b"\xff") == np.array([0], dtype=np.int16).tobytes()


def test_chunk_frames_pads():
    frames = list(chunk_frames(b"\x01" * (FRAME_BYTES + 10)))
    assert len(frames) == 2 and all(len(f) == FRAME_BYTES for f in frames)


def test_vad_ignores_silence_and_triggers_on_voice_like_signal():
    det = SpeechDetector(aggressiveness=1, trigger_ms=100)
    assert not det.feed(b"\x00" * FRAME_BYTES * 20)
    t = np.arange(8000) / 8000
    # Harmonic-rich "voiced" signal with amplitude modulation
    voice = sum(np.sin(2 * np.pi * f * t) / k for k, f in enumerate([140, 280, 420, 560, 700, 840], 1))
    voice *= (0.6 + 0.4 * np.sin(2 * np.pi * 4 * t)) * 6000
    assert det.feed(voice.astype(np.int16).tobytes())
