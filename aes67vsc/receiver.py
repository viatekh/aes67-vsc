"""Receive an AES67 stream: RTP multicast -> jitter buffer -> audio sink."""
from __future__ import annotations

import socket
import threading

import numpy as np

from . import rtp, sdp
from .elastic import ElasticBuffer
from .net import multicast_rx


class Receiver:
    def __init__(self, stream: sdp.Stream, iface_ip: str, latency_ms: float = 10):
        self.stream = stream
        self.sock = multicast_rx(stream.address, stream.port, iface_ip)
        self.sock.settimeout(0.5)
        self.target = max(stream.frames_per_packet * 2, int(stream.rate * latency_ms / 1000))
        self.buf = ElasticBuffer(stream.channels, stream.rate, self.target)
        self.packets = self.lost = 0
        self._expected_seq = None
        self.ssrc = None
        self._other_ssrc = 0
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()

    def _run(self):
        s = self.stream
        while not self._stop.is_set():
            try:
                pkt = self.sock.recv(9000)
            except socket.timeout:
                continue
            except OSError:
                break
            r = rtp.unpack(pkt)
            if not r or r[0] != s.payload_type:
                continue
            _, seq, _, ssrc, payload = r
            if self.ssrc is None:
                self.ssrc = ssrc
            elif ssrc != self.ssrc:  # different sender on same group/port, or sender restarted
                self._other_ssrc += 1
                if self._other_ssrc < 50:
                    continue
                self.ssrc, self._expected_seq = ssrc, None
            self._other_ssrc = 0
            if self._expected_seq is not None:
                gap = (seq - self._expected_seq) & 0xFFFF
                if gap >= 0x8000:  # late / duplicate packet
                    continue
                if 0 < gap < 100:  # conceal loss with silence
                    self.lost += gap
                    self.buf.write(np.zeros((gap * s.frames_per_packet, s.channels), np.float32))
            self._expected_seq = (seq + 1) & 0xFFFF
            self.buf.write(rtp.decode(payload, s.encoding, s.channels))
            self.packets += 1

    def stop(self):
        self._stop.set()
        self._thread.join(1)
        self.sock.close()
