"""Dante audio flows (unicast). Ported from Inferno's flows_tx.rs / flows_rx.rs.

Packet: [0x02][u32 seconds][u32 sample-in-second][interleaved big-endian PCM]
Timestamps are the network (PTP) clock in samples. Receivers keep a unicast
flow alive by sending b"\\x13\\x37" back to the transmitter every 250 ms;
transmitters drop flows without keepalives for 4 s.
"""
from __future__ import annotations

import os
import select
import socket
import struct
import threading
import time

import numpy as np

KEEPALIVE = b"\x13\x37"
KEEPALIVE_INTERVAL = 0.25
KEEPALIVE_TIMEOUT = 4.0
FPP_MIN, FPP_MAX, FPP_MAX_ADVERTISED = 2, 256, 32
MAX_CHANNELS_IN_FLOW = 8
MAX_FLOWS = 32
MAX_PAYLOAD = 1400


def encode(x: np.ndarray, bps: int) -> bytes:
    """float32 (n, ch) -> interleaved big-endian PCM, 2/3/4 bytes per sample."""
    x = np.clip(x, -1.0, 1.0)
    if bps == 2:
        return np.clip(np.round(x * 32768), -32768, 32767).astype(">i2").tobytes()
    if bps == 4:
        return np.clip(np.round(x * 2147483648.0), -2147483648, 2147483647).astype(">i4").tobytes()
    i = np.clip(np.round(x * 8388608), -8388608, 8388607).astype(">i4").reshape(-1)
    return i.view(np.uint8).reshape(-1, 4)[:, 1:].tobytes()


def decode(b: bytes, bps: int, channels: int) -> np.ndarray:
    n = len(b) // (bps * channels) * bps * channels
    if bps == 2:
        return (np.frombuffer(b[:n], ">i2").astype(np.float32) / 32768).reshape(-1, channels)
    if bps == 4:
        return (np.frombuffer(b[:n], ">i4").astype(np.float32) / 2147483648.0).reshape(-1, channels)
    a = np.frombuffer(b[:n], np.uint8).reshape(-1, 3)
    p = np.zeros((len(a), 4), np.uint8)
    p[:, :3] = a
    return ((p.view(">i4").reshape(-1) >> 8).astype(np.float32) / 8388608).reshape(-1, channels)


def make_audio_packet(ts: int, rate: int, frames: np.ndarray, bps: int) -> bytes:
    return struct.pack("!BII", 2, (ts // rate) & 0xFFFFFFFF, ts % rate) + encode(frames, bps)


def parse_audio_packet(pkt: bytes, rate: int):
    """-> (timestamp in samples, payload) or None."""
    if len(pkt) < 9:
        return None
    sec, sub = struct.unpack_from("!II", pkt, 1)
    return sec * rate + sub, pkt[9:]


class TimedRing:
    """Multichannel audio indexed by network-clock sample timestamp."""

    def __init__(self, channels: int, size: int = 1 << 16):
        self.size, self.channels = size, channels
        self.buf = np.zeros((size, channels), np.float32)
        self.lock = threading.Lock()

    def _idx(self, ts: int, n: int):
        return (ts + np.arange(n)) % self.size

    def write(self, ts: int, frames: np.ndarray, channels=None):
        """Write frames (n, k) at ts into `channels` (list of k indices, default all)."""
        n = min(len(frames), self.size)
        idx = self._idx(ts, n)
        with self.lock:
            if channels is None:
                self.buf[idx] = frames[:n]
            else:
                for k, ch in enumerate(channels):
                    self.buf[idx, ch] = frames[:n, k]

    def read(self, ts: int, n: int, clear: bool = False) -> np.ndarray:
        idx = self._idx(ts, n)
        with self.lock:
            out = self.buf[idx].copy()
            if clear:
                self.buf[idx] = 0
        return out


def _samples_now(clock, rate: int) -> int | None:
    if not clock.locked:
        return None
    return clock.now_ns() * rate // 1_000_000_000


# --------------------------------------------------------------------------- TX
class TxFlow:
    def __init__(self, index: int, dst, channels: list, fpp: int, bps: int, rx_host="", rx_flow=""):
        self.index, self.dst, self.channels, self.fpp, self.bps = index, dst, channels, fpp, bps
        self.rx_host, self.rx_flow = rx_host, rx_flow
        self.cookie = int.from_bytes(os.urandom(2), "big")
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setblocking(False)
        self.next_ts: int | None = None
        self.expires = time.monotonic() + KEEPALIVE_TIMEOUT
        self.packets = 0

    @property
    def handle(self) -> bytes:
        return struct.pack("!IH", self.index, self.cookie)


class Transmitter:
    """Feeds a source into a TimedRing at the network clock, and sends flows from it."""

    def __init__(self, clock, rate: int, channels: int, source, iface_ip: str):
        self.clock, self.rate, self.nch, self.source = clock, rate, channels, source
        self.ip = iface_ip
        self.ring = TimedRing(channels)
        self.flows: dict[int, TxFlow] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._fill_ts = None
        threading.Thread(target=self._run, daemon=True, name="dante-tx").start()

    def add_flow(self, dst, channels, fpp, bps, rx_host="", rx_flow="") -> TxFlow:
        with self._lock:
            for f in self.flows.values():  # same receiver socket: update instead
                if f.dst == dst:
                    f.channels = channels
                    return f
            if len(self.flows) >= MAX_FLOWS:
                raise RuntimeError("too many flows")
            idx = next(i for i in range(MAX_FLOWS) if i not in self.flows)
            f = TxFlow(idx, dst, channels, fpp, bps, rx_host, rx_flow)
            f.sock.bind((self.ip, 0))
            f.sock.connect(dst)
            self.flows[idx] = f
            return f

    def by_handle(self, handle: bytes) -> TxFlow | None:
        idx, cookie = struct.unpack("!IH", handle[:6])
        f = self.flows.get(idx)
        return f if f and f.cookie == cookie else None

    def remove_flow(self, f: TxFlow):
        with self._lock:
            self.flows.pop(f.index, None)
        f.sock.close()

    def _run(self):
        while not self._stop.is_set():
            now = _samples_now(self.clock, self.rate)
            if now is None:
                time.sleep(0.05)
                continue
            # 1. pull new audio from the source up to "now"
            if self._fill_ts is None or not 0 <= now - self._fill_ts < self.rate // 2:
                self._fill_ts = now
            n = now - self._fill_ts
            if n > 0:
                self.ring.write(self._fill_ts, self.source.read(n))
                self._fill_ts = now
            # 2. send every flow's due packets
            t = time.monotonic()
            with self._lock:
                flows = list(self.flows.values())
            for f in flows:
                try:
                    while f.sock.recv(64):  # keepalives from the receiver
                        f.expires = t + KEEPALIVE_TIMEOUT
                except (BlockingIOError, OSError):
                    pass
                if t > f.expires:
                    self.remove_flow(f)
                    continue
                if f.next_ts is None or not -self.rate // 2 < now - f.next_ts < self.rate // 2:
                    f.next_ts = now - now % f.fpp  # align to packet boundary
                while f.next_ts + f.fpp <= now:
                    data = self.ring.read(f.next_ts, f.fpp)
                    frames = np.zeros((f.fpp, len(f.channels)), np.float32)
                    for k, ch in enumerate(f.channels):
                        if ch is not None and ch < self.nch:
                            frames[:, k] = data[:, ch]
                    try:
                        f.sock.send(make_audio_packet(f.next_ts, self.rate, frames, f.bps))
                        f.packets += 1
                    except OSError:
                        pass
                    f.next_ts += f.fpp
            time.sleep(0.0005)

    def stop(self):
        self._stop.set()
        for f in list(self.flows.values()):
            self.remove_flow(f)


# --------------------------------------------------------------------------- RX
class RxFlow:
    def __init__(self, index: int, iface_ip: str, channel_map: list, bps: int, latency: int,
                 tx_host: str = "", tx_addr=None):
        self.index, self.channel_map, self.bps, self.latency = index, channel_map, bps, latency
        self.tx_host, self.tx_addr = tx_host, tx_addr   # tx_addr: flow-control address (ip, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind((iface_ip, 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.source = None          # where audio comes from (for keepalives)
        self.handle: bytes | None = None
        self.packets = 0
        self.last_packet = 0.0
        self.actual_latency = 0


class Receiver:
    """Receives flows into a TimedRing and plays it out `latency` behind the network clock."""

    def __init__(self, clock, rate: int, channels: int, iface_ip: str, out_buffer=None):
        self.clock, self.rate, self.nch, self.ip = clock, rate, channels, iface_ip
        self.ring = TimedRing(channels)
        self.flows: dict[int, RxFlow] = {}
        self.out = out_buffer      # ElasticBuffer (or anything with write(frames))
        self.latency = rate // 100
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._play_ts = None
        threading.Thread(target=self._rx_loop, daemon=True, name="dante-rx").start()
        threading.Thread(target=self._play_loop, daemon=True, name="dante-play").start()

    def new_flow(self, channel_map, bps, latency_samples, tx_host="", tx_addr=None) -> RxFlow:
        with self._lock:
            idx = next(i for i in range(MAX_FLOWS) if i not in self.flows)
            f = RxFlow(idx, self.ip, channel_map, bps, latency_samples, tx_host, tx_addr)
            self.flows[idx] = f
            self.latency = max([x.latency for x in self.flows.values()] + [self.rate // 100])
            return f

    def remove_flow(self, f: RxFlow):
        with self._lock:
            self.flows.pop(f.index, None)
        f.sock.close()

    def _rx_loop(self):
        next_ka = 0.0
        while not self._stop.is_set():
            with self._lock:
                flows = list(self.flows.values())
            socks = {f.sock: f for f in flows}
            if not socks:
                time.sleep(0.05)
                continue
            try:
                r, _, _ = select.select(list(socks), [], [], 0.05)
            except (OSError, ValueError):
                continue
            now = _samples_now(self.clock, self.rate)
            for s in r:
                f = socks[s]
                try:
                    pkt, src = s.recvfrom(2048)
                except OSError:
                    continue
                p = parse_audio_packet(pkt, self.rate)
                if not p:
                    continue
                ts, payload = p
                f.source, f.packets, f.last_packet = src, f.packets + 1, time.monotonic()
                frames = decode(payload, f.bps, len(f.channel_map))
                if now is not None:
                    f.actual_latency = max(f.actual_latency, (now - ts) % (1 << 32))
                for k, locals_ in enumerate(f.channel_map):
                    if locals_:
                        self.ring.write(ts, frames[:, k:k + 1].repeat(len(locals_), 1), locals_)
            if time.monotonic() >= next_ka:
                next_ka = time.monotonic() + KEEPALIVE_INTERVAL
                for f in flows:
                    if f.source:
                        try:
                            f.sock.sendto(KEEPALIVE, f.source)
                        except OSError:
                            pass

    def _play_loop(self):
        while not self._stop.is_set():
            time.sleep(0.005)
            now = _samples_now(self.clock, self.rate)
            if now is None or self.out is None:
                continue
            target = now - self.latency
            if self._play_ts is None or not 0 <= target - self._play_ts < self.rate // 2:
                self._play_ts = target
            n = target - self._play_ts
            if n > 0:
                self.out.write(self.ring.read(self._play_ts, n, clear=True))
                self._play_ts = target

    def stop(self):
        self._stop.set()
        for f in list(self.flows.values()):
            self.remove_flow(f)
