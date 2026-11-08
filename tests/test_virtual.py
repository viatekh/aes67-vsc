import subprocess
import threading
import time

import numpy as np
import pytest

from aes67vsc import virtual
from aes67vsc.engine import Config, Engine, RecvCfg, SendCfg

pulse = pytest.mark.skipif(not virtual.pulse_available(), reason="needs PulseAudio/PipeWire")


def test_find_cables(monkeypatch):
    devs = [{"name": "BlackHole 2ch", "max_input_channels": 2, "max_output_channels": 2},
            {"name": "BlackHole 16ch", "max_input_channels": 16, "max_output_channels": 16},
            {"name": "MacBook Speakers", "max_input_channels": 0, "max_output_channels": 2}]
    monkeypatch.setattr(virtual.sys, "platform", "darwin")
    assert virtual.find_cables(devs) == ("BlackHole 2ch", "BlackHole 16ch")
    monkeypatch.setattr(virtual.sys, "platform", "win32")
    devs = [{"name": "CABLE Output (VB-Audio Virtual Cable)", "max_input_channels": 2, "max_output_channels": 0},
            {"name": "CABLE Input (VB-Audio Virtual Cable)", "max_input_channels": 0, "max_output_channels": 2}]
    assert virtual.find_cables(devs) == ("CABLE Output (VB-Audio Virtual Cable)", None)


@pulse
def test_app_to_app_through_network():
    """App plays into "AES67 Studio" -> network -> another app records from "AES67 Studio" mic."""
    cfg = Config(interface="127.0.0.1", ptp_can_lead=False, nmos=False, ravenna=False,
                 sends=[SendCfg("Studio", device="virtual")],
                 receives=[RecvCfg("Studio", device="virtual", latency_ms=20)])
    eng = Engine(cfg, log=lambda *a: None)
    eng.start()
    rec = play = None
    try:
        for _ in range(30):
            if eng.receivers:
                break
            time.sleep(0.1)
        fmt = ["--format=float32le", "--rate=48000", "--channels=2", "--raw", "--latency-msec=20"]
        rec = subprocess.Popen(["parec", "-d", "aes67_recv_studio", *fmt], stdout=subprocess.PIPE)
        chunks = []
        threading.Thread(target=lambda: [chunks.append(c) for c in iter(lambda: rec.stdout.read(4096), b"")],
                         daemon=True).start()
        t = np.arange(48000 * 3) / 48000
        tone = np.repeat((0.3 * np.sin(2 * np.pi * 600 * t)).astype("<f4")[:, None], 2, 1)
        play = subprocess.Popen(["pacat", "-p", "-d", "aes67_send_studio", *fmt], stdin=subprocess.PIPE)
        play.stdin.write(tone.tobytes())
        play.stdin.close()
        time.sleep(3.5)
    finally:
        for p in (rec, play):
            if p:
                p.kill()
        eng.stop()
    d = b"".join(chunks)
    y = np.frombuffer(d[: len(d) // 8 * 8], "<f4").reshape(-1, 2)[:, 0]
    loud = y[np.abs(y) > 0.05]
    assert len(loud) > 48000, len(loud)  # > 1 s of tone made it through
    seg = y[np.argmax(np.abs(y) > 0.05):][:24000]
    assert abs(np.argmax(np.abs(np.fft.rfft(seg))) * 48000 / len(seg) - 600) < 10
    out = subprocess.run(["pactl", "list", "short", "modules"], capture_output=True, text=True).stdout
    assert "aes67_" not in out  # devices removed on exit
