"""SAP (RFC 2974) announce + discovery. This is how Dante Controller finds AES67 streams."""
from __future__ import annotations

import socket
import threading
import time
import zlib

from . import sdp
from .net import multicast_rx, multicast_tx

SAP_GROUP = "239.255.255.255"
SAP_PORT = 9875
MIME = b"application/sdp\x00"


def build(sdp_text: str, origin_ip: str, delete: bool = False) -> bytes:
    msg_hash = zlib.crc32(sdp_text.encode()) & 0xFFFF
    flags = 0x20 | (0x04 if delete else 0)  # V=1, IPv4, T=announce/delete
    return (bytes([flags, 0]) + msg_hash.to_bytes(2, "big") + socket.inet_aton(origin_ip)
            + MIME + sdp_text.encode())


def parse(pkt: bytes):
    """Returns (is_delete, origin_ip, msg_hash, sdp_text) or None."""
    if len(pkt) < 8 or pkt[0] >> 5 != 1 or pkt[0] & 0x10 or pkt[0] & 0x03:
        return None  # wrong version, IPv6, encrypted or compressed
    auth_len = pkt[1] * 4
    origin = socket.inet_ntoa(pkt[4:8])
    body = pkt[8 + auth_len:]
    if body.startswith(b"application/sdp\x00"):
        body = body[len(MIME):]
    elif not body.startswith(b"v=0"):
        return None
    return bool(pkt[0] & 0x04), origin, int.from_bytes(pkt[2:4], "big"), body.decode("utf-8", "replace")


class Announcer:
    """Re-announces a stream every `interval` seconds, sends a deletion on stop."""

    def __init__(self, stream: sdp.Stream, iface_ip: str, interval: float = 5.0):
        self.stream = stream
        self.iface_ip = iface_ip
        self.interval = interval
        self.sock = multicast_tx(iface_ip, ttl=15)
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    @property
    def sdp_text(self) -> str:
        return sdp.build(self.stream)

    def start(self):
        self._thread.start()

    def refresh(self):
        """Stream description changed (e.g. new PTP grandmaster): re-announce now."""
        self.stream.version += 1
        self._wake.set()

    def _run(self):
        while not self._stop.is_set():
            try:
                self.sock.sendto(build(self.sdp_text, self.iface_ip), (SAP_GROUP, SAP_PORT))
            except OSError:
                pass
            self._wake.wait(self.interval)
            self._wake.clear()

    def stop(self):
        self._stop.set()
        self._wake.set()
        try:
            self.sock.sendto(build(self.sdp_text, self.iface_ip, delete=True), (SAP_GROUP, SAP_PORT))
        except OSError:
            pass


class Browser:
    """Collects announced AES67 streams on the network."""

    def __init__(self, iface_ip: str, timeout: float = 300.0):
        self.sock = multicast_rx(SAP_GROUP, SAP_PORT, iface_ip)
        self.sock.settimeout(0.5)
        self.timeout = timeout
        self.streams: dict[tuple, tuple[float, sdp.Stream]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self._thread.start()
        return self

    def _run(self):
        while not self._stop.is_set():
            try:
                pkt, _ = self.sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            r = parse(pkt)
            if not r:
                continue
            delete, origin, h, text = r
            s = sdp.parse(text)
            key = (origin, s.session_id if s else h)
            with self._lock:
                if delete:
                    self.streams.pop(key, None)
                elif s:
                    self.streams[key] = (time.monotonic(), s)

    def list(self) -> list[sdp.Stream]:
        now = time.monotonic()
        with self._lock:
            return [s for t, s in self.streams.values() if now - t < self.timeout]

    def stop(self):
        self._stop.set()
        self.sock.close()
