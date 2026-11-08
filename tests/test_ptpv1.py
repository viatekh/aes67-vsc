import select
import struct
import threading
import time

from aes67vsc.net import multicast_rx, multicast_tx
from aes67vsc.ptp import Clock, PTP_GROUP

OFFSET = 1000 * 1_000_000_000  # simulated Dante leader runs 1000 s ahead of us


def _v1(ctrl, seq, body, msg_type, uuid=b"\x00\x1d\xc1\x11\x22\x33", flags=0):
    h = bytearray(40)
    struct.pack_into("!HH", h, 0, 1, 1)
    h[4:9] = b"_DFLT"
    h[20], h[21] = msg_type, 1
    h[22:28] = uuid
    struct.pack_into("!HHB", h, 28, 1, seq, ctrl)
    h[35] = flags
    return bytes(h) + body


def _ts(ns):
    return struct.pack("!II", ns // 1_000_000_000, ns % 1_000_000_000)


def fake_dante_leader(stop):
    tx = multicast_tx("127.0.0.1", ttl=1)
    ev = multicast_rx(PTP_GROUP, 319, "127.0.0.1")
    seq = 0
    while not stop.is_set():
        seq += 1
        t1 = time.time_ns() + OFFSET
        body = bytearray(84)
        body[14:20] = b"\x00\x1d\xc1\x11\x22\x33"  # grandmaster uuid
        body[27] = 1                               # stratum
        tx.sendto(_v1(0, seq, bytes(body), 1, flags=0x08), (PTP_GROUP, 319))   # two-step Sync
        tx.sendto(_v1(2, seq, struct.pack("!HH", 0, seq) + _ts(t1), 2), (PTP_GROUP, 320))  # Follow_Up
        end = time.monotonic() + 0.125
        while time.monotonic() < end:
            r, _, _ = select.select([ev], [], [], max(0, end - time.monotonic()))
            if r:
                p = ev.recv(1500)
                rx = time.time_ns() + OFFSET
                if len(p) >= 40 and p[:2] == b"\x00\x01" and p[32] == 1:  # Delay_Req -> Delay_Resp
                    body = _ts(rx) + b"\x00\x01" + p[22:28] + p[28:30] + p[30:32]
                    tx.sendto(_v1(3, 0, body, 2), (PTP_GROUP, 320))


def test_follows_dante_ptpv1_leader():
    stop = threading.Event()
    threading.Thread(target=fake_dante_leader, args=(stop,), daemon=True).start()
    c = Clock("127.0.0.1", identity=bytes.fromhex("0200aafffe000001"))
    c.start()
    try:
        time.sleep(5)  # past the point where it would otherwise become leader
        assert c.state == "follower" and c.version == 1
        assert c.gm_id == "00-1D-C1-FF-FE-11-22-33"
        assert c.path_delay_ns is not None, "delay request/response did not complete"
        assert abs(c.offset_ns - OFFSET) < 2_000_000, (c.offset_ns - OFFSET)
        assert abs(c.now_ns() - (time.time_ns() + OFFSET)) < 2_000_000
    finally:
        stop.set()
        c.stop()
