import time

import numpy as np

from aes67vsc.engine import Config, Engine, RecvCfg, SendCfg


def _peak(x, rate):
    return np.argmax(np.abs(np.fft.rfft(x))) * rate / len(x)


def test_two_streams_isolated_and_autoconnect():
    cfg = Config(interface="127.0.0.1", ptp_can_lead=False,
                 sends=[SendCfg("iso A", tone=500), SendCfg("iso B", tone=2000, rate=96000, channels=4)],
                 receives=[RecvCfg("iso A", null=True, latency_ms=100), RecvCfg("iso B", null=True, latency_ms=100)])
    eng = Engine(cfg, log=lambda *a: None)
    eng.start()
    try:
        time.sleep(2.5)
        rx = {st.name: r for r, _, st in eng.receivers.values()}
        assert set(rx) == {"iso A", "iso B"}
        a = rx["iso A"].buf.ring.read(len(rx["iso A"].buf.ring))
        b = rx["iso B"].buf.ring.read(len(rx["iso B"].buf.ring))
        assert a.shape[1] == 2 and b.shape[1] == 4
        assert abs(_peak(a[:, 0], 48000) - 500) < 100
        assert abs(_peak(b[:, 3], 96000) - 2000) < 200
        assert rx["iso A"].lost == 0 and rx["iso B"].lost == 0
    finally:
        eng.stop()
