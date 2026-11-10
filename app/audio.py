"""Audio helpers. The pipeline's internal format is 16-bit signed little-endian PCM, 8 kHz mono.

Twilio speaks 8 kHz G.711 mu-law; Asterisk AudioSocket speaks 8 kHz slin16. `audioop` is gone
in Python 3.13, so mu-law is implemented with numpy.
"""

import numpy as np

SAMPLE_RATE = 8000
FRAME_MS = 20
FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS // 1000  # 160
FRAME_BYTES = FRAME_SAMPLES * 2  # 320 bytes of PCM16

_BIAS = 0x84
_CLIP = 32635


def _build_decode_table() -> np.ndarray:
    table = np.zeros(256, dtype=np.int16)
    for i in range(256):
        u = ~i & 0xFF
        sign = u & 0x80
        exponent = (u >> 4) & 0x07
        mantissa = u & 0x0F
        sample = (((mantissa << 3) + _BIAS) << exponent) - _BIAS
        table[i] = -sample if sign else sample
    return table


_DECODE = _build_decode_table()


def ulaw_to_pcm16(data: bytes) -> bytes:
    return _DECODE[np.frombuffer(data, dtype=np.uint8)].tobytes()


def pcm16_to_ulaw(data: bytes) -> bytes:
    pcm = np.frombuffer(data, dtype=np.int16).astype(np.int32)
    sign = np.where(pcm < 0, 0x80, 0)
    mag = np.minimum(np.abs(pcm), _CLIP) + _BIAS
    exponent = np.floor(np.log2(mag)).astype(np.int32) - 7
    exponent = np.clip(exponent, 0, 7)
    mantissa = (mag >> (exponent + 3)) & 0x0F
    ulaw = ~(sign | (exponent << 4) | mantissa) & 0xFF
    return ulaw.astype(np.uint8).tobytes()


def chunk_frames(data: bytes, frame_bytes: int = FRAME_BYTES):
    """Yield fixed-size frames, zero-padding the tail."""
    for i in range(0, len(data), frame_bytes):
        frame = data[i : i + frame_bytes]
        if len(frame) < frame_bytes:
            frame = frame + b"\x00" * (frame_bytes - len(frame))
        yield frame
