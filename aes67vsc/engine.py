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
class DanteCfg:
    """Appear as a native Dante device in Dante Controller (unofficial, reverse-engineered)."""
    enabled: bool = False
    name: str = ""                     # default: computer name
    tx_channels: int = 2               # channels this computer sends to Dante
    rx_channels: int = 2               # channels this computer receives from Dante
    tx_device: str | int | None = "virtual"
    rx_device: str | int | None = "virtual"
    latency_ms: float = 10


@dataclass
class Config:
    interface: str = "auto"
    ptp_domain: int = 0
    ptp_can_lead: bool = True
    ravenna: bool = True               # mDNS/RTSP discovery + advertisement
    nmos: bool = True                  # NMOS IS-04/IS-05 node
    sends: list[SendCfg] = field(default_factory=list)
    receives: list[RecvCfg] = field(default_factory=list)
    dante: DanteCfg = field(default_factory=DanteCfg)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = path or config_path()
        if not path.exists():
            return cls(sends=[SendCfg(socket.gethostname()[:28] or "aes67-vsc", device="virtual")],
                       receives=[RecvCfg("", device="virtual")])  # empty slot: patch it from NMOS or the UI
        d = json.loads(path.read_text())
        return cls.from_dict(d)

    @classmethod
    def from_dict(cls, d: dict) -> "Config":
        known = {f for f in cls.__dataclass_fields__ if f not in ("sends", "receives", "dante")}
        return cls(**{k: v for k, v in d.items() if k in known},
                   sends=[SendCfg(**s) for s in d.get("sends", [])],
                   receives=[RecvCfg(**r) for r in d.get("receives", [])],
                   dante=DanteCfg(**d.get("dante", {})))

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
        self.dante = None
        if c.dante.enabled:
            self._start_dante()
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
            if s.tone:
                src = ToneSource(s.channels, s.rate, s.tone)
            elif s.device == "virtual":
                from . import virtual
                src = virtual.resolve("send", s.name, s.channels, s.rate, self.log)
                if src is None:
                    return
            else:
                src = DeviceSource(s.channels, s.rate, s.device)
        except Exception as e:  # noqa: BLE001 - sound card errors vary by platform
            self.log(f"! send {s.name!r}: can't open input device: {e}")
            return
        tx = Sender(st, src, self.clock, self.ip)
        tx.start()
        self.senders.append(tx)
        if self.ravenna:
            self.ravenna.advertise(st)
        self.log(f"Sending {s.name!r} -> {st.address}:{st.port} {s.encoding}/{s.rate}/{s.channels}ch")

    def _start_dante(self):
        from .audio import DeviceSink, DeviceSource
        from .dante.device import DanteDevice, DeviceInfo, dante_name
        from .elastic import ElasticBuffer
        d, rate = self.cfg.dante, 48000
        name = dante_name(d.name or socket.gethostname().split(".")[0])
        try:
            src = None
            if d.tx_channels > 0:
                if d.tx_device == "virtual":
                    from . import virtual
                    src = virtual.resolve("send", name + " Dante", d.tx_channels, rate, self.log)
                else:
                    src = DeviceSource(d.tx_channels, rate, d.tx_device)
            out = ElasticBuffer(max(1, d.rx_channels), rate, int(rate * 0.02)) if d.rx_channels > 0 else None
            self._dante_sink = None
            if out is not None:
                if d.rx_device == "virtual":
                    from . import virtual
                    make = virtual.resolve("recv", name + " Dante", d.rx_channels, rate, self.log)
                    self._dante_sink = make(out) if make else None
                else:
                    self._dante_sink = DeviceSink(out, rate, d.rx_device)
            info = DeviceInfo(name, self.ip, [f"RX {k + 1}" for k in range(d.rx_channels)],
                              [f"TX {k + 1}" for k in range(d.tx_channels)], rate=rate,
                              latency_ns=int(d.latency_ms * 1_000_000))
            self.dante = DanteDevice(info, self.clock, src, out, zc=self.ravenna.zc if self.ravenna else None,
                                     log=self.log)
            self.dante.start()
            if self.clock.state == "leader":
                self.log("! Dante: no Dante clock leader found. Native Dante audio needs a Dante device "
                         "(or Dante Virtual Soundcard/Via) on the network to provide the clock.")
        except OSError as e:
            self.log(f"! Dante: can't start ({e}). Is Dante Virtual Soundcard or Dante Via running on this "
                     "computer? They use the same ports; quit them first.")
            self.dante = None
        except Exception as e:  # noqa: BLE001
            self.log(f"! Dante: can't start: {e}")
            self.dante = None

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
                if r.device == "virtual":
                    from . import virtual
                    make = virtual.resolve("recv", r.stream or st.name, st.channels, st.rate, self.log)
                    sink = make(rx.buf) if make else None
                else:
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
        parts = [f"ptp={self.clock.state}" + (f"/v{self.clock.version}" if self.clock.state == "follower" else "")
                 + ("" if self.clock.locked else "(unsynced)")]
        if getattr(self, "dante", None):
            subs = sum(1 for s in self.dante.subs if s)
            parts.append(f"dante[tx flows={len(self.dante.tx.flows) if self.dante.tx else 0} rx subs={subs}]")
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
        if getattr(self, "dante", None):
            self.dante.stop()
            if getattr(self, "_dante_sink", None):
                self._dante_sink.close()
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
        try:
            from . import virtual
            virtual.cleanup()
        except Exception:  # noqa: BLE001
            pass
