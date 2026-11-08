"""Audio sources/sinks: sound card (via PortAudio) or test tone / null."""
from __future__ import annotations

import threading

import numpy as np


class Ring:
    """Thread-safe FIFO of float32 frames."""

    def __init__(self, channels: int, capacity: int):
        self.buf = np.zeros((capacity, channels), np.float32)
        self.cap, self.r, self.n = capacity, 0, 0
        self.lock = threading.Lock()

    def __len__(self):
        return self.n

    def write(self, x: np.ndarray):
        with self.lock:
            if len(x) > self.cap:
                x = x[-self.cap:]
            over = self.n + len(x) - self.cap
            if over > 0:  # drop oldest
                self.r = (self.r + over) % self.cap
                self.n -= over
            w = (self.r + self.n) % self.cap
            k = min(len(x), self.cap - w)
            self.buf[w:w + k] = x[:k]
            self.buf[:len(x) - k] = x[k:]
            self.n += len(x)

    def read(self, count: int) -> np.ndarray:
        with self.lock:
            k = min(count, self.n)
            idx = (self.r + np.arange(k)) % self.cap
            out = self.buf[idx]
            self.r = (self.r + k) % self.cap
            self.n -= k
            return out

    def drop(self, count: int):
        with self.lock:
            count = min(count, self.n)
            self.r = (self.r + count) % self.cap
            self.n -= count


def _sd():
    try:
        import sounddevice
        return sounddevice
    except OSError as e:  # PortAudio library missing
        raise RuntimeError(f"sound card access unavailable ({e}). On Linux: sudo apt install libportaudio2") from e


def list_devices() -> str:
    return str(_sd().query_devices())


class ToneSource:
    def __init__(self, channels: int, rate: int, freq: float = 440.0, level: float = 0.25):
        self.channels, self.rate, self.freq, self.level, self.phase = channels, rate, freq, level, 0

    def read(self, n: int) -> np.ndarray:
        t = (self.phase + np.arange(n)) / self.rate
        self.phase += n
        x = (self.level * np.sin(2 * np.pi * self.freq * t)).astype(np.float32)
        return np.repeat(x[:, None], self.channels, axis=1)

    def close(self):
        pass


class DeviceSource:
    """Captures from a sound card. Pads with silence / drops to absorb clock drift."""

    def __init__(self, channels: int, rate: int, device=None, target_ms: float = 20):
        from .elastic import ElasticBuffer
        self.buf = ElasticBuffer(channels, rate, int(rate * target_ms / 1000))
        self.stream = _sd().InputStream(samplerate=rate, channels=channels, dtype="float32",
                                        device=device, latency="low",
                                        callback=lambda d, f, t, s: self.buf.write(d.copy()))
        self.stream.start()

    def read(self, n: int) -> np.ndarray:
        return self.buf.read(n)

    def close(self):
        self.stream.stop()
        self.stream.close()


class DeviceSink:
    """Plays an ElasticBuffer on a sound card."""

    def __init__(self, buf, rate: int, device=None):
        self.buf = buf

        def cb(out, frames, t, status):
            out[:] = buf.read(frames)

        self.stream = _sd().OutputStream(samplerate=rate, channels=buf.channels, dtype="float32",
                                         device=device, latency="low", callback=cb)
        self.stream.start()

    def close(self):
        self.stream.stop()
        self.stream.close()
