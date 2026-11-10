"""PTPv2 (IEEE 1588-2008) node for the AES67 media profile, plus PTPv1 (IEEE 1588-2002) for Dante.

* Runs the Best Master Clock Algorithm: follows a better clock (e.g. a Dante
  device) when one exists, otherwise becomes grandmaster itself, so the app
  also works PC-to-PC or with gear that can only follow.
* As follower: two-step Sync/Follow_Up + Delay_Req/Delay_Resp (end-to-end
  delay measurement), min-filtered to reject network queuing jitter, slewed
  so the media clock never jumps.
* As master: sends Announce/Sync/Follow_Up and answers Delay_Req.

Software timestamps only: expect roughly 10-100 us accuracy on a LAN, well
inside Dante's minimum 1 ms receive latency.

Ports 319/320 are privileged on Linux/macOS (sudo, or see README).
"""
from __future__ import annotations

import collections
import random
import select
import struct
import threading
import time
import uuid

from .net import multicast_rx, multicast_tx

PTP_GROUP = "224.0.1.129"
EVENT_PORT, GENERAL_PORT = 319, 320
TAI_UTC_OFFSET = 37

SYNC, DELAY_REQ, FOLLOW_UP, DELAY_RESP, ANNOUNCE = 0x0, 0x1, 0x8, 0x9, 0xB
CONTROL = {SYNC: 0, DELAY_REQ: 1, FOLLOW_UP: 2, DELAY_RESP: 3, ANNOUNCE: 5}

V1_STRATUM, V1_VARIANCE, V1_LOG_SYNC = 4, 0x7FFF, -2   # our PTPv1 master properties (4 Sync/s)

LOG_SYNC, LOG_ANNOUNCE = -3, 0  # 8 Sync/s, 1 Announce/s (AES67 media profile ranges)
ANNOUNCE_TIMEOUT = 3  # announce intervals


def fmt_id(b: bytes) -> str:
    return "-".join(f"{x:02X}" for x in b)


def local_clock_id() -> bytes:
    """EUI-64 from the MAC address, like real PTP devices."""
    mac = uuid.getnode().to_bytes(6, "big")
    return mac[:3] + b"\xff\xfe" + mac[3:]


def _ts_get(b: bytes, off: int) -> int:
    return int.from_bytes(b[off:off + 6], "big") * 1_000_000_000 + int.from_bytes(b[off + 6:off + 10], "big")


def _ts_put(ns: int) -> bytes:
    return (ns // 1_000_000_000).to_bytes(6, "big") + (ns % 1_000_000_000).to_bytes(4, "big")


class Dataset:
    """Grandmaster properties compared by the BMCA (lower wins, field by field)."""

    def __init__(self, priority1, clock_class, accuracy, variance, priority2, identity: bytes, steps=0):
        self.priority1, self.clock_class, self.accuracy = priority1, clock_class, accuracy
        self.variance, self.priority2, self.identity, self.steps = variance, priority2, identity, steps

    def key(self):
        return (self.priority1, self.clock_class, self.accuracy, self.variance, self.priority2,
                self.identity, self.steps)


class Clock:
    """PTP clock: `now_ns()` gives network (TAI) time whichever role we're in."""

    def __init__(self, iface_ip: str, domain: int = 0, can_lead: bool = True, priority1: int = 250,
                 identity: bytes | None = None, lead_v1: bool = False):
        self.iface_ip, self.domain, self.can_lead = iface_ip, domain, can_lead
        # Also send PTPv1 Sync when we lead, so Dante devices (and other copies of this app in
        # Dante mode) can follow us when no Dante hardware is around to provide the clock.
        self.lead_v1 = lead_v1 and can_lead
        self.port_id = (identity or local_clock_id()) + b"\x00\x01"
        # Low priority by default so real hardware always wins the election.
        self.own = Dataset(priority1, 248, 0xFE, 0xFFFF, priority1, self.port_id[:8])
        self.state = "listening"  # listening | follower | leader | disabled
        self.error: str | None = None
        self.gm_id: str | None = None
        self.offset_ns: int | None = None  # network - local
        self.path_delay_ns: int | None = None
        self.last_sync = 0.0
        self._foreign: dict[bytes, tuple[float, Dataset, float]] = {}  # port -> (seen, ds, timeout)
        self._master_port: bytes | None = None
        self._ms = collections.deque(maxlen=16)  # t2 - t1 samples (master->slave)
        self._sm = collections.deque(maxlen=8)   # t4 - t3 samples (slave->master)
        self._pending_sync: dict[int, int] = {}
        self._pending_dreq: dict[int, int] = {}
        self._seq = {SYNC: 0, DELAY_REQ: 0, ANNOUNCE: 0}
        # PTPv1 (IEEE 1588-2002): what Dante uses natively.
        self.version = 2                  # protocol of the clock we follow
        self._v1_masters: dict[bytes, tuple[float, tuple, bytes]] = {}  # src -> (seen, BMCA key, gm uuid)
        self._v1_uuid = self.port_id[:3] + self.port_id[5:8]           # MAC from our EUI-64
        # Deliberately the worst acceptable master: any real Dante device wins the election.
        self._v1_own_key = (1, V1_STRATUM, V1_VARIANCE, self._v1_uuid)
        self._v1_seq = 0
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._start_time = time.monotonic()
        self._listeners = []

    # ---- public -----------------------------------------------------------
    def start(self) -> bool:
        try:
            self._ev = multicast_rx(PTP_GROUP, EVENT_PORT, self.iface_ip)
            self._gen = multicast_rx(PTP_GROUP, GENERAL_PORT, self.iface_ip)
            self._tx = multicast_tx(self.iface_ip, ttl=1)
        except OSError as e:
            self.error, self.state = str(e), "disabled"
            return False
        threading.Thread(target=self._run, daemon=True, name="ptp").start()
        return True

    def stop(self):
        self._stop.set()

    @property
    def locked(self) -> bool:
        if self.state == "leader":
            return True
        return self.state == "follower" and self.offset_ns is not None and time.monotonic() - self.last_sync < 3

    def now_ns(self) -> int:
        """Network PTP time (TAI) in ns."""
        off = self.offset_ns if self.offset_ns is not None else TAI_UTC_OFFSET * 1_000_000_000
        return time.time_ns() + off

    def on_change(self, fn):
        """Register fn() called when grandmaster changes."""
        self._listeners.append(fn)

    def wait_settled(self, timeout: float = 4.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end and not self.locked and self.state != "disabled":
            time.sleep(0.05)
        return self.locked

    # ---- main loop --------------------------------------------------------
    def _run(self):
        socks = [self._ev, self._gen]
        next_sync = next_announce = next_dreq = next_bmca = next_v1 = 0.0
        while not self._stop.is_set():
            now = time.monotonic()
            if now >= next_bmca:
                self._bmca()
                next_bmca = now + 0.25
            if self.state == "leader":
                if now >= next_announce:
                    self._send_announce()
                    next_announce = now + 2.0 ** LOG_ANNOUNCE
                if now >= next_sync:
                    self._send_sync()
                    next_sync = now + 2.0 ** LOG_SYNC
                if self.lead_v1 and now >= next_v1:
                    self._send_v1_sync()
                    next_v1 = now + 2.0 ** V1_LOG_SYNC
            elif self.state == "follower" and now >= next_dreq and self._ms:
                self._send_delay_req()
                next_dreq = now + random.uniform(0.5, 1.5)
            wait = max(0.0, min(next_sync if self.state == "leader" else next_dreq, next_bmca) - time.monotonic())
            r, _, _ = select.select(socks, [], [], min(wait, 0.1))
            for s in r:
                try:
                    pkt, addr = s.recvfrom(1500)
                except OSError:
                    return
                self._handle(pkt, time.time_ns(), addr)

    # ---- BMCA -------------------------------------------------------------
    def _bmca(self):
        now = time.monotonic()
        with self._lock:
            for p in [p for p, (t, _, to) in self._foreign.items() if now - t > to]:
                del self._foreign[p]
            best = min(self._foreign.items(), key=lambda kv: kv[1][1].key(), default=None)
            for p in [p for p, (t, _, _) in self._v1_masters.items() if now - t > 3]:
                del self._v1_masters[p]
            v1 = min(self._v1_masters.items(), key=lambda kv: kv[1][1], default=None)
        if v1 and self.lead_v1 and self._v1_own_key < v1[1][1]:
            v1 = None   # we are the better PTPv1 master (only another copy of this app ranks this low)
        if best and (not self.can_lead or best[1][1].key() < self.own.key()):
            if self._master_port != best[0]:
                self._follow(best[0], 2, fmt_id(best[1][1].identity))
        elif v1:  # a Dante (PTPv1) leader is on the network: never compete with it
            if self._master_port != v1[0]:
                gm = v1[1][2]
                self._follow(v1[0], 1, fmt_id(gm[:3] + b"\xff\xfe" + gm[3:]))
        elif self.can_lead and now - self._start_time > ANNOUNCE_TIMEOUT * 2.0 ** LOG_ANNOUNCE:
            if self.state != "leader":
                self._master_port = None
                if self.offset_ns is None:  # nobody to follow: our TAI = system UTC + 37 s
                    self.offset_ns = TAI_UTC_OFFSET * 1_000_000_000
                self._set_state("leader", fmt_id(self.own.identity))
        elif not best and self.state == "follower":
            self._master_port = None
            self._set_state("listening", self.gm_id)

    def _follow(self, port: bytes, version: int, gm: str):
        self._master_port, self.version = port, version
        self._ms.clear()
        self._sm.clear()
        self._pending_sync.clear()
        self.path_delay_ns = None
        self._set_state("follower", gm)

    def _set_state(self, state, gm):
        changed = gm != self.gm_id
        self.state, self.gm_id = state, gm
        if changed:
            for fn in self._listeners:
                fn()

    # ---- receive ----------------------------------------------------------
    def _handle(self, p: bytes, rx: int, addr=None):
        if len(p) >= 40 and p[0] == 0 and p[1] == 1:
            return self._handle_v1(p, rx)
        if len(p) < 34 or p[1] & 0x0F != 2 or p[4] != self.domain:
            return
        mtype = p[0] & 0x0F
        src = p[20:30]
        if src == self.port_id:
            return  # our own multicast looped back
        seq = struct.unpack_from("!H", p, 30)[0]
        corr = struct.unpack_from("!q", p, 8)[0] >> 16
        if mtype == ANNOUNCE and len(p) >= 64:
            ds = Dataset(p[47], p[48], p[49], struct.unpack_from("!H", p, 50)[0], p[52], p[53:61],
                         struct.unpack_from("!H", p, 61)[0])
            log_iv = struct.unpack_from("!b", p, 33)[0]
            if ds.steps >= 255:
                return
            with self._lock:
                self._foreign[src] = (time.monotonic(), ds, ANNOUNCE_TIMEOUT * 2.0 ** max(log_iv, -3))
            return
        if mtype == DELAY_REQ and self.state == "leader" and len(p) >= 44:
            self._send_delay_resp(p, rx)
            return
        if src != self._master_port:
            return
        if mtype == SYNC and len(p) >= 44:
            if p[6] & 0x02:  # two-step
                self._pending_sync[seq] = rx - corr
                if len(self._pending_sync) > 32:
                    self._pending_sync.pop(next(iter(self._pending_sync)))
            else:
                self._sync_sample(_ts_get(p, 34), rx - corr)
        elif mtype == FOLLOW_UP and len(p) >= 44:
            t2 = self._pending_sync.pop(seq, None)
            if t2 is not None:
                self._sync_sample(_ts_get(p, 34) + corr, t2)
        elif mtype == DELAY_RESP and len(p) >= 54 and p[44:54] == self.port_id:
            t3 = self._pending_dreq.pop(seq, None)
            if t3 is not None:
                self._sm.append(_ts_get(p, 34) - corr - t3)
                self._update()

    def _handle_v1(self, p: bytes, rx: int):
        """PTPv1: 40-byte header, control field at byte 32 (0 Sync, 1 Delay_Req, 2 Follow_Up, 3 Delay_Resp)."""
        ctrl, src, seq = p[32], p[22:30], struct.unpack_from("!H", p, 30)[0]
        if src[:6] == self._v1_uuid:
            return
        b = 40  # body
        if ctrl == 1 and self.state == "leader" and self.lead_v1:
            return self._send_v1_delay_resp(p, rx)
        if ctrl == 0 and len(p) >= b + 84:  # Sync: only masters send it
            # preferred flag, stratum, variance, uuid: lower wins (simplified IEEE 1588-2002 BMCA)
            key = (0 if p[b + 37] else 1, p[b + 27], struct.unpack_from("!h", p, b + 34)[0], bytes(src[:6]))
            with self._lock:
                self._v1_masters[src] = (time.monotonic(), key, p[b + 14:b + 20])
            if src != self._master_port:
                return
            if p[35] & 0x08:  # PTP_ASSIST: two-step, time follows in Follow_Up
                self._pending_sync[seq] = rx
                if len(self._pending_sync) > 32:
                    self._pending_sync.pop(next(iter(self._pending_sync)))
            else:
                self._sync_sample(_ts_v1(p, b), rx)
        elif src != self._master_port:
            return
        elif ctrl == 2 and len(p) >= b + 12:  # Follow_Up
            t2 = self._pending_sync.pop(struct.unpack_from("!H", p, b + 2)[0], None)
            if t2 is not None:
                self._sync_sample(_ts_v1(p, b + 4), t2)
        elif ctrl == 3 and len(p) >= b + 20 and p[b + 10:b + 16] == self._v1_uuid \
                and struct.unpack_from("!H", p, b + 16)[0] == 1:  # Delay_Resp for us
            t3 = self._pending_dreq.pop(struct.unpack_from("!H", p, b + 18)[0], None)
            if t3 is not None:
                self._sm.append(_ts_v1(p, b) - t3)
                self._update()

    def _sync_sample(self, t1: int, t2: int):
        self._ms.append(t2 - t1)
        self.last_sync = time.monotonic()
        self._update()

    def _update(self):
        # Min-filter each direction: the least-delayed packet is the most accurate.
        ms = min(self._ms)
        if self._sm:
            sm = min(self._sm)
            self.path_delay_ns = max(0, (ms + sm) // 2)
            target = -(ms - sm) // 2           # network - local
        else:
            target = -ms                        # path delay unknown yet (~tens of us)
        with self._lock:
            cur = self.offset_ns
            if cur is None or abs(target - cur) > 1_000_000 or self.state != "follower":
                self.offset_ns = target         # first lock or big jump: step
            else:
                step = target - cur             # slew: at most 20 us per update
                self.offset_ns = cur + max(-20_000, min(20_000, step))

    # ---- transmit ---------------------------------------------------------
    def _header(self, mtype: int, length: int, seq: int, log_iv: int, flags0=0, flags1=0, corr=0) -> bytearray:
        h = bytearray(length)
        h[0], h[1] = mtype, 2
        struct.pack_into("!H", h, 2, length)
        h[4], h[6], h[7] = self.domain, flags0, flags1
        struct.pack_into("!q", h, 8, corr << 16)
        h[20:30] = self.port_id
        struct.pack_into("!HBb", h, 30, seq & 0xFFFF, CONTROL[mtype], log_iv)
        return h

    def _next(self, mtype):
        self._seq[mtype] = (self._seq[mtype] + 1) & 0xFFFF
        return self._seq[mtype]

    def _send(self, data, port):
        try:
            self._tx.sendto(bytes(data), (PTP_GROUP, port))
        except OSError:
            pass

    def _send_announce(self):
        o = self.own
        h = self._header(ANNOUNCE, 64, self._next(ANNOUNCE), LOG_ANNOUNCE, flags1=0x0C)  # utcValid, PTP timescale
        struct.pack_into("!h", h, 44, TAI_UTC_OFFSET)
        h[47], h[48], h[49] = o.priority1, o.clock_class, o.accuracy
        struct.pack_into("!H", h, 50, o.variance)
        h[52], h[53:61] = o.priority2, o.identity
        struct.pack_into("!HB", h, 61, 0, 0xA0)  # stepsRemoved 0, timeSource internal oscillator
        self._send(h, GENERAL_PORT)

    def _send_sync(self):
        seq = self._next(SYNC)
        t1 = self.now_ns()
        self._send(self._header(SYNC, 44, seq, LOG_SYNC, flags0=0x02), EVENT_PORT)
        fu = self._header(FOLLOW_UP, 44, seq, LOG_SYNC)
        fu[34:44] = _ts_put(t1)
        self._send(fu, GENERAL_PORT)

    def _send_delay_resp(self, req: bytes, rx: int):
        h = self._header(DELAY_RESP, 54, struct.unpack_from("!H", req, 30)[0], LOG_SYNC,
                         corr=struct.unpack_from("!q", req, 8)[0] >> 16)
        h[34:44] = _ts_put(rx + self.offset_ns)
        h[44:54] = req[20:30]
        self._send(h, GENERAL_PORT)

    def _send_delay_req(self):
        seq = self._next(DELAY_REQ)
        t3 = time.time_ns()
        if self.version == 1:
            self._send(self._v1_delay_req(seq, t3), EVENT_PORT)
        else:
            self._send(self._header(DELAY_REQ, 44, seq, 0x7F), EVENT_PORT)
        self._pending_dreq[seq] = t3
        if len(self._pending_dreq) > 16:
            self._pending_dreq.pop(next(iter(self._pending_dreq)))


    # ---- PTPv1 transmit -----------------------------------------------------
    def _v1_header(self, length: int, event: bool, seq: int, ctrl: int, flags: int = 0) -> bytearray:
        h = bytearray(length)
        struct.pack_into("!HH", h, 0, 1, 1)               # versionPTP 1, versionNetwork 1
        h[4:9] = b"_DFLT"                                  # subdomain
        h[20], h[21] = (1 if event else 2), 1              # event/general message, ethernet
        h[22:28] = self._v1_uuid
        struct.pack_into("!HHB", h, 28, 1, seq & 0xFFFF, ctrl)
        h[35] = flags
        return h

    def _v1_sync_body(self, h: bytearray, ns: int, leader: bool):
        """Fields shared by Sync and Delay_Req (IEEE 1588-2002 annex D)."""
        struct.pack_into("!II", h, 40, (ns // 1_000_000_000) & 0xFFFFFFFF, ns % 1_000_000_000)
        stratum = V1_STRATUM if leader else 255            # 255: "never a master"
        if leader:
            struct.pack_into("!h", h, 50, TAI_UTC_OFFSET)
            h[53] = 1
            h[54:60] = self._v1_uuid                       # we are grandmaster
            struct.pack_into("!HH", h, 60, 1, self._v1_seq)
            struct.pack_into("!H", h, 74, V1_VARIANCE)
            struct.pack_into("!b", h, 83, V1_LOG_SYNC)
            struct.pack_into("!H", h, 86, V1_VARIANCE)
            h[101] = 1
            h[102:108] = self._v1_uuid
            struct.pack_into("!H", h, 110, 1)
            h[123] = 1                                     # utcReasonable
        else:
            h[83] = 0x7F
        h[67] = h[95] = stratum
        h[68:72] = h[96:100] = b"DFLT"

    def _send_v1_sync(self):
        self._v1_seq = (self._v1_seq + 1) & 0xFFFF
        h = self._v1_header(124, True, self._v1_seq, 0, flags=0x08)   # PTP_ASSIST: Follow_Up carries the time
        t1 = self.now_ns()
        self._v1_sync_body(h, t1, True)
        self._send(h, EVENT_PORT)
        fu = self._v1_header(52, False, self._v1_seq, 2)
        struct.pack_into("!HII", fu, 42, self._v1_seq, (t1 // 1_000_000_000) & 0xFFFFFFFF, t1 % 1_000_000_000)
        self._send(fu, GENERAL_PORT)

    def _send_v1_delay_resp(self, req: bytes, rx: int):
        t4 = rx + (self.offset_ns or 0)
        h = self._v1_header(60, False, self._v1_seq, 3)
        struct.pack_into("!II", h, 40, (t4 // 1_000_000_000) & 0xFFFFFFFF, t4 % 1_000_000_000)
        h[49] = 1
        h[50:56] = req[22:28]                              # requesting uuid, port, sequence
        h[56:58] = req[28:30]
        h[58:60] = req[30:32]
        self._send(h, GENERAL_PORT)

    def _v1_delay_req(self, seq: int, t3: int) -> bytes:
        h = self._v1_header(124, True, seq, 1)
        self._v1_sync_body(h, t3 + (self.offset_ns or 0), False)
        return bytes(h)


def _ts_v1(b: bytes, off: int) -> int:
    sec, ns = struct.unpack_from("!II", b, off)
    return sec * 1_000_000_000 + ns


Follower = Clock  # backwards compatibility
