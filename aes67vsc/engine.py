"""Runs a whole setup: one PTP clock, N send streams, N auto-connecting receive streams."""
from __future__ import annotations

import json
import os
import random
import socket
import sys
import threading
import time
import zlib
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import iface, sap, sdp
from .ptp import Clock


def config_path() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA", Path.home()))
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "aes67-vsc" / "config.json"


@dataclass
class SendCfg:
    name: str
    channels: int = 2
    device: str | int | None = None
    tone: float | None = None          # Hz, test tone instead of a device
    rate: int = 48000
    encoding: str = "L24"
    ptime: float = 1.0
    address: str | None = None         # default: derived from IP + stream index
    port: int = 5004


@dataclass
class RecvCfg:
    stream: str                        # announced stream name
    device: str | int | None = None
    latency_ms: float = 10
    null: bool = False                 # no audio output (testing)


@dataclass
class Config:
    interface: str = "auto"
    ptp_domain: int = 0
    ptp_can_lead: bool = True
    ravenna: bool = True               # mDNS/RTSP discovery + advertisement
    nmos: bool = True                  # NMOS IS-04/IS-05 node
    sends: list[SendCfg] = field(default_factory=list)
    receives: list[RecvCfg] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if not path.exists():
            return cls(sends=[SendCfg(socket.gethostname()[:28] or "aes67-vsc")],
                       receives=[RecvCfg("")])  # empty slot: patch it from NMOS or the UI
        d = json.loads(path.read_text())
        known = {f for f in cls.__dataclass_fields__ if f not in ("sends", "receives")}
        return cls(**{k: v for k, v in d.items() if k in known},
                   sends=[SendCfg(**s) for s in d.get("sends", [])],
                   receives=[RecvCfg(**r) for r in d.get("receives", [])])

    def save(self, path: Path | None = None):
        path = path or config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


def default_address(ip: str, index: int, name: str) -> str:
    """Stable multicast address in Dante's default AES67 range 239.69.0.0/16."""
    h = zlib.crc32(f"{ip}/{index}/{name}".encode())
    return f"239.69.{(h >> 8) & 0xFF}.{(h & 0xFE) + 1}"


class Engine:
    def __init__(self, cfg: Config, log=print):
        self.cfg, self.log = cfg, log
        self.ip = None
        self.clock: Clock | None = None
        self.browser: sap.Browser | None = None
        self.senders = []
        self.receivers: dict[int, tuple] = {}  # index -> (Receiver, sink, Stream)
        self.overrides: dict[int, sdp.Stream | None] = {}  # slot -> stream set by NMOS/UI
        self.ravenna = self.nmos = None
        self._stop = threading.Event()
        self._kick = threading.Event()
        self._lock = threading.Lock()

    def start(self):
        c = self.cfg
        self.ip = iface.choose() if c.interface in ("", "auto") else c.interface
        self.log(f"Network interface: {self.ip}")
        self.clock = Clock(self.ip, c.ptp_domain, c.ptp_can_lead)
        if not self.clock.start():
            self.log(f"! PTP unavailable ({self.clock.error}). Run as admin/sudo for clock sync.")
        else:
            self.clock.wait_settled(4.0)
            self.log(f"PTP: {self.clock.state} (grandmaster {self.clock.gm_id})")
        self.clock.on_change(lambda: self.log(f"PTP: {self.clock.state} (grandmaster {self.clock.gm_id})"))
        self.browser = sap.Browser(self.ip).start()
        if c.ravenna:
            try:
                from .ravenna import Ravenna
                self.ravenna = Ravenna(self.ip)
            except Exception as e:  # noqa: BLE001
                self.log(f"! RAVENNA/mDNS discovery unavailable: {e}")
        for i, s in enumerate(c.sends):
            self._start_send(i, s)
        if c.nmos:
            try:
                from .nmos import Node
                self.nmos = Node(self, zc=self.ravenna.zc if self.ravenna else None)
                self.log(f"NMOS node API: http://{self.ip}:{self.nmos.port}/x-nmos/")
            except Exception as e:  # noqa: BLE001
                self.log(f"! NMOS unavailable: {e}")
        threading.Thread(target=self._watch, daemon=True, name="engine").start()

    def _start_send(self, i: int, s: SendCfg):
        from .audio import DeviceSource, ToneSource
        from .sender import Sender
        st = sdp.Stream(s.name, s.address or default_address(self.ip, i, s.name), s.port, s.encoding,
                        s.rate, s.channels, s.ptime, 96, self.ip, random.getrandbits(31),
                        self.clock.gm_id or "00-00-00-00-00-00-00-00", self.cfg.ptp_domain)
        try:
            src = ToneSource(s.channels, s.rate, s.tone) if s.tone else DeviceSource(s.channels, s.rate, s.device)
        except Exception as e:  # noqa: BLE001 - sound card errors vary by platform
            self.log(f"! send {s.name!r}: can't open input device: {e}")
            return
        tx = Sender(st, src, self.clock, self.ip)
        tx.start()
        self.senders.append(tx)
        if self.ravenna:
            self.ravenna.advertise(st)
        self.log(f"Sending {s.name!r} -> {st.address}:{st.port} {s.encoding}/{s.rate}/{s.channels}ch")

    def _watch(self):
        """Connect receivers when their stream appears; reconnect if it moves."""
        while not self._stop.is_set():
            found = {s.name: s for s in self.discovered()}
            for i, r in enumerate(self.cfg.receives):
                st = self.overrides[i] if i in self.overrides else found.get(r.stream)
                cur = self.receivers.get(i)
                if st and cur and (cur[2].address, cur[2].port, cur[2].channels, cur[2].rate, cur[2].encoding) \
                        == (st.address, st.port, st.channels, st.rate, st.encoding):
                    continue
                if cur:
                    self._stop_recv(i)
                if st:
                    self._start_recv(i, r, st)
            self._kick.wait(1.0)
            self._kick.clear()

    def set_override(self, i: int, stream: sdp.Stream | None):
        """Connect slot i to a specific stream (NMOS IS-05 / UI); None disconnects."""
        self.overrides[i] = stream
        self._kick.set()

    def discovered(self) -> list[sdp.Stream]:
        """All streams seen via SAP (Dante, most AES67) and RAVENNA mDNS/RTSP."""
        out = {s.name: s for s in (self.ravenna.list() if self.ravenna else [])}
        out.update({s.name: s for s in self.browser.list()})
        return list(out.values())

    def _start_recv(self, i: int, r: RecvCfg, st: sdp.Stream):
        from .receiver import Receiver
        rx = Receiver(st, self.ip, r.latency_ms)
        rx.start()
        sink = None
        if not r.null:
            from .audio import DeviceSink
            try:
                sink = DeviceSink(rx.buf, st.rate, r.device)
            except Exception as e:  # noqa: BLE001
                self.log(f"! receive {st.name!r}: can't open output device: {e}")
        with self._lock:
            self.receivers[i] = (rx, sink, st)
        self.log(f"Receiving {st.name!r} <- {st.address}:{st.port} {st.encoding}/{st.rate}/{st.channels}ch")

    def _stop_recv(self, i: int):
        with self._lock:
            rx, sink, _ = self.receivers.pop(i)
        rx.stop()
        if sink:
            sink.close()

    def status(self) -> str:
        parts = [f"ptp={self.clock.state}" + ("" if self.clock.locked else "(unsynced)")]
        parts += [f"tx[{t.stream.name}]={t.packets}" for t in self.senders]
        with self._lock:
            for rx, _, st in self.receivers.values():
                parts.append(f"rx[{st.name}]={rx.packets} lost={rx.lost} "
                             f"drift={(rx.buf.ratio - 1) * 1e6:+.0f}ppm")
        return "  ".join(parts)

    def stop(self):
        self._stop.set()
        self._kick.set()
        if self.nmos:
            self.nmos.close()
        for t in self.senders:
            t.stop()
        for i in list(self.receivers):
            self._stop_recv(i)
        if self.browser:
            self.browser.stop()
        if getattr(self, "ravenna", None):
            self.ravenna.close()
        if self.clock:
            self.clock.stop()
