"""RTP packet + L16/L24 PCM codec (big-endian, interleaved)."""
import struct

import numpy as np

HEADER = struct.Struct("!BBHII")


def pack(payload_type: int, seq: int, ts: int, ssrc: int, payload: bytes) -> bytes:
    return HEADER.pack(0x80, payload_type & 0x7F, seq & 0xFFFF, ts & 0xFFFFFFFF, ssrc) + payload


def unpack(pkt: bytes):
    """Returns (payload_type, seq, ts, ssrc, payload) or None if not RTPv2."""
    if len(pkt) < 12 or pkt[0] >> 6 != 2:
        return None
    b0, b1, seq, ts, ssrc = HEADER.unpack_from(pkt)
    off = 12 + 4 * (b0 & 0x0F)  # skip CSRCs
    if b0 & 0x10:  # header extension
        if len(pkt) < off + 4:
            return None
        off += 4 + 4 * struct.unpack_from("!H", pkt, off + 2)[0]
    end = len(pkt)
    if b0 & 0x20:  # padding
        end -= pkt[-1]
    return b1 & 0x7F, seq, ts, ssrc, pkt[off:end]


def encode(frames: np.ndarray, encoding: str) -> bytes:
    """float32 (n, ch) in [-1, 1] -> network-order PCM bytes."""
    x = np.clip(frames, -1.0, 1.0)
    if encoding == "L16":
        return np.clip(np.round(x * 32768), -32768, 32767).astype(">i2").tobytes()
    i = np.clip(np.round(x * 8388608), -8388608, 8388607).astype(">i4").reshape(-1)
    return i.view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()


def decode(payload: bytes, encoding: str, channels: int) -> np.ndarray:
    """Network-order PCM bytes -> float32 (n, ch)."""
    if encoding == "L16":
        n = len(payload) // (2 * channels) * 2 * channels
        return (np.frombuffer(payload[:n], ">i2").astype(np.float32) / 32768).reshape(-1, channels)
    n = len(payload) // (3 * channels) * 3 * channels
    b = np.frombuffer(payload[:n], np.uint8).reshape(-1, 3)
    padded = np.zeros((len(b), 4), np.uint8)
    padded[:, :3] = b  # sample in top 24 bits, then arithmetic shift keeps sign
    v = padded.view(">i4").reshape(-1) >> 8
    return (v.astype(np.float32) / 8388608).reshape(-1, channels)
