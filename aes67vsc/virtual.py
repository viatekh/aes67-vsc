"""Virtual sound cards, so other apps can use network audio like a normal device.

Set a stream's device to "virtual" (the default for new installs):

* Linux (PulseAudio or PipeWire): we create the devices ourselves.
  - each send stream  -> a playback device "AES67 <name>" apps can output to
  - each receive slot -> a microphone "AES67 <stream>" apps can record from
* macOS: uses BlackHole (free), Windows: VB-CABLE (free) if installed.
  Two cables are needed to send and receive at the same time
  (BlackHole 2ch + 16ch, or VB-CABLE + CABLE A/B).
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import threading

import numpy as np

INSTALL_HINT = {
    "darwin": "Install BlackHole (free): https://existential.audio/blackhole/ or `brew install blackhole-2ch blackhole-16ch`",
    "win32": "Install VB-CABLE (free): https://vb-audio.com/Cable/",
}


def _slug(name: str) -> str:
    return "aes67_" + re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()[:40]


# ---------------------------------------------------------------- Linux (pulse/pipewire)
def pulse_available() -> bool:
    if not (sys.platform.startswith("linux") and shutil.which("pactl") and shutil.which("pacat")):
        return False
    return subprocess.run(["pactl", "info"], capture_output=True).returncode == 0


class _Modules:
    """Tracks pactl modules we loaded so they are removed on exit."""
    loaded: list[str] = []
    lock = threading.Lock()

    @classmethod
    def load(cls, *args) -> str:
        r = subprocess.run(["pactl", "load-module", *args], capture_output=True, text=True, check=True)
        with cls.lock:
            cls.loaded.append(r.stdout.strip())
        return r.stdout.strip()

    @classmethod
    def unload(cls, mod_id: str):
        subprocess.run(["pactl", "unload-module", mod_id], capture_output=True)
        with cls.lock:
            if mod_id in cls.loaded:
                cls.loaded.remove(mod_id)


def _props(desc: str) -> str:
    d = re.sub(r"[\"'\\]", "", desc)
    return f"\"device.description='{d}'\""


def pulse_send_device(name: str, channels: int, rate: int) -> tuple[str, list[str]]:
    """Null sink apps play into; we record its monitor. Returns (source to record, module ids)."""
    sink = _slug("send " + name)
    m = _Modules.load("module-null-sink", f"sink_name={sink}", f"rate={rate}", f"channels={channels}",
                      f"sink_properties={_props('AES67 ' + name)}")
    return f"{sink}.monitor", [m]


def pulse_recv_device(name: str, channels: int, rate: int) -> tuple[str, list[str]]:
    """A microphone apps can record from, fed by a FIFO we write. Returns (fifo path, module ids)."""
    import os
    import tempfile
    slug = _slug("recv " + name)
    fifo = os.path.join(tempfile.gettempdir(), f"{slug}_{os.getpid()}.fifo")
    m = _Modules.load("module-pipe-source", f"source_name={slug}", f"file={fifo}", "format=float32le",
                      f"rate={rate}", f"channels={channels}",
                      f"source_properties={_props('AES67 ' + name)}")
    return fifo, [m]


class PulseSource:
    """Records a Pulse/PipeWire source via `parec`. Same interface as audio.DeviceSource."""

    def __init__(self, device: str, channels: int, rate: int, modules=(), target_ms: float = 20):
        from .elastic import ElasticBuffer
        self.channels, self.modules = channels, list(modules)
        self.buf = ElasticBuffer(channels, rate, int(rate * target_ms / 1000))
        self.proc = subprocess.Popen(["parec", "-d", device, "--format=float32le", f"--rate={rate}",
                                      f"--channels={channels}", "--latency-msec=20", "--raw"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        frame = 4 * self.channels
        while True:
            data = self.proc.stdout.read(frame * 64)
            if not data:
                return
            n = len(data) // frame * frame
            self.buf.write(np.frombuffer(data[:n], "<f4").reshape(-1, self.channels).copy())

    def read(self, n: int) -> np.ndarray:
        return self.buf.read(n)

    def close(self):
        self.proc.terminate()
        for m in self.modules:
            _Modules.unload(m)


class PulseSink:
    """Feeds an ElasticBuffer into a pipe-source FIFO, paced by the system clock
    (which the sound server also follows); the elastic buffer absorbs the
    network-vs-system clock difference."""

    def __init__(self, buf, rate: int, fifo: str, modules=()):
        self.buf, self.modules, self.rate, self.fifo = buf, list(modules), rate, fifo
        self._stop = threading.Event()
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        import os
        import time
        try:
            fd = os.open(self.fifo, os.O_WRONLY)
        except OSError:
            return
        block = self.rate // 200  # 5 ms
        nxt = time.monotonic()
        try:
            while not self._stop.is_set():
                os.write(fd, self.buf.read(block).astype("<f4").tobytes())
                nxt += 0.005
                delay = nxt - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                elif delay < -0.1:  # stalled: don't burst to catch up
                    nxt = time.monotonic()
        except OSError:
            pass
        finally:
            os.close(fd)

    def close(self):
        self._stop.set()
        time_ = __import__("time")
        time_.sleep(0.02)
        for m in self.modules:
            _Modules.unload(m)


def cleanup():
    for m in list(_Modules.loaded):
        _Modules.unload(m)


# ---------------------------------------------------------------- macOS / Windows cables
CABLES = {
    # (device apps play into -> we record from, device we play into -> apps record from)
    "darwin": [("BlackHole 2ch", "BlackHole 16ch"), ("BlackHole 16ch", "BlackHole 2ch"),
               ("BlackHole 2ch", None), ("BlackHole 16ch", None)],
    "win32": [("CABLE Output", "CABLE-A Input"), ("CABLE Output", "CABLE-B Input"),
              ("CABLE-A Output", "CABLE-B Input"), ("CABLE Output", None)],
}


def find_cables(devices: list[dict]) -> tuple[str | None, str | None]:
    """Returns (input device for send capture, output device for receive playback)."""
    ins = [d["name"] for d in devices if d.get("max_input_channels", 0) > 0]
    outs = [d["name"] for d in devices if d.get("max_output_channels", 0) > 0]

    def has(names, pat):
        return next((n for n in names if pat.lower() in n.lower()), None)

    for rec, play in CABLES.get(sys.platform, []):
        r = has(ins, rec)
        p = has(outs, play) if play else None
        if r and (p or play is None):
            return r, p
    return None, None


def resolve(kind: str, name: str, channels: int, rate: int, log=print):
    """Build the source ("send") or sink factory ("recv") for device="virtual".
    Returns an object, or None with a log message if no virtual device is possible."""
    if pulse_available():
        if kind == "send":
            dev, mods = pulse_send_device(name, channels, rate)
            log(f"Virtual device: apps can now play to \"AES67 {name}\"")
            return PulseSource(dev, channels, rate, mods)
        dev, mods = pulse_recv_device(name, channels, rate)
        log(f"Virtual device: apps can now record from \"AES67 {name}\"")
        return lambda buf: PulseSink(buf, rate, dev, mods)
    try:
        import sounddevice as sd
        devices = list(sd.query_devices())
    except Exception as e:  # noqa: BLE001
        log(f"! virtual device: {e}")
        return None
    rec, play = find_cables(devices)
    from .audio import DeviceSink, DeviceSource
    if kind == "send" and rec:
        log(f"Virtual device: apps play to the cable, we send it ({rec})")
        return DeviceSource(channels, rate, rec)
    if kind == "recv" and play:
        log(f"Virtual device: apps record from the cable ({play})")
        return lambda buf: DeviceSink(buf, rate, play)
    log("! No virtual audio device found. " + INSTALL_HINT.get(sys.platform, "Install PulseAudio or PipeWire."))
    return None
