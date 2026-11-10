"""A virtual Dante device that shows up in Dante Controller.

Ported from Inferno (GPL-3.0-or-later): device_server/{mdns_server, arc_server,
cmc_server, info_mcast_server, flows_control_server, channels_subscriber,
tx_multicasts, saved_settings}.rs. Additional message formats (device settings,
property directory, sample-rate/encoding status, rename) follow netaudio
(https://github.com/chris-ritsen/network-audio-controller, public domain).
"""
from __future__ import annotations

import json
import os
import re
import socket
import struct
import threading
import time
import uuid
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from . import proto
from .flows import (FPP_MAX, FPP_MAX_ADVERTISED, FPP_MIN, MAX_CHANNELS_IN_FLOW, MAX_FLOWS, MAX_PAYLOAD,
                    MULTICAST_MEDIA_PORT, Receiver, Transmitter)
from .proto import Buf, HEADER, serialize_items, start_index

CHAN_TYPE = "_netaudio-chan._udp.local."
BUND_TYPE = "_netaudio-bund._udp.local."
ARC_TYPE, CMC_TYPE, DBC_TYPE = ("_netaudio-arc._udp.local.", "_netaudio-cmc._udp.local.",
                                "_netaudio-dbc._udp.local.")
INFO_GROUP, INFO_PORT = "224.0.0.231", 8702
HEARTBEAT_GROUP, HEARTBEAT_PORT = "224.0.0.233", 8708

# subscription status values shown by Dante Controller
UNRESOLVED, TX_FAIL, RECEIVING_UNICAST, RECEIVING_MULTICAST = 1, 0x15, 0x01010009, 0x0101000A
RECEIVING = (RECEIVING_UNICAST, RECEIVING_MULTICAST)

SAMPLE_RATES = [44100, 48000, 88200, 96000]
ENCODINGS = [16, 24, 32]
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,30}$")


def dante_name(s: str) -> str:
    """Dante device names: letters, digits, hyphens; max 31 chars (Controller ignores longer)."""
    s = re.sub(r"[^A-Za-z0-9-]+", "-", s).strip("-")
    return (s or "aes67-vsc")[:31]


def channel_name_ok(s: str) -> bool:
    return 0 < len(s) <= 31 and not any(c in s for c in "=.@\0")


@dataclass
class Subscription:
    tx_channel: str
    tx_host: str
    status: int = UNRESOLVED


@dataclass
class DeviceInfo:
    name: str
    ip: str
    rx_names: list[str]
    tx_names: list[str]                 # friendly TX names (renamable); factory names are "01", "02", ...
    rate: int = 48000
    bits: int = 24
    latency_ns: int = 10_000_000        # receive latency ("device latency" in Dante Controller)
    process_id: int = 0
    mac: bytes = field(default_factory=lambda: uuid.getnode().to_bytes(6, "big"))
    manufacturer: str = "aes67-vsc"
    board: str = "aes67-vsc"
    model: str = "aes67-vsc virtual soundcard"
    model_number: str = "_000000000000000b"
    vendor: str = "Audinate"
    pcm_type: int = 0xE                 # bitmask of supported sample sizes: 2, 3 and 4 bytes
    arc_port: int = proto.ARC_PORT
    cmc_port: int = proto.CMC_PORT
    flows_port: int = proto.FLOWS_CONTROL_PORT
    info_port: int = proto.INFO_REQUEST_PORT

    @property
    def device_id(self) -> bytes:
        return b"\0\0" + socket.inet_aton(self.ip) + struct.pack("!H", self.process_id)

    @property
    def factory_name(self) -> str:
        return f"aes67vsc-{self.device_id.hex()}"

    def tx_factory(self, idx: int) -> str:
        return f"{idx + 1:02d}"


class _Mdns:
    """Runs zeroconf register/update/unregister on its event loop: different names in parallel
    (each registration probes for ~1 s), operations on the same name strictly in order."""

    def __init__(self, zc):
        self.zc = zc
        self.services: dict[str, object] = {}
        self._tail: dict[str, object] = {}
        self._lock = threading.Lock()

    def _submit(self, op, name, info=None):
        import asyncio
        with self._lock:
            prev = self._tail.get(name)

            async def run():
                if prev is not None:
                    try:
                        await asyncio.wrap_future(prev)
                    except Exception:  # noqa: BLE001
                        pass
                old = self.services.pop(name, None)
                try:
                    if op == "set":
                        if old is not None:
                            await self.zc.async_update_service(info)
                        else:
                            await self.zc.async_register_service(info)
                        self.services[name] = info
                    elif old is not None:
                        await self.zc.async_unregister_service(old)
                except Exception:  # noqa: BLE001 - mDNS hiccups must not kill the device
                    pass

            try:
                self._tail[name] = asyncio.run_coroutine_threadsafe(run(), self.zc.loop)
            except RuntimeError:   # loop already closed
                pass

    def set(self, info):
        self._submit("set", info.name, info)

    def remove(self, name: str):
        self._submit("remove", name)

    def remove_all(self):
        with self._lock:
            names = list(self._tail)
        for name in names:
            self.remove(name)

    def flush(self, timeout=5.0):
        end = time.monotonic() + timeout
        with self._lock:
            futs = list(self._tail.values())
        for f in futs:
            try:
                f.result(max(0.0, end - time.monotonic()))
            except Exception:  # noqa: BLE001
                pass

    def stop(self):
        self.remove_all()
        self.flush()


class DanteDevice:
    def __init__(self, info: DeviceInfo, clock, source=None, out_buffer=None, zc=None, log=print,
                 state_path: Path | None = None, on_reconfigure=None):
        self.info, self.clock, self.log = info, clock, log
        self.state_path = state_path
        self.on_reconfigure = on_reconfigure    # called with rate=... when a controller changes sample rate
        self.subs: list[Subscription | None] = [None] * len(info.rx_names)
        self.bundles: dict[int, tuple[str, list]] = {}   # multicast TX: flow index -> (group, channels)
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._mcast_seq = 1
        self._socks = []
        self._threads = []
        self._lock = threading.RLock()
        self._load_state()
        self.tx = Transmitter(clock, info.rate, len(info.tx_names), source, info.ip) if source else None
        self.rx = Receiver(clock, info.rate, len(info.rx_names), info.ip, out_buffer)
        self.rx.latency = max(self.rx.latency, info.latency_ns * info.rate // 1_000_000_000)
        self._rx_flows_by_key: dict[str, list] = {}
        self._fc_seq = 1
        from zeroconf import IPVersion, Zeroconf
        self._own_zc = zc is None
        self.zc = zc or Zeroconf(interfaces=[info.ip], ip_version=IPVersion.V4Only)
        self.mdns = _Mdns(self.zc)

    # ================================================================ persistence
    def _load_state(self):
        i = self.info
        try:
            s = json.loads(self.state_path.read_text()) if self.state_path else {}
        except (OSError, ValueError):
            s = {}
        self._saved_bundles = s.get("bundles", [])
        if NAME_RE.match(s.get("name", "") or ""):
            i.name = s["name"]
        for key, names in (("rx_names", i.rx_names), ("tx_names", i.tx_names)):
            for k, n in enumerate(s.get(key, [])[:len(names)]):
                if isinstance(n, str) and channel_name_ok(n):
                    names[k] = n
        for k, sub in enumerate(s.get("subs", [])[:len(self.subs)]):
            if sub and len(sub) == 2:
                self.subs[k] = Subscription(sub[0], sub[1])
        if s.get("latency_ns"):
            i.latency_ns = int(s["latency_ns"])
        if s.get("bits") in ENCODINGS:
            i.bits = s["bits"]

    def _save_state(self):
        if not self.state_path:
            return
        i = self.info
        s = {"name": i.name, "rx_names": i.rx_names, "tx_names": i.tx_names, "latency_ns": i.latency_ns,
             "bits": i.bits, "subs": [[x.tx_channel, x.tx_host] if x else None for x in self.subs],
             "bundles": [{"id": k + 1, "addr": g, "channels": [0 if c is None else c + 1 for c in ch]}
                         for k, (g, ch) in sorted(self.bundles.items())]}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(s, indent=1))
            os.replace(tmp, self.state_path)
        except OSError as e:
            self.log(f"Dante: can't save state: {e}")

    # ================================================================ lifecycle
    def start(self):
        i = self.info
        for port, handler, name in ((i.arc_port, self._arc, "arc"), (i.cmc_port, self._cmc, "cmc"),
                                    (i.flows_port, self._flow_control, "flows")):
            s = self._udp(port)
            t = threading.Thread(target=self._serve, args=(s, handler), daemon=True, name=f"dante-{name}")
            t.start()
            self._threads.append(t)
        self._info_sock = self._udp(i.info_port)
        self._mc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._mc.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(i.ip))
        self._mc.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        if self.tx:
            for b in self._saved_bundles:  # restore multicast transmit flows
                try:
                    self._add_bundle(int(b["id"]) - 1, [None if c == 0 else c - 1 for c in b["channels"]],
                                     b.get("addr"), save=False)
                except (RuntimeError, KeyError, ValueError, TypeError):
                    pass
        threading.Thread(target=self._info_loop, daemon=True, name="dante-info").start()
        threading.Thread(target=self._subscriber_loop, daemon=True, name="dante-subs").start()
        self._advertise()
        self._dirty.set()
        self.log(f"Dante: device \"{i.name}\" online ({len(i.tx_names)} TX / {len(i.rx_names)} RX channels, "
                 f"{i.rate} Hz)")

    def stop(self):
        self._stop.set()
        self._dirty.set()
        self.mdns.stop()
        if self._own_zc:
            self.zc.close()
        for key in list(self._rx_flows_by_key):
            self._drop_flows(key)
        if self.tx:
            self.tx.stop()
        self.rx.stop()
        for t in self._threads:    # a socket stays bound while a thread is still blocked in recvfrom
            t.join(1.5)
        for s in self._socks:
            s.close()

    def _udp(self, port: int) -> socket.socket:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.bind((self.info.ip, port))
        s.settimeout(0.5)
        self._socks.append(s)
        return s

    def _serve(self, s, handler):
        while not self._stop.is_set():
            try:
                pkt, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            p = proto.parse_packet(pkt)
            if not p or p[3] != 0:  # opcode2 must be 0 in requests
                continue
            start, seq, op1, _, content = p
            try:
                r = handler(op1, content, pkt)
            except Exception as e:  # noqa: BLE001 - never let a malformed packet kill the server
                self.log(f"Dante: error handling opcode {op1:#06x}: {e}")
                r = (0xFFFF, b"")
            if r is not None:
                code, body = r if isinstance(r, tuple) else (proto.CODE_OK, r)
                try:
                    s.sendto(proto.make_packet(start, seq, op1, code, body), addr)
                except OSError:
                    pass

    # ================================================================ mDNS
    def _svc(self, type_, inst, port, props):
        from zeroconf import ServiceInfo
        return ServiceInfo(type_, f"{inst}.{type_}", port=port, addresses=[socket.inet_aton(self.info.ip)],
                           server=f"{self.info.name}.local.", properties=props, host_ttl=120, other_ttl=4500)

    def _chan_names(self, idx: int) -> list[tuple[str, bool]]:
        """Each TX channel is advertised under its factory name (default) and, if different, its label."""
        i = self.info
        out = [(i.tx_factory(idx), True)]
        if i.tx_names[idx] != i.tx_factory(idx):
            out.append((i.tx_names[idx], False))
        return out

    def _advertise_channel(self, idx: int):
        i = self.info
        for name, default in self._chan_names(idx):
            props = {"txtvers": "2", "dbcp1": "0x1102", "dbcp": "0x1004", "id": str(idx + 1), "rate": str(i.rate),
                     "pcm": f"{i.bits // 8} {i.pcm_type:x}", "enc": str(i.bits), "en": str(i.bits),
                     "latency_ns": str(i.latency_ns), "fpp": f"{FPP_MAX_ADVERTISED},{FPP_MIN}",
                     "nchan": str(min(MAX_CHANNELS_IN_FLOW, len(i.tx_names)))}
            if default:
                props["default"] = None
            for fidx, (_, chans) in self.bundles.items():
                if idx in chans:
                    props[f"b.{fidx + 1}"] = str(chans.index(idx) + 1)
                    break
            self.mdns.set(self._svc(CHAN_TYPE, f"{name}@{i.name}", i.flows_port, props))

    def _advertise(self):
        i = self.info
        self.mdns.set(self._svc(ARC_TYPE, i.name, i.arc_port, {
            "arcp_vers": "2.7.41", "arcp_min": "0.2.4", "router_vers": "4.0.2", "router_info": i.board,
            "mf": i.manufacturer, "model": i.model_number}))
        self.mdns.set(self._svc(CMC_TYPE, i.name, i.cmc_port, {
            "id": i.device_id.hex(), "process": str(i.process_id), "cmcp_vers": "1.2.0", "cmcp_min": "1.0.0",
            "server_vers": "4.0.2", "channels": "0x6000004d", "mf": i.manufacturer, "model": i.model_number}))
        if self.tx:
            self.mdns.set(self._svc(DBC_TYPE, i.name, i.flows_port, {"dbcp1": "0x1102", "dbcp": "0x1004"}))
            for idx in range(len(i.tx_names)):
                self._advertise_channel(idx)
            for fidx in self.bundles:
                self._advertise_bundle(fidx)

    def _advertise_bundle(self, fidx: int):
        i = self.info
        group, chans = self.bundles[fidx]
        self.mdns.set(self._svc(BUND_TYPE, f"{fidx + 1}@{i.name}", i.flows_port, {
            "txtvers": "1", "id": str(fidx + 1), "nchan": str(len(chans)), "latency_ns": str(i.latency_ns),
            "fpp": str(FPP_MAX_ADVERTISED), "rate": str(i.rate), "enc": str(i.bits), "a.0": group,
            "p.0": str(MULTICAST_MEDIA_PORT)}))

    def rename(self, new_name: str) -> bool:
        """Rename the device (from Dante Controller or the UI): everything is re-announced."""
        if not NAME_RE.match(new_name):
            return False
        if new_name != self.info.name:
            self.mdns.remove_all()
            self.mdns.flush(3)
            self.info.name = new_name
            self._advertise()
            self._save_state()
            self.log(f"Dante: device renamed to \"{new_name}\"")
        return True

    # ================================================================ multicast transmit flows
    def _random_group(self, salt: int) -> str:
        h = zlib.crc32(self.info.device_id + struct.pack("!IQ", salt, time.time_ns()))
        return f"239.255.{(h >> 8) & 0xFF}.{h & 0xFF}"

    def _add_bundle(self, fidx: int, chans: list, group: str | None = None, save=True):
        if not self.tx:
            raise RuntimeError("no transmitter")
        if any(c is not None and c >= len(self.info.tx_names) for c in chans) or not chans:
            raise RuntimeError("bad channel")
        used = {g for g, _ in self.bundles.values()}
        while not group or group in used:
            group = self._random_group(fidx)
        self.tx.add_flow((group, MULTICAST_MEDIA_PORT), chans, FPP_MAX_ADVERTISED, self.info.bits // 8,
                         index=fidx, multicast=True)
        self.bundles[fidx] = (group, chans)
        self._advertise_bundle(fidx)
        for c in chans:
            if c is not None:
                self._advertise_channel(c)
        if save:
            self._save_state()
        self.log(f"Dante: multicast flow {fidx + 1} -> {group} ({len(chans)} ch)")

    def _remove_bundle(self, fidx: int) -> bool:
        b = self.bundles.pop(fidx, None)
        if not b:
            return False
        f = self.tx.flows.get(fidx) if self.tx else None
        if f:
            self.tx.remove_flow(f)
        self.mdns.remove(f"{fidx + 1}@{self.info.name}.{BUND_TYPE}")
        for c in b[1]:
            if c is not None:
                self._advertise_channel(c)
        self._save_state()
        return True

    # ================================================================ CMC
    def _cmc(self, op1, content, pkt):
        if op1 == 0x1001:  # device advertisement request
            i = self.info
            return struct.pack("!H8sHH4sHH", i.process_id, i.device_id, 1, 0, socket.inet_aton(i.ip),
                               i.info_port, 0)
        return None

    # ================================================================ ARC (Dante Controller's main protocol)
    def _arc(self, op1, c, pkt):
        i = self.info
        common_off = [0]

        def common(buf):
            if not common_off[0]:
                common_off[0] = buf.wpos
                buf.write(struct.pack("!IBBHHHHH", i.rate, 1, 1, i.bits, 0x400, i.bits, i.bits, i.pcm_type))
            return common_off[0]

        def page(space, item_size, items, fn):
            st = start_index(c)
            if st is None:
                return 0xFFFF, b""
            more, body = serialize_items(space, item_size, items[st:], fn)
            return (proto.MORE_PAGES if more else proto.CODE_OK), body

        if op1 == 0x1000:  # channel & flow counts
            ntx, nrx = len(i.tx_names), len(i.rx_names)
            return struct.pack("!BBHHHHHHHHHH12s", 0, 0x30, ntx, nrx, 4, min(MAX_CHANNELS_IN_FLOW, max(ntx, 1)),
                               8, MAX_FLOWS, MAX_FLOWS, ntx + nrx, 1, 1, b"")
        if op1 == 0x1001:  # set device name (empty = reset to factory name)
            new = proto.read_cstr(c, 0) if c else ""
            new = new or dante_name(i.factory_name)
            if not NAME_RE.match(new):
                return 0x0022, b""
            threading.Thread(target=self.rename, args=(new,), daemon=True).start()
            return b""
        if op1 == 0x1002:  # device name
            return i.name.encode() + b"\0"
        if op1 == 0x1003:  # device names
            b = Buf()
            b.write(b"\0" * 38)
            fr = b.wpos + HEADER
            b.cstr(i.name)
            fa = b.wpos + HEADER
            b.cstr(i.factory_name)
            bo = b.wpos + HEADER
            b.cstr(i.board)
            rv = b.wpos + HEADER
            b.write(b":705\0")
            b.b[0:38] = struct.pack("!HHHHHHHHH12sHHHH", 0, 0, 0, bo, rv, 0x0500, fr, fa, fr, b"", 0x2729, 0,
                                    0x1102, 0)
            return b.bytes()
        if op1 == 0x1100:  # device settings: sample rate and latencies, as [n, n, (property, pointer)*n, values]
            lat = i.latency_ns
            settings = [(0x8020, i.rate), (0x8204, lat), (0x8205, lat), (0x8301, lat), (0x8302, 40_000_000),
                        (0x8306, 1_000_000)]
            first = HEADER + 2 + 4 * len(settings)
            body = bytes([len(settings), len(settings)])
            body += b"".join(struct.pack("!HH", p, first + 4 * k) for k, (p, _) in enumerate(settings))
            return body + b"".join(struct.pack("!I", v) for _, v in settings)
        if op1 == 0x1101:  # set latency: 22-byte preamble, then latency (ns) twice
            if len(c) < 26:
                return 0x0022, b""
            lat = struct.unpack_from("!I", c, 22)[0]
            if not 1_000_000 <= lat <= 40_000_000:
                return 0x0022, b""
            self.set_latency(lat)
            # reply: property write-back (configured/RX latency by pointer, frames-per-packet inline)
            props = [(0x8205, None), (0x0211, 4), (0x8301, None), (0x0310, 4)]
            body, ptr = bytes([len(props), 4]), 12 + 4 * len(props)
            for p, inline in props:
                body += struct.pack("!HH", p, ptr if inline is None else inline)
                ptr += 4 if inline is None else 0
            return body + struct.pack("!II", lat, lat)
        if op1 == 0x1102:  # property directory: which settings exist (id, flags)
            props = [(0x8020, 1), (0x8021, 3), (0x0022, 3), (0x0023, 3), (0x0024, 1), (0x0201, 3), (0x8204, 3),
                     (0x8205, 3), (0x020A, 1), (0x020B, 1), (0x0210, 3), (0x0211, 3), (0x0212, 3), (0x0213, 1),
                     (0x0214, 1), (0x8301, 3), (0x8306, 1), (0x8302, 1), (0x0310, 1), (0x0311, 1), (0x0303, 3),
                     (0x83F0, 1), (0x0601, 1)]
            return struct.pack("!H", len(props)) + b"".join(struct.pack("!HH", p, f) for p, f in props)
        if op1 == 0x3000:  # receive channels + their subscriptions
            def rx_item(item, buf):
                idx, name = item
                co = common(buf)
                sub = self.subs[idx]
                tco = buf.cstr(sub.tx_channel) if sub else 0
                tho = buf.cstr(sub.tx_host) if sub else 0
                fo = buf.cstr(name)
                return struct.pack("!HHHHHHII", idx + 1, 6, co, tco, tho, fo, sub.status if sub else 0, 0)
            return page(min(len(i.rx_names), 32), 20, list(enumerate(i.rx_names)), rx_item)
        if op1 == 0x2000:  # transmit channels (factory names)
            def tx_item(idx, buf):
                co = common(buf)
                return struct.pack("!HHHH", idx + 1, 7, co, buf.cstr(i.tx_factory(idx)))
            return page(min(len(i.tx_names), 32), 8, list(range(len(i.tx_names))), tx_item)
        if op1 == 0x2010:  # transmit channel labels
            wrote = [False]

            def fn_item(item, buf):
                if not wrote[0]:
                    buf.u32(0)
                    wrote[0] = True
                idx, name = item
                return struct.pack("!HHH", idx + 1, idx + 1, buf.cstr(name))
            return page(min(len(i.tx_names), 32), 6, list(enumerate(i.tx_names)), fn_item)
        if op1 == 0x3001:  # rename RX channels
            changed = []
            for it in proto.deserialize_items(c, 4):
                ch, off = struct.unpack("!HH", it)
                name = proto.read_cstr(c, off - HEADER) if off >= HEADER else None
                if ch and name and ch <= len(i.rx_names) and channel_name_ok(name):
                    i.rx_names[ch - 1] = name
                    changed.append(ch - 1)
            if changed:
                self._save_state()
                self._notify_channels(changed)
            return (proto.CODE_OK if changed else 0xFFFF), b""
        if op1 == 0x2013:  # rename TX channels: both names stay resolvable (factory + new label)
            changed = []
            for it in proto.deserialize_items(c, 6):
                _, ch, off = struct.unpack("!HHH", it)
                if not 0 < ch <= len(i.tx_names):
                    continue
                name = (proto.read_cstr(c, off - HEADER) if off >= HEADER else None) or i.tx_factory(ch - 1)
                if not channel_name_ok(name) or (name in i.tx_names and i.tx_names.index(name) != ch - 1):
                    continue
                for old, default in self._chan_names(ch - 1):
                    if not default:
                        self.mdns.remove(f"{old}@{i.name}.{CHAN_TYPE}")
                i.tx_names[ch - 1] = name
                self._advertise_channel(ch - 1)
                changed.append(ch)
            if changed:
                self._save_state()
            return (proto.CODE_OK, b"\0\0") if changed else (0xFFFF, b"")
        if op1 == 0x3010:  # set subscriptions (Dante Controller routing grid)
            changed = []
            for it in proto.deserialize_items(c, 6):
                ch, tco, tho = struct.unpack("!HHH", it)
                if not 0 < ch <= len(i.rx_names):
                    continue
                if tco and tho:
                    txc = proto.read_cstr(c, tco - HEADER) if tco >= HEADER else None
                    txh = proto.read_cstr(c, tho - HEADER) if tho >= HEADER else None
                    if txc and txh:
                        self.log(f"Dante: RX {ch} <- {txc}@{txh}")
                        self.subs[ch - 1] = Subscription(txc, txh)
                else:
                    self.subs[ch - 1] = None
                changed.append(ch - 1)
            self._subs_changed(changed)
            return b""
        if op1 == 0x3014:  # unsubscribe one channel
            ch = struct.unpack_from("!H", c, 4)[0]
            if 0 < ch <= len(i.rx_names):
                self.subs[ch - 1] = None
                self._subs_changed([ch - 1])
            return b""
        if op1 == 0x2200:  # our transmit flows
            flows = sorted(self.tx.flows.values(), key=lambda f: f.index) if self.tx else []

            def txf(f, buf):
                local = buf.cstr(f"{f.index + 1}_{i.process_id}")
                rh = buf.cstr_or_0(None if f.multicast else f.rx_host or None)
                rf = buf.cstr_or_0(None if f.multicast else f.rx_flow or None)
                buf.align(4)
                so = buf.wpos
                buf.write(struct.pack("!HH4s", 0x8002, f.dst[1], socket.inet_aton(f.dst[0])))
                no = buf.wpos
                buf.write(struct.pack("!HHHHHH8s", 0xA00, 1, rh, rf, 0x10, local, b""))
                mo = buf.wpos
                buf.write(struct.pack("!HHIHHHHH", f.index + 1, 2 if f.multicast else 0x11, i.rate, 0, f.bps * 8, 1,
                                      len(f.channels), so))
                for ch in f.channels:
                    buf.u16(0 if ch is None else ch + 1)
                buf.u16(no)
                return struct.pack("!H", mo)
            return page(16, 2, flows, txf)
        if op1 == 0x2201:  # create multicast transmit flows
            ids = []
            for (off,) in (struct.unpack("!H", it) for it in proto.deserialize_items(c, 2)):
                o = off - HEADER
                if o < 0 or o + 16 > len(c):
                    continue
                flow_id, flow_type = struct.unpack_from("!HH", c, o)
                n = struct.unpack_from("!H", c, o + 14)[0]
                if flow_type != 2 or not 0 < flow_id <= MAX_FLOWS or o + 16 + 2 * n > len(c):
                    continue
                chans = [None if x == 0 else x - 1 for x in struct.unpack_from(f"!{n}H", c, o + 16)]
                try:
                    self._add_bundle(flow_id - 1, chans)
                    ids.append(flow_id)
                except RuntimeError:
                    continue
            if not ids:
                return 0xFFFF, b""
            return struct.pack(f"!HH{len(ids)}H", len(ids), 0, *ids)
        if op1 == 0x2202:  # delete multicast transmit flows
            n = struct.unpack_from("!H", c, 0)[0]
            ok = [self._remove_bundle(x - 1) for x in struct.unpack_from(f"!{n}H", c, 4) if x]
            return b"" if any(ok) else (0xFFFF, b"")
        if op1 == 0x3200:  # our receive flows
            flows = sorted(self.rx.flows.values(), key=lambda f: f.index)

            def rxf(f, buf):
                buf.align(4)
                so = buf.wpos
                buf.write(struct.pack("!HH4s", 0x8002, f.port, socket.inet_aton(f.mcast[0] if f.mcast else i.ip)))
                d2 = buf.wpos
                buf.write(struct.pack("!HHHHII", 9, 1, 0x800, 0, f.latency * 1_000_000_000 // i.rate, 0))
                words = max(1, (len(i.rx_names) + 15) // 16)
                masks = []
                for locals_ in f.channel_map:
                    masks.append(buf.wpos)
                    for w in range(words):
                        buf.u16(sum(1 << (ch - w * 16) for ch in locals_ if w * 16 <= ch < (w + 1) * 16))
                buf.align(4)
                mo = buf.wpos
                buf.write(struct.pack("!HHIHHHHHH", f.index + 1, 1, i.rate, 0, f.bps * 8, 1, len(f.channel_map),
                                      words, so))
                for m in masks:
                    buf.u16(m)
                buf.u16(d2)
                return struct.pack("!H", mo)
            return page(16, 2, flows, rxf)
        if op1 == 0x2204:  # transmit flow labels: none
            return bytes([2, 0])
        if op1 == 0x2320:
            return 0x30, b""
        if op1 == 0x3300:  # receiver port ranges
            return bytes([0x38, 0x00, 0x38, 0xFD, 0x38, 0xFE, 0x38, 0xFF])
        return None

    def set_latency(self, latency_ns: int):
        i = self.info
        i.latency_ns = latency_ns
        self.rx.latency = max(i.rate // 1000, latency_ns * i.rate // 1_000_000_000)
        for idx in range(len(i.tx_names) if self.tx else 0):
            self._advertise_channel(idx)
        self._save_state()
        self.log(f"Dante: latency set to {latency_ns / 1e6:g} ms")

    def _subs_changed(self, indices):
        self._save_state()
        self._dirty.set()
        self._notify_channels(indices)

    # ================================================================ flow control (others request our audio)
    def _flow_control(self, op1, c, pkt):
        i = self.info
        if not self.tx:
            return 0x0315, b""
        if op1 == 0x0100 and len(c) > 16:  # request flow
            host_off, rate, bits, _one, nch, remote_off = struct.unpack_from("!HIIHHH", c, 0)
            if not 0 < nch <= 64 or len(c) < 16 + nch * 2 + 10 or len(pkt) < remote_off + 8:
                return 0x0302, b""
            chans = [None if x == 0 else x - 1 for x in struct.unpack_from(f"!{nch}H", c, 16)]
            o = 16 + nch * 2 + 6
            fpp, flow_name_off = struct.unpack_from("!HH", c, o)
            host, flow_name = proto.read_cstr(pkt, host_off), proto.read_cstr(pkt, flow_name_off)
            port = struct.unpack_from("!H", pkt, remote_off + 2)[0]
            ip = socket.inet_ntoa(pkt[remote_off + 4:remote_off + 8])
            if rate != i.rate:
                return 0x0301, b""  # sample rate mismatch
            if not FPP_MIN <= fpp <= FPP_MAX or bits not in ENCODINGS \
                    or any(ch is not None and ch >= len(i.tx_names) for ch in chans) \
                    or fpp * nch * bits // 8 > 1460:
                return 0x0302, b""
            try:
                f = self.tx.add_flow((ip, port), chans, fpp, bits // 8, host or "", flow_name or "")
            except RuntimeError:
                return 0x0315, b""
            self.log(f"Dante: sending {sum(c is not None for c in chans)} ch to {host} ({ip}:{port})")
            return f.handle
        if op1 == 0x0101 and len(c) >= 6:  # stop flow
            f = self.tx.by_handle(c[:6])
            if not f or f.multicast:
                return 0x0103, b""
            self.tx.remove_flow(f)
            return b""
        if op1 == 0x0102 and len(c) >= 8:  # update flow channels
            f = self.tx.by_handle(c[:6])
            if not f or f.multicast:
                return 0x0103, b""
            n = struct.unpack_from("!H", c, 6)[0]
            f.channels = [None if x == 0 else x - 1 for x in struct.unpack_from(f"!{n}H", c, 8)]
            return b""
        return None

    # ================================================================ info multicasts / heartbeat
    def _mcast(self, dst, start_code, opcode: bytes, content: bytes, also=None):
        i = self.info
        pkt = proto.make_mcast(start_code, self._mcast_seq, i.process_id, i.device_id,
                               proto.fixed_str(i.vendor, 8).replace(b"\0", b" "), opcode, content)
        self._mcast_seq = (self._mcast_seq + 1) & 0xFFFF or 1
        for d in (dst, also):
            if d:
                try:
                    self._mc.sendto(pkt, d)
                except OSError:
                    pass

    def _info(self, op: int, content: bytes, also=None):
        self._mcast((INFO_GROUP, INFO_PORT), 0xFFFF, bytes([7, 0x2A, 0, op, 0, 0, 0, 0]), content, also)

    def _notify_channels(self, indices):
        mask = bytearray(1)
        for ch in indices:
            if ch // 8 >= len(mask):
                mask.extend(bytes(ch // 8 + 1 - len(mask)))
            mask[ch // 8] |= 1 << (ch % 8)
        self._mcast((INFO_GROUP, INFO_PORT), 0xFFFF, bytes([7, 0x2A, 1, 2, 0, 0, 0, 0]),
                    struct.pack("!H", len(mask)) + bytes(mask))

    def _board_info(self, also=None):
        c = bytearray(200)
        c[0:8] = bytes([4, 1, 0, 6, 4, 1, 0, 3])
        c[0x16], c[0x23], c[0x27], c[0x28] = 0x10, 2, 1, 1
        c[0xBB] = 0x1F
        c[12:20] = proto.fixed_str(self.info.board, 8)
        c[0x38:0x48] = proto.fixed_str(self.info.board, 16)
        self._info(0x60, bytes(c), also)

    def _product_info(self, also=None):
        i = self.info
        c = bytearray(336)
        c[0:8] = proto.fixed_str(i.manufacturer, 8)
        c[8:16] = proto.fixed_str(i.board, 8)
        c[0x2C:0x3C] = proto.fixed_str(i.manufacturer, 16)
        c[0xAC:0xBC] = proto.fixed_str(i.model, 16)
        c[0x1C:0x20] = bytes([0, 1, 0, 0])
        self._info(0xC0, bytes(c), also)

    def _network_info(self, also=None):
        i = self.info
        mask, gw = _netmask_gateway(i.ip)
        c = bytes([0, 1, 0, 0, 0, 0]) + struct.pack("!HH", 1000, 1) + i.mac + socket.inet_aton(i.ip) \
            + socket.inet_aton(mask) + socket.inet_aton(gw) * 2 + bytes([0, 0x18, 0, 0x30]) + bytes(28)
        self._info(0x11, c, also)

    def _clock_stats(self, also=None):
        gm = self.clock.gm_id
        if not self.clock.locked or not gm:
            return
        g = bytes.fromhex(gm.replace("-", ""))
        c = bytes([0, 3, 0, 3, 0, 0, 0, 0x9F]) + struct.pack("!i", self._freq_ppb()) + self.info.mac + b"\0\0" \
            + g + g + bytes(76)
        self._info(0x20, c, also)

    def _audio_status(self, op: int, current: int, supported: list[int], also=None):
        """Sample-rate (0x80) / encoding (0x82) status: [0x18, count, current, 0, 0x00020000, values...]."""
        c = struct.pack("!HHIII", 0x18, len(supported), current, 0, 0x00020000)
        self._info(op, c + b"".join(struct.pack("!I", v) for v in supported), also)

    def _freq_ppb(self) -> int:
        return int(getattr(self.clock, "freq_ppb", 0) or 0)

    def _heartbeat(self):
        b = Buf()
        ctr = self._mcast_seq
        if self.clock.locked:
            b.write(struct.pack("!HHHHHHi", 16, 0x8001, 4, 4, ctr, 0, self._freq_ppb()))
            txp = self.tx.peaks() if self.tx else b""
            rxp = self.rx.peaks()
            total = len(txp) + len(rxp)
            if total:  # level meters
                end = b.wpos + ((24 + total + 3) & ~3)
                b.write(struct.pack("!HHHHHHHHHHHH", 24 + total, 0x8002, 4, 12 + total, ctr, 0, len(txp), 0,
                                    len(rxp), 0, 24, 0))
                b.write(txp + rxp)
                b.write(bytes(end - b.wpos))
            flows = sorted(self.rx.flows.values(), key=lambda f: f.index)
            b.write(struct.pack("!HHHHHHHHHHI", 24 + 4 * len(flows), 0x8003, 4, 12 + 4 * len(flows), ctr, 0,
                                len(flows), 0, 24, 0, self.info.rate))
            for f in flows:
                b.u32(min(f.actual_latency, 0x7FFFFFFF))
                f.actual_latency = 0
        self._mcast((HEARTBEAT_GROUP, HEARTBEAT_PORT), 0xFFFE, bytes([0, 8, 0, 1, 0x10, 0, 0, 0]), b.bytes())

    def _info_loop(self):
        i = self.info
        self._board_info()
        self._product_info()
        self._audio_status(0x80, i.rate, SAMPLE_RATES)
        self._audio_status(0x82, i.bits, ENCODINGS)
        next_hb = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_hb:
                self._heartbeat()
                next_hb = time.monotonic() + 1.0
            try:
                pkt, addr = self._info_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(pkt) < 32 or pkt[24] != 7 or pkt[26] != 0:
                continue
            op = pkt[27]
            try:
                if op == 0x61:
                    self._board_info(addr)
                elif op == 0xC1:
                    self._product_info(addr)
                elif op == 0x21:
                    self._clock_stats(addr)
                elif op == 0x13:
                    self._network_info(addr)
                elif op == 0x77:
                    self._info(0x78, bytes([0, 0, 0, 3, 0, 0, 0, 0]), addr)
                elif op in (0x81, 0x83):  # sample rate / encoding: query (mode 0) or set (mode 1)
                    mode, value = struct.unpack_from("!II", pkt, 32) if len(pkt) >= 40 else (0, 0)
                    if op == 0x81:
                        if mode == 1 and value in SAMPLE_RATES and value != i.rate and self.on_reconfigure:
                            self.log(f"Dante: controller set sample rate to {value}")
                            self._audio_status(0x80, value, SAMPLE_RATES, addr)
                            threading.Thread(target=self.on_reconfigure, kwargs={"rate": value}, daemon=True).start()
                            continue
                        self._audio_status(0x80, i.rate, SAMPLE_RATES, addr)
                    else:
                        if mode == 1 and value in ENCODINGS and value != i.bits:
                            i.bits = value
                            for idx in range(len(i.tx_names) if self.tx else 0):
                                self._advertise_channel(idx)
                            self._save_state()
                            self._dirty.set()
                        self._audio_status(0x82, i.bits, ENCODINGS, addr)
            except Exception as e:  # noqa: BLE001
                self.log(f"Dante: error handling info request {op:#04x}: {e}")

    # ================================================================ subscriptions (we receive from others)
    def _txt(self, type_, name):
        info = self.zc.get_service_info(type_, f"{name}.{type_}", timeout=2000)
        if not info or not info.parsed_addresses():
            return None, None
        p = {k.decode(): (v.decode("utf-8", "replace") if v is not None else "") for k, v in info.properties.items()}
        return info, p

    @staticmethod
    def _num(p, k, default=0):
        v = p.get(k, "")
        try:
            return int(v, 16) if v.startswith("0x") else int(v)
        except ValueError:
            return default

    def _resolve_channel(self, ch: str, host: str):
        info, p = self._txt(CHAN_TYPE, f"{ch}@{host}")
        if not info:
            return None
        nums = [int(x) for x in re.findall(r"\d+", p.get("fpp", ""))] or [FPP_MAX_ADVERTISED, FPP_MIN]
        r = {"addr": (info.parsed_addresses()[0], info.port), "id": self._num(p, "id"),
             "nchan": self._num(p, "nchan") or 1, "bits": self._num(p, "enc") or self._num(p, "en") or 24,
             "fpp_max": max(nums), "fpp_min": min(nums), "latency_ns": self._num(p, "latency_ns"),
             "rate": self._num(p, "rate") or self.info.rate, "bundle": None}
        if not r["id"]:
            return None
        for k, v in p.items():  # "b.<bundle id>=<channel in bundle>": also available as multicast
            if k.startswith("b.") and k[2:].isdigit() and v.isdigit() and int(v) > 0:
                r["bundle"] = (int(k[2:]), int(v) - 1)
                break
        return r

    def _resolve_bundle(self, bundle_id: int, host: str):
        info, p = self._txt(BUND_TYPE, f"{bundle_id}@{host}")
        if not info or "a.0" not in p:
            return None
        try:
            socket.inet_aton(p["a.0"])
        except OSError:
            return None
        return {"group": p["a.0"], "port": self._num(p, "p.0") or MULTICAST_MEDIA_PORT,
                "nchan": self._num(p, "nchan") or 1, "bits": self._num(p, "enc") or self._num(p, "en") or 24,
                "latency_ns": self._num(p, "latency_ns")}

    def _fc_request(self, addr, op1, content: bytes) -> bytes | None:
        """Send a flow-control request; return response content if OK."""
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind((self.info.ip, 0))
            s.settimeout(3)
            seq = self._fc_seq = (self._fc_seq + 1) & 0xFFFF
            s.sendto(proto.make_packet(proto.DBCP1, seq, op1, 0, content), addr)
            end = time.monotonic() + 3
            while time.monotonic() < end:
                r = proto.parse_packet(s.recv(2048))
                if r and r[1] == seq and r[2] == op1:
                    return r[4] if r[3] == proto.CODE_OK else None
        except OSError:
            return None
        finally:
            s.close()
        return None

    def _request_flow(self, addr, tx_ids, bits, fpp, port, flow_name) -> bytes | None:
        """Ask a Dante transmitter to send `tx_ids` (1-based, 0 = none) to our `port`. Returns flow handle."""
        i = self.info
        n = len(tx_ids)
        strings_off = 0x26 + n * 2 + HEADER
        strings = Buf()
        strings.cstr(i.name)
        name_off = strings.wpos + strings_off
        strings.cstr(flow_name)
        while (strings.wpos + strings_off) % 8:
            strings.u8(0)
        remote_off = strings.wpos + strings_off
        strings.write(struct.pack("!HH4s", 0x0802, port, socket.inet_aton(i.ip)))
        body = Buf()
        body.write(struct.pack("!HIIHH", strings_off, i.rate, bits, 1, n))
        body.u16(remote_off)
        for t in tx_ids:
            body.u16(t)
        body.write(struct.pack("!HHHHH", 0x1C + 2 * n, 0x0A00, 0x0002, fpp, name_off))
        body.write(bytes(12))
        assert body.wpos == strings_off - HEADER
        body.write(strings.bytes())
        r = self._fc_request(addr, 0x0100, body.bytes())
        return r[:6] if r and len(r) >= 6 else None

    def _drop_flows(self, key):
        for f in self._rx_flows_by_key.pop(key, []):
            if f.handle and f.tx_addr:
                self._fc_request(f.tx_addr, 0x0101, f.handle)
            self.rx.remove_flow(f)

    def _subscriber_loop(self):
        applied: dict[str, tuple] = {}  # tx host -> subscription signature currently set up
        last_retry = 0.0
        while not self._stop.is_set():
            self._dirty.wait(1.0)
            self._dirty.clear()
            if self._stop.is_set():
                return
            wanted: dict[str, list[tuple[int, str]]] = {}
            for idx, s in enumerate(list(self.subs)):
                if s:
                    wanted.setdefault(s.tx_host, []).append((idx, s.tx_channel))
            retry = time.monotonic() - last_retry > 9  # retry unresolved / failed every ~9 s
            if retry:
                last_retry = time.monotonic()
            for host in list(applied):
                if host not in wanted:
                    self._drop_flows(host)
                    applied.pop(host)
            now = time.monotonic()
            for host, chans in wanted.items():
                sig = (tuple(sorted(chans)), self.info.bits)
                ok = all(self.subs[i] and self.subs[i].status in RECEIVING for i, _ in chans)
                # dead flow: had audio and lost it for 5 s, or never got any within 5 s of being set up
                stale = any(now - (f.last_packet or f.created) > 5 for f in self._rx_flows_by_key.get(host, []))
                if applied.get(host) == sig and not stale and (ok or not retry):
                    continue
                self._drop_flows(host)
                applied[host] = sig
                try:
                    self._setup_host(host, chans)
                except Exception as e:  # noqa: BLE001
                    self.log(f"Dante: subscription to {host} failed: {e}")
                self._notify_channels([i for i, _ in chans])

    def _setup_host(self, host, chans):
        i = self.info
        unicast: dict[int, tuple[dict, list]] = {}          # tx channel id -> (advert, local channels)
        mcast: dict[int, dict[int, list]] = {}              # bundle id -> {channel in bundle: local channels}
        for idx, ch in chans:
            r = self._resolve_channel(ch, host)
            sub = self.subs[idx]
            if not sub:
                continue
            if not r or r["rate"] != i.rate:
                sub.status = UNRESOLVED if not r else TX_FAIL
                continue
            if r["bundle"]:  # the transmitter already multicasts this channel: just listen
                mcast.setdefault(r["bundle"][0], {}).setdefault(r["bundle"][1], []).append(idx)
            else:
                unicast.setdefault(r["id"], (r, []))[1].append(idx)

        def mark(channel_map, status):
            for locals_ in channel_map:
                for idx in locals_:
                    if self.subs[idx]:
                        self.subs[idx].status = status

        for bid, slots in mcast.items():
            b = self._resolve_bundle(bid, host)
            cmap = [slots.get(k, []) for k in range(b["nchan"])] if b else list(slots.values())
            if not b:
                mark(cmap, UNRESOLVED)
                continue
            latency = max(b["latency_ns"], i.latency_ns) * i.rate // 1_000_000_000
            try:
                f = self.rx.new_flow(cmap, b["bits"] // 8, latency, host, mcast=(b["group"], b["port"]))
            except OSError as e:
                self.log(f"Dante: can't join multicast {b['group']}: {e}")
                mark(cmap, TX_FAIL)
                continue
            self._rx_flows_by_key.setdefault(host, []).append(f)
            mark(cmap, RECEIVING_MULTICAST)
            self.log(f"Dante: receiving multicast flow {bid} from {host}")
        if not unicast:
            return
        items = sorted(unicast.items())
        first = items[0][1][0]
        per_flow = max(1, min(first["nchan"], MAX_CHANNELS_IN_FLOW))
        bits = i.bits if i.bits in ENCODINGS else first["bits"]
        for k in range(0, len(items), per_flow):
            chunk = items[k:k + per_flow]
            tx_ids = [tx_id for tx_id, _ in chunk]
            channel_map = [locals_ for _, (_, locals_) in chunk]
            fpp = max(first["fpp_min"], min(first["fpp_max"], MAX_PAYLOAD // (len(tx_ids) * bits // 8)))
            latency = max(first["latency_ns"], i.latency_ns) * i.rate // 1_000_000_000
            f = self.rx.new_flow(channel_map, bits // 8, latency, host, first["addr"])
            f.handle = self._request_flow(first["addr"], tx_ids, bits, fpp, f.port, f"{f.index + 1}_{i.process_id}")
            if not f.handle and bits != first["bits"]:  # transmitter may only do its native encoding
                self.rx.remove_flow(f)
                bits = first["bits"]
                f = self.rx.new_flow(channel_map, bits // 8, latency, host, first["addr"])
                f.handle = self._request_flow(first["addr"], tx_ids, bits, fpp, f.port,
                                              f"{f.index + 1}_{i.process_id}")
            if not f.handle:
                self.rx.remove_flow(f)
                self.log(f"Dante: {host} refused flow request")
                mark(channel_map, TX_FAIL)
            else:
                self._rx_flows_by_key.setdefault(host, []).append(f)
                self.log(f"Dante: receiving {len(tx_ids)} ch from {host}")
                mark(channel_map, RECEIVING_UNICAST)


def _netmask_gateway(ip: str) -> tuple[str, str]:
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and a.address == ip and a.netmask:
                    return a.netmask, ip.rsplit(".", 1)[0] + ".1"
    except ImportError:
        pass
    return "255.255.255.0", ip.rsplit(".", 1)[0] + ".1"
