"""Minimal PTPv2 (IEEE 1588-2008) follower.

Listens to the grandmaster's Sync/Follow_Up messages (Dante devices in AES67
mode act as one) to know the network's media clock. Software timestamps only,
and no path-delay measurement: good to roughly tens of microseconds on a LAN,
well inside Dante's minimum 1 ms receive latency.

Ports 319/320 are privileged on Linux/macOS, so this needs sudo there.
"""
from __future__ import annotations

import collections
import select
import socket
import struct
import threading
import time

from .net import multicast_rx

PTP_GROUP = "224.0.1.129"
TAI_UTC_OFFSET = 37  # seconds; used only when no PTP master is present


def _ts(b: bytes, off: int) -> int:
    """PTP timestamp (48-bit seconds, 32-bit ns) -> ns."""
    sec = int.from_bytes(b[off:off + 6], "big")
    ns = int.from_bytes(b[off + 6:off + 10], "big")
    return sec * 1_000_000_000 + ns


def fmt_id(b: bytes) -> str:
    return "-".join(f"{x:02X}" for x in b)


class Follower:
    def __init__(self, iface_ip: str, domain: int = 0):
        self.iface_ip = iface_ip
        self.domain = domain
        self.gm_id: str | None = None
        self.offset_ns: int | None = None  # master - local
        self.last_sync = 0.0
        self._samples = collections.deque(maxlen=16)
        self._pending: dict[tuple, int] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self.error: str | None = None

    def start(self) -> bool:
        try:
            self._ev = multicast_rx(PTP_GROUP, 319, self.iface_ip)
            self._gen = multicast_rx(PTP_GROUP, 320, self.iface_ip)
        except OSError as e:
            self.error = str(e)
            return False
        threading.Thread(target=self._run, daemon=True).start()
        return True

    @property
    def locked(self) -> bool:
        return self.offset_ns is not None and time.monotonic() - self.last_sync < 3

    def now_ns(self) -> int:
        """Current PTP (TAI) time in ns, or a best guess if unsynced."""
        off = self.offset_ns if self.offset_ns is not None else TAI_UTC_OFFSET * 1_000_000_000
        return time.time_ns() + off

    def _run(self):
        socks = [self._ev, self._gen]
        while not self._stop.is_set():
            r, _, _ = select.select(socks, [], [], 0.5)
            for s in r:
                try:
                    pkt = s.recv(1500)
                except OSError:
                    return
                self._handle(pkt, time.time_ns())

    def _handle(self, p: bytes, rx_ns: int):
        if len(p) < 44 or p[1] & 0x0F != 2 or p[4] != self.domain:
            return
        mtype = p[0] & 0x0F
        correction = struct.unpack_from("!q", p, 8)[0] >> 16
        src = p[20:30]
        seq = struct.unpack_from("!H", p, 30)[0]
        if mtype == 0xB and len(p) >= 61:  # Announce
            if self.gm_id is None:
                self.gm_id = fmt_id(p[53:61])
        elif mtype == 0x0:  # Sync
            if p[6] & 0x02:  # two-step: wait for Follow_Up
                self._pending[(src, seq)] = rx_ns
                if len(self._pending) > 64:
                    self._pending.pop(next(iter(self._pending)))
            else:
                self._sample(_ts(p, 34) + correction - rx_ns, src)
        elif mtype == 0x8:  # Follow_Up
            rx = self._pending.pop((src, seq), None)
            if rx is not None:
                self._sample(_ts(p, 34) + correction - rx, src)

    def _sample(self, offset: int, src: bytes):
        if self.gm_id is None:
            self.gm_id = fmt_id(src[:8])
        with self._lock:
            self._samples.append(offset)
            # network delay only ever makes samples smaller -> max is the best estimate
            self.offset_ns = max(self._samples)
            self.last_sync = time.monotonic()

    def stop(self):
        self._stop.set()
