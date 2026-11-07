import numpy as np
import pytest

from aes67vsc.elastic import ElasticBuffer


@pytest.mark.parametrize("ppm", [-300, -50, 0, 50, 300])
def test_drift_tracked_without_glitches(ppm):
    """Writer clock off by `ppm`; buffer must stay near target, never under/overrun,
    and output a clean sine (no clicks)."""
    rate, f = 48000, 997.0
    buf = ElasticBuffer(1, rate, target_frames=480)
    w_rate = rate * (1 + ppm * 1e-6)
    t_w = 0.0
    out = []
    w_pos = 0
    for step in range(60 * 1000 // 5):  # 60 s in 5 ms reader callbacks
        t_w += 0.005
        n_w = int(t_w * w_rate) - w_pos
        idx = w_pos + np.arange(n_w)
        buf.write(np.sin(2 * np.pi * f * idx / w_rate).astype(np.float32)[:, None])
        w_pos += n_w
        out.append(buf.read(240))
    assert buf.underruns == 0 and buf.overruns == 0
    assert abs(len(buf) + 240 - 480) < 100  # fill is regulated at read time
    assert abs((buf.ratio - 1) * 1e6 - ppm) < 30
    y = np.concatenate(out)[-rate * 5:, 0]  # last 5 s, settled
    # Fit a pure sine in each 10 ms window: residual must be tiny (no clicks / dropouts)
    w = 480
    t = np.arange(w) / rate
    A = np.column_stack([np.sin(2 * np.pi * f * t), np.cos(2 * np.pi * f * t)])
    worst = 0.0
    for k in range(0, len(y) - w, w):
        seg = y[k:k + w]
        fit, *_ = np.linalg.lstsq(A, seg, rcond=None)
        worst = max(worst, np.abs(seg - A @ fit).max())
    assert worst < 1e-3, worst
