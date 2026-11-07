"""Transmit an AES67 stream: audio source -> RTP multicast, paced by the PTP clock."""
from __future__ import annotations

import random
import threading
import time

from . import rtp, sap, sdp
from .net import multicast_tx
from .ptp import Follower


class Sender:
    def __init__(self, stream: sdp.Stream, source, clock: Follower, iface_ip: str):
        self.stream, self.source, self.clock = stream, source, clock
        self.sock = multicast_tx(iface_ip, ttl=15)
        self.ssrc = random.getrandbits(32)
        self.seq = random.getrandbits(16)
        self.packets = 0
        self._stop = threading.Event()
        self.announcer = sap.Announcer(stream, iface_ip)
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.announcer.start()
        self._thread.start()

    def _now_samples(self) -> int:
        # mediaclk:direct=0 -> RTP timestamp = PTP time * sample rate
        return self.clock.now_ns() * self.stream.rate // 1_000_000_000

    def _run(self):
        s, fpp = self.stream, self.stream.frames_per_packet
        dest = (s.address, s.port)
        next_ts = self._now_samples() - fpp
        while not self._stop.is_set():
            now = self._now_samples()
            if abs(now - next_ts) > s.rate // 10:  # stall or clock step: resync
                next_ts = now - fpp
            while next_ts + fpp <= now:
                payload = rtp.encode(self.source.read(fpp), s.encoding)
                self.sock.sendto(rtp.pack(s.payload_type, self.seq, next_ts, self.ssrc, payload), dest)
                self.seq += 1
                next_ts += fpp
                self.packets += 1
            time.sleep(0.0003)

    def stop(self):
        self._stop.set()
        self._thread.join(1)
        self.announcer.stop()
        self.source.close()
