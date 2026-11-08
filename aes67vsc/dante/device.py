"""A virtual Dante device that shows up in Dante Controller.

Ported from Inferno (GPL-3.0-or-later): device_server/{mdns_server, arc_server,
cmc_server, info_mcast_server, flows_control_server, channels_subscriber}.rs
"""
from __future__ import annotations

import re
import socket
import struct
import threading
import time
import uuid
from dataclasses import dataclass, field

from . import proto
from .flows import (FPP_MAX, FPP_MAX_ADVERTISED, FPP_MIN, MAX_CHANNELS_IN_FLOW, MAX_FLOWS, MAX_PAYLOAD,
                    Receiver, Transmitter)
from .proto import Buf, HEADER, serialize_items, start_index

CHAN_TYPE = "_netaudio-chan._udp.local."
ARC_TYPE, CMC_TYPE = "_netaudio-arc._udp.local.", "_netaudio-cmc._udp.local."
INFO_GROUP, INFO_PORT = "224.0.0.231", 8702
HEARTBEAT_GROUP, HEARTBEAT_PORT = "224.0.0.233", 8708

# subscription status values shown by Dante Controller
UNRESOLVED, TX_FAIL, RECEIVING_UNICAST = 1, 0x15, 0x01010009


def dante_name(s: str) -> str:
    """Dante device names: letters, digits, hyphens; max 31 chars (Controller ignores longer)."""
    s = re.sub(r"[^A-Za-z0-9-]+", "-", s).strip("-")
    return (s or "aes67-vsc")[:31]


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
    tx_names: list[str]
    rate: int = 48000
    bits: int = 24
    latency_ns: int = 10_000_000
    process_id: int = 0
    mac: bytes = field(default_factory=lambda: uuid.getnode().to_bytes(6, "big"))
    manufacturer: str = "aes67-vsc"
    board: str = "aes67-vsc"
    model: str = "aes67-vsc virtual soundcard"
    model_number: str = "_000000000000000b"
    vendor: str = "Audinate Dante-compatible"
    pcm_type: int = 0xE
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


class DanteDevice:
    def __init__(self, info: DeviceInfo, clock, source=None, out_buffer=None, zc=None, log=print):
        self.info, self.clock, self.log = info, clock, log
        self.subs: list[Subscription | None] = [None] * len(info.rx_names)
        self._stop = threading.Event()
        self._dirty = threading.Event()
        self._mcast_seq = 1
        self._socks = []
        self.tx = Transmitter(clock, info.rate, len(info.tx_names), source, info.ip) if source else None
        self.rx = Receiver(clock, info.rate, len(info.rx_names), info.ip, out_buffer)
        self._rx_flows_by_host: dict[str, list] = {}
        self._fc_seq = 1
        from zeroconf import IPVersion, Zeroconf
        self._own_zc = zc is None
        self.zc = zc or Zeroconf(interfaces=[info.ip], ip_version=IPVersion.V4Only)
        self._services = []

    # ================================================================ lifecycle
    def start(self):
        i = self.info
        for port, handler, name in ((i.arc_port, self._arc, "arc"), (i.cmc_port, self._cmc, "cmc"),
                                    (i.flows_port, self._flow_control, "flows")):
            s = self._udp(port)
            threading.Thread(target=self._serve, args=(s, handler), daemon=True, name=f"dante-{name}").start()
        self._info_sock = self._udp(i.info_port)
        self._mc = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._mc.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(i.ip))
        self._mc.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 1)
        threading.Thread(target=self._info_loop, daemon=True, name="dante-info").start()
        threading.Thread(target=self._subscriber_loop, daemon=True, name="dante-subs").start()
        self._advertise()
        self.log(f"Dante: device \"{i.name}\" online ({len(i.tx_names)} TX / {len(i.rx_names)} RX channels)")

    def stop(self):
        self._stop.set()
        self._dirty.set()
        for s in self._services:
            try:
                self.zc.unregister_service(s)
            except Exception:  # noqa: BLE001
                pass
        if self._own_zc:
            self.zc.close()
        for host in list(self._rx_flows_by_host):
            self._drop_host_flows(host)
        if self.tx:
            self.tx.stop()
        self.rx.stop()
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
                s.sendto(proto.make_packet(start, seq, op1, code, body), addr)

    # ================================================================ mDNS
    def _advertise(self):
        from zeroconf import ServiceInfo
        i = self.info
        host = f"{i.name}.local."
        addr = [socket.inet_aton(i.ip)]

        def svc(type_, inst, port, props):
            info = ServiceInfo(type_, f"{inst}.{type_}", port=port, addresses=addr, server=host,
                               properties=props, host_ttl=120, other_ttl=4500)
            self._services.append(info)
            threading.Thread(target=self.zc.register_service, args=(info,), daemon=True).start()

        svc(ARC_TYPE, i.name, i.arc_port, {
            "arcp_vers": "2.7.41", "arcp_min": "0.2.4", "router_vers": "4.0.2", "router_info": i.board,
            "mf": i.manufacturer, "model": i.model_number})
        svc(CMC_TYPE, i.name, i.cmc_port, {
            "id": i.device_id.hex(), "process": str(i.process_id), "cmcp_vers": "1.2.0", "cmcp_min": "1.0.0",
            "server_vers": "4.0.2", "channels": "0x6000004d", "mf": i.manufacturer, "model": i.model_number})
        nchan = min(MAX_CHANNELS_IN_FLOW, len(i.tx_names))
        for idx, name in enumerate(i.tx_names):
            svc(CHAN_TYPE, f"{name}@{i.name}", i.flows_port, {
                "txtvers": "2", "dbcp1": "0x1102", "dbcp": "0x1004", "id": str(idx + 1), "rate": str(i.rate),
                "pcm": f"{i.bits // 8} {i.pcm_type:x}", "enc": str(i.bits), "en": str(i.bits),
                "latency_ns": str(i.latency_ns), "fpp": f"{FPP_MAX_ADVERTISED},{FPP_MIN}",
                "nchan": str(nchan), "default": None})

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
            return struct.pack("!BBHHHHHHHHHH12s", 0, 0x30, ntx, nrx, 4, min(MAX_CHANNELS_IN_FLOW, ntx), 8,
                               MAX_FLOWS, MAX_FLOWS, ntx + nrx, 1, 1, b"")
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
            b.b[0:38] = struct.pack("!HHHHHHHHH12sHHHH", 0, 0, 0, bo, rv, 0, fr, fa, fr, b"", 0x2729, 0, 0x1102, 0)
            return b.bytes()
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
        if op1 == 0x2000:  # transmit channels
            def tx_item(item, buf):
                idx, name = item
                co = common(buf)
                return struct.pack("!HHHH", idx + 1, 7, co, buf.cstr(name))
            return page(min(len(i.tx_names), 32), 8, list(enumerate(i.tx_names)), tx_item)
        if op1 == 0x2010:  # transmit channel friendly names
            wrote = [False]

            def fn_item(item, buf):
                if not wrote[0]:
                    buf.u32(0)
                    wrote[0] = True
                idx, name = item
                return struct.pack("!HHH", idx + 1, idx + 1, buf.cstr(name))
            return page(min(len(i.tx_names), 32), 6, list(enumerate(i.tx_names)), fn_item)
        if op1 == 0x3001:  # rename RX channels
            any_ = False
            for it in proto.deserialize_items(c, 4):
                ch, off = struct.unpack("!HH", it)
                name = proto.read_cstr(c, off - HEADER) if off >= HEADER else None
                if ch and name and ch <= len(i.rx_names):
                    i.rx_names[ch - 1], any_ = name, True
            self._notify_channels(range(len(i.rx_names)))
            return (proto.CODE_OK if any_ else 0xFFFF), b""
        if op1 == 0x2013:  # rename TX channels: needs re-advertising, not supported yet
            return 0xFFFF, b""
        if op1 == 0x3010:  # set subscriptions (Dante Controller routing grid)
            changed = []
            for it in proto.deserialize_items(c, 6):
                ch, tco, tho = struct.unpack("!HHH", it)
                if not 0 < ch <= len(i.rx_names):
                    continue
                if tco and tho:
                    txc, txh = proto.read_cstr(c, tco - HEADER), proto.read_cstr(c, tho - HEADER)
                    if txc and txh:
                        self.log(f"Dante: RX {ch} <- {txc}@{txh}")
                        self.subs[ch - 1] = Subscription(txc, txh)
                else:
                    self.subs[ch - 1] = None
                changed.append(ch - 1)
            self._dirty.set()
            self._notify_channels(changed)
            return b""
        if op1 == 0x3014:  # unsubscribe one channel
            ch = struct.unpack_from("!H", c, 4)[0]
            if 0 < ch <= len(i.rx_names):
                self.subs[ch - 1] = None
                self._dirty.set()
                self._notify_channels([ch - 1])
            return b""
        if op1 == 0x2200:  # our transmit flows
            flows = sorted(self.tx.flows.values(), key=lambda f: f.index) if self.tx else []

            def txf(f, buf):
                local = buf.cstr(f"{f.index + 1}_{i.process_id}")
                rh, rf = buf.cstr_or_0(f.rx_host or None), buf.cstr_or_0(f.rx_flow or None)
                buf.align(4)
                so = buf.wpos
                buf.write(struct.pack("!HH4s", 0x8002, f.dst[1], socket.inet_aton(f.dst[0])))
                no = buf.wpos
                buf.write(struct.pack("!HHHHHH8s", 0xA00, 1, rh, rf, 0x10, local, b""))
                mo = buf.wpos
                buf.write(struct.pack("!HHIHHHHH", f.index + 1, 0x11, i.rate, 0, f.bps * 8, 1, len(f.channels), so))
                for ch in f.channels:
                    buf.u16(0 if ch is None else ch + 1)
                buf.u16(no)
                return struct.pack("!H", mo)
            return page(16, 2, flows, txf)
        if op1 == 0x3200:  # our receive flows
            flows = sorted(self.rx.flows.values(), key=lambda f: f.index)

            def rxf(f, buf):
                buf.align(4)
                so = buf.wpos
                buf.write(struct.pack("!HH4s", 0x8002, f.port, socket.inet_aton(i.ip)))
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
        if op1 == 0x2201 or op1 == 0x2202:  # create/delete multicast TX flow: not supported yet
            return 0xFFFF, b""
        if op1 == 0x2320:
            return 0x30, b""
        if op1 == 0x1100:
            return bytes(110)
        if op1 == 0x1102:
            return bytes(94)
        if op1 == 0x3300:
            return bytes([0x38, 0x00, 0x38, 0xFD, 0x38, 0xFE, 0x38, 0xFF])
        return None

    # ================================================================ flow control (others request our audio)
    def _flow_control(self, op1, c, pkt):
        i = self.info
        if not self.tx:
            return 0x0315, b""
        if op1 == 0x0100 and len(c) > 16:  # request flow
            host_off, rate, bits, _one, nch, remote_off = struct.unpack_from("!HIIHHH", c, 0)
            chans = [None if x == 0 else x - 1 for x in struct.unpack_from(f"!{nch}H", c, 16)]
            o = 16 + nch * 2 + 6
            fpp, flow_name_off = struct.unpack_from("!HH", c, o)
            host, flow_name = proto.read_cstr(pkt, host_off), proto.read_cstr(pkt, flow_name_off)
            port = struct.unpack_from("!H", pkt, remote_off + 2)[0]
            ip = socket.inet_ntoa(pkt[remote_off + 4:remote_off + 8])
            if rate != i.rate:
                return 0x0301, b""  # sample rate mismatch
            if fpp > FPP_MAX or any(ch is not None and ch >= len(i.tx_names) for ch in chans):
                return 0x0302, b""
            try:
                f = self.tx.add_flow((ip, port), chans, fpp, bits // 8, host or "", flow_name or "")
            except RuntimeError:
                return 0x0315, b""
            self.log(f"Dante: sending {sum(c is not None for c in chans)} ch to {host} ({ip}:{port})")
            return f.handle
        if op1 == 0x0101 and len(c) >= 6:  # stop flow
            f = self.tx.by_handle(c[:6])
            if not f:
                return 0x0103, b""
            self.tx.remove_flow(f)
            return b""
        if op1 == 0x0102 and len(c) >= 8:  # update flow channels
            f = self.tx.by_handle(c[:6])
            if not f:
                return 0x0103, b""
            n = struct.unpack_from("!H", c, 6)[0]
            f.channels = [None if x == 0 else x - 1 for x in struct.unpack_from(f"!{n}H", c, 8)]
            return b""
        return None

    # ================================================================ info multicasts / heartbeat
    def _mcast(self, dst, start_code, opcode: bytes, content: bytes):
        i = self.info
        pkt = proto.make_mcast(start_code, self._mcast_seq, i.process_id, i.device_id,
                               proto.fixed_str(i.vendor, 8).replace(b"\0", b" "), opcode, content)
        self._mcast_seq = (self._mcast_seq + 1) & 0xFFFF
        try:
            self._mc.sendto(pkt, dst)
        except OSError:
            pass

    def _notify_channels(self, indices):
        mask = bytearray(1)
        for ch in indices:
            if ch // 8 >= len(mask):
                mask.extend(bytes(ch // 8 + 1 - len(mask)))
            mask[ch // 8] |= 1 << (ch % 8)
        self._mcast((INFO_GROUP, INFO_PORT), 0xFFFF, bytes([7, 0x2A, 1, 2, 0, 0, 0, 0]),
                    struct.pack("!H", len(mask)) + bytes(mask))

    def _board_info(self):
        c = bytearray(200)
        c[0:8] = bytes([4, 1, 0, 6, 4, 1, 0, 3])
        c[0x16], c[0x23], c[0x27], c[0x28] = 0x10, 2, 1, 1
        c[0xBB] = 0x1F
        c[12:20] = proto.fixed_str(self.info.board, 8)
        c[0x38:0x48] = proto.fixed_str(self.info.board, 16)
        self._mcast((INFO_GROUP, INFO_PORT), 0xFFFF, bytes([7, 0x2A, 0, 0x60, 0, 0, 0, 0]), bytes(c))

    def _product_info(self):
        i = self.info
        c = bytearray(336)
        c[0:8] = proto.fixed_str(i.manufacturer, 8)
        c[8:16] = proto.fixed_str(i.board, 8)
        c[0x2C:0x3C] = proto.fixed_str(i.manufacturer, 16)
        c[0xAC:0xBC] = proto.fixed_str(i.model, 16)
        c[0x1C:0x20] = bytes([0, 1, 0, 0])
        self._mcast((INFO_GROUP, INFO_PORT), 0xFFFF, bytes([7, 0x2A, 0, 0xC0, 0, 0, 0, 0]), bytes(c))

    def _network_info(self):
        i = self.info
        mask, gw = _netmask_gateway(i.ip)
        c = bytes([0, 1, 0, 0, 0, 0]) + struct.pack("!HH", 1000, 1) + i.mac + socket.inet_aton(i.ip) \
            + socket.inet_aton(mask) + socket.inet_aton(gw) * 2 + bytes([0, 0x18, 0, 0x30]) + bytes(28)
        self._mcast((INFO_GROUP, INFO_PORT), 0xFFFF, bytes([7, 0x2A, 0, 0x11, 0, 0, 0, 0]), c)

    def _clock_stats(self):
        gm = self.clock.gm_id
        if not self.clock.locked or not gm:
            return
        g = bytes.fromhex(gm.replace("-", ""))
        c = bytes([0, 3, 0, 3, 0, 0, 0, 0x9F]) + struct.pack("!i", 0) + self.info.mac + b"\0\0" + g + g + bytes(76)
        self._mcast((INFO_GROUP, INFO_PORT), 0xFFFF, bytes([7, 0x2A, 0, 0x20, 0, 0, 0, 0]), c)

    def _heartbeat(self):
        b = Buf()
        ctr = self._mcast_seq
        if self.clock.locked:
            b.write(struct.pack("!HHHHHHi", 16, 0x8001, 4, 4, ctr, 0, 0))  # clock: frequency offset (ppb)
            flows = sorted(self.rx.flows.values(), key=lambda f: f.index)
            b.write(struct.pack("!HHHHHHHHHHI", 24 + 4 * len(flows), 0x8003, 4, 12 + 4 * len(flows), ctr, 0,
                                len(flows), 0, 24, 0, self.info.rate))
            for f in flows:
                b.u32(min(f.actual_latency, 0x7FFFFFFF))
                f.actual_latency = 0
        self._mcast((HEARTBEAT_GROUP, HEARTBEAT_PORT), 0xFFFE, bytes([0, 8, 0, 1, 0x10, 0, 0, 0]), b.bytes())

    def _info_loop(self):
        self._board_info()
        self._product_info()
        next_hb = 0.0
        while not self._stop.is_set():
            if time.monotonic() >= next_hb:
                self._heartbeat()
                next_hb = time.monotonic() + 1.0
            try:
                pkt, _ = self._info_sock.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(pkt) < 32:
                continue
            op = pkt[24:32]
            if op[0] != 7 or op[2] != 0:
                continue
            {0x61: self._board_info, 0xC1: self._product_info, 0x21: self._clock_stats,
             0x13: self._network_info}.get(op[3], lambda: None)()
            if op[3] == 0x77:
                self._mcast((INFO_GROUP, INFO_PORT), 0xFFFF, bytes([7, 0x2A, 0, 0x78, 0, 0, 0, 0]),
                            bytes([0, 0, 0, 3, 0, 0, 0, 0]))

    # ================================================================ subscriptions (we receive from others)
    def _resolve_channel(self, ch: str, host: str):
        info = self.zc.get_service_info(CHAN_TYPE, f"{ch}@{host}.{CHAN_TYPE}", timeout=2000)
        if not info or not info.parsed_addresses():
            return None
        p = {k.decode(): (v.decode() if v is not None else "") for k, v in info.properties.items()}

        def num(k):
            v = p.get(k, "0")
            return int(v, 16) if v.startswith("0x") else int(v)
        try:
            fpp_max, fpp_min = (int(x) for x in p.get("fpp", "32,2").split(","))
            return {"addr": (info.parsed_addresses()[0], info.port), "id": num("id"),
                    "nchan": num("nchan") or 1, "bits": num("enc") or num("en") or 24,
                    "dbcp1": num("dbcp1") or proto.DBCP1, "fpp_max": fpp_max, "fpp_min": fpp_min,
                    "latency_ns": num("latency_ns"), "rate": num("rate") or self.info.rate}
        except ValueError:
            return None

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

    def _drop_host_flows(self, host):
        for f in self._rx_flows_by_host.pop(host, []):
            if f.handle and f.tx_addr:
                self._fc_request(f.tx_addr, 0x0101, f.handle)
            self.rx.remove_flow(f)

    def _subscriber_loop(self):
        applied: dict[str, tuple] = {}  # host -> subscription signature currently set up
        last_full = 0.0
        while not self._stop.is_set():
            self._dirty.wait(1.0)
            self._dirty.clear()
            if self._stop.is_set():
                return
            wanted: dict[str, list[tuple[int, str]]] = {}
            for idx, s in enumerate(self.subs):
                if s:
                    wanted.setdefault(s.tx_host, []).append((idx, s.tx_channel))
            retry = time.monotonic() - last_full > 9  # retry unresolved / failed every ~9 s
            if retry:
                last_full = time.monotonic()
            for host in list(applied):
                if host not in wanted:
                    self._drop_host_flows(host)
                    applied.pop(host)
            for host, chans in wanted.items():
                sig = tuple(sorted(chans))
                ok = all(self.subs[i] and self.subs[i].status == RECEIVING_UNICAST for i, _ in chans)
                stale = any(time.monotonic() - f.last_packet > 5 and f.packets
                            for f in self._rx_flows_by_host.get(host, []))
                if applied.get(host) == sig and (ok or not retry) and not stale:
                    continue
                self._drop_host_flows(host)
                applied[host] = sig
                self._setup_host(host, chans)
                self._notify_channels([i for i, _ in chans])

    def _setup_host(self, host, chans):
        i = self.info
        resolved = {}
        for idx, ch in chans:
            r = self._resolve_channel(ch, host)
            sub = self.subs[idx]
            if not sub:
                continue
            if not r or r["rate"] != i.rate:
                sub.status = UNRESOLVED if not r else TX_FAIL
                continue
            resolved.setdefault(r["id"], (r, []))[1].append(idx)
        if not resolved:
            return
        items = sorted(resolved.items())
        first = items[0][1][0]
        per_flow = max(1, min(first["nchan"], MAX_CHANNELS_IN_FLOW))
        bits = first["bits"]
        for k in range(0, len(items), per_flow):
            chunk = items[k:k + per_flow]
            tx_ids = [tx_id for tx_id, _ in chunk]
            channel_map = [locals_ for _, (_, locals_) in chunk]
            fpp = min(first["fpp_max"], MAX_PAYLOAD // (len(tx_ids) * bits // 8))
            latency = max(first["latency_ns"], i.latency_ns) * i.rate // 1_000_000_000
            f = self.rx.new_flow(channel_map, bits // 8, latency, host, first["addr"])
            f.handle = self._request_flow(first["addr"], tx_ids, bits, fpp, f.port, f"{f.index + 1}_{i.process_id}")
            status = RECEIVING_UNICAST if f.handle else TX_FAIL
            if not f.handle:
                self.rx.remove_flow(f)
                self.log(f"Dante: {host} refused flow request")
            else:
                self._rx_flows_by_host.setdefault(host, []).append(f)
                self.log(f"Dante: receiving {len(tx_ids)} ch from {host}")
            for locals_ in channel_map:
                for idx in locals_:
                    if self.subs[idx]:
                        self.subs[idx].status = status


def _netmask_gateway(ip: str) -> tuple[str, str]:
    try:
        import psutil
        for addrs in psutil.net_if_addrs().values():
            for a in addrs:
                if a.family == socket.AF_INET and a.address == ip and a.netmask:
                    gw = ip.rsplit(".", 1)[0] + ".1"
                    return a.netmask, gw
    except ImportError:
        pass
    return "255.255.255.0", ip.rsplit(".", 1)[0] + ".1"
