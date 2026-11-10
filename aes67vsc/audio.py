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


def fit_channels(x: np.ndarray, channels: int) -> np.ndarray:
    """Trim extra channels or pad missing ones with silence."""
    if x.shape[1] == channels:
        return x
    if x.shape[1] > channels:
        return x[:, :channels]
    return np.hstack([x, np.zeros((len(x), channels - x.shape[1]), np.float32)])


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
        sd = _sd()
        self.channels = channels
        have = int(sd.query_devices(device, "input")["max_input_channels"])
        self.dev_channels = max(1, min(channels, have))  # e.g. 8 requested on a 2-channel cable
        self.buf = ElasticBuffer(self.dev_channels, rate, int(rate * target_ms / 1000))
        self.stream = sd.InputStream(samplerate=rate, channels=self.dev_channels, dtype="float32",
                                     device=device, latency="low",
                                     callback=lambda d, f, t, s: self.buf.write(d.copy()))
        self.stream.start()

    def read(self, n: int) -> np.ndarray:
        return fit_channels(self.buf.read(n), self.channels)

    def close(self):
        self.stream.stop()
        self.stream.close()


class DeviceSink:
    """Plays an ElasticBuffer on a sound card."""

    def __init__(self, buf, rate: int, device=None):
        self.buf = buf
        sd = _sd()
        have = int(sd.query_devices(device, "output")["max_output_channels"])
        dev_channels = max(1, min(buf.channels, have))

        def cb(out, frames, t, status):
            out[:] = fit_channels(buf.read(frames), dev_channels)

        self.stream = sd.OutputStream(samplerate=rate, channels=dev_channels, dtype="float32",
                                         device=device, latency="low", callback=cb)
        self.stream.start()

    def close(self):
        self.stream.stop()
        self.stream.close()
