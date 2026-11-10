"""Two virtual Dante devices on one machine: discovery, Dante Controller (ARC) queries,
routing a subscription, and audio flowing between them."""
import socket
import struct
import time

import numpy as np
import pytest

from aes67vsc.dante import proto
from aes67vsc.dante.device import RECEIVING_UNICAST, DanteDevice, DeviceInfo
from aes67vsc.net import default_interface_ip

RATE = 48000


class FakeClock:
    locked, gm_id, state = True, "00-1D-C1-FF-FE-11-22-33", "follower"

    def now_ns(self):
        return time.time_ns()


class Tones:
    def __init__(self, freqs):
        self.freqs, self.pos = freqs, 0

    def read(self, n):
        t = (self.pos + np.arange(n)) / RATE
        self.pos += n
        return np.stack([0.4 * np.sin(2 * np.pi * f * t) for f in self.freqs], 1).astype(np.float32)


class Collector:
    def __init__(self):
        self.chunks = []

    def write(self, frames):
        self.chunks.append(frames)

    def audio(self):
        return np.concatenate(self.chunks) if self.chunks else np.zeros((0, 2))


def arc(port, ip, op1, content=b"", seq=7):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    s.sendto(proto.make_packet(0x27FF, seq, op1, 0, content), (ip, port))
    pkt = s.recv(4096)
    s.close()
    start, rseq, rop1, code, body = proto.parse_packet(pkt)
    assert (start, rseq, rop1) == (0x27FF, seq, op1)
    assert struct.unpack_from("!H", pkt, 2)[0] == len(pkt)
    return code, body, pkt


def page_request(start=1):
    return struct.pack("!HH", 0x0001, start)


def wait(cond, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.1)
    return False


def peak_hz(x):
    return np.argmax(np.abs(np.fft.rfft(x))) * RATE / len(x)


@pytest.fixture
def pair(tmp_path):
    ip = default_interface_ip()
    clk = FakeClock()
    a = DanteDevice(DeviceInfo("vsc-tx", ip, ["In 1", "In 2"], ["Out 1", "Out 2", "Out 3", "Out 4"],
                               arc_port=24440, cmc_port=28800, flows_port=24455, info_port=28700),
                    clk, source=Tones([440, 660, 1000, 1500]), log=lambda *a: None,
                    state_path=tmp_path / "a.json")
    out = Collector()
    b = DanteDevice(DeviceInfo("vsc-rx", ip, ["Mic L", "Mic R"], ["Send 1"],
                               arc_port=25440, cmc_port=29800, flows_port=25455, info_port=29700,
                               process_id=1),
                    clk, source=Tones([300]), out_buffer=out, log=lambda *a: None, state_path=tmp_path / "b.json")
    a.start()
    b.start()
    yield ip, a, b, out
    a.stop()
    b.stop()


def test_controller_queries(pair):
    ip, a, b, _ = pair
    code, body, _ = arc(24440, ip, 0x1000)
    assert code == 1 and len(body) == 34
    assert struct.unpack_from("!HH", body, 2) == (4, 2)  # tx, rx channel counts
    _, body, _ = arc(24440, ip, 0x1002)
    assert body == b"vsc-tx\0"
    _, body, pkt = arc(24440, ip, 0x1003)
    off = struct.unpack_from("!H", body, 12)[0]  # friendly name offset (absolute in packet)
    assert proto.read_cstr(pkt, off) == "vsc-tx"
    assert struct.unpack_from("!H", body, 30)[0] == 0x2729
    # transmit channels (paginated): [space, count, (id, 7, common_off, name_off) * count, data...]
    code, body, pkt = arc(24440, ip, 0x2000, page_request())
    assert code == 1 and body[1] == 4
    names = [proto.read_cstr(pkt, struct.unpack_from("!HHHH", body, 2 + 8 * k)[3]) for k in range(4)]
    assert names == ["01", "02", "03", "04"]  # factory names; labels come from 0x2010
    common = struct.unpack_from("!HHHH", body, 2)[2]
    assert struct.unpack_from("!I", pkt, common)[0] == RATE
    code, body, pkt = arc(24440, ip, 0x2010, page_request())
    labels = [proto.read_cstr(pkt, struct.unpack_from("!HHH", body, 2 + 6 * k)[2]) for k in range(4)]
    assert labels == ["Out 1", "Out 2", "Out 3", "Out 4"]
    # CMC advertisement points at our info port
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(2)
    s.sendto(proto.make_packet(0xFFFF, 1, 0x1001, 0, b""), (ip, 28800))
    _, _, _, code, body = proto.parse_packet(s.recv(512))
    assert code == 1 and struct.unpack_from("!H", body, 18)[0] == 28700


def subscribe_request(items):
    """Build a 0x3010 request like Dante Controller: items = [(local_ch, tx_channel, tx_host)]."""
    buf = proto.Buf(b"\0" * proto.HEADER)
    buf.u8(len(items))
    buf.u8(len(items))
    pos = buf.wpos
    buf.write(b"\0" * (6 * len(items)))
    for k, (ch, txc, txh) in enumerate(items):
        co = buf.cstr(txc) if txc else 0
        ho = buf.cstr(txh) if txh else 0
        buf.b[pos + 6 * k:pos + 6 * k + 6] = struct.pack("!HHH", ch, co, ho)
    return buf.bytes()[proto.HEADER:]


def test_route_audio_between_devices(pair):
    ip, a, b, out = pair
    # Dante Controller: vsc-rx "Mic L" <- Out 3 (1000 Hz), "Mic R" <- Out 1 (440 Hz)
    code, _, _ = arc(25440, ip, 0x3010, subscribe_request([(1, "Out 3", "vsc-tx"), (2, "Out 1", "vsc-tx")]))
    assert code == 1
    for _ in range(60):
        if all(s and s.status == RECEIVING_UNICAST for s in b.subs) and len(out.audio()) > RATE:
            break
        time.sleep(0.1)
    assert all(s and s.status == RECEIVING_UNICAST for s in b.subs), [s and s.status for s in b.subs]
    time.sleep(0.5)
    y = out.audio()[-RATE // 2:]
    peak = lambda x: np.argmax(np.abs(np.fft.rfft(x))) * RATE / len(x)
    assert abs(peak(y[:, 0]) - 1000) < 5 and abs(peak(y[:, 1]) - 440) < 5
    assert 0.35 < np.abs(y).max() < 0.45
    # receive-channel listing now shows the subscription as Dante Controller would read it
    _, body, pkt = arc(25440, ip, 0x3000, page_request())
    ch_id, _, _, tco, tho, _, status, _ = struct.unpack_from("!HHHHHHII", body, 2)
    assert (ch_id, proto.read_cstr(pkt, tco), proto.read_cstr(pkt, tho), status) == \
        (1, "Out 3", "vsc-tx", RECEIVING_UNICAST)
    # flow listings on both sides
    _, body, _ = arc(24440, ip, 0x2200, page_request())
    assert body[1] == 1 and len(a.tx.flows) == 1
    _, body, _ = arc(25440, ip, 0x3200, page_request())
    assert body[1] == 1
    # unsubscribe everything: transmitter flow is stopped
    arc(25440, ip, 0x3010, subscribe_request([(1, None, None), (2, None, None)]))
    for _ in range(40):
        if not a.tx.flows:
            break
        time.sleep(0.1)
    assert not a.tx.flows


@pytest.mark.skipif(not __import__("aes67vsc.virtual", fromlist=["x"]).pulse_available(), reason="needs PulseAudio")
def test_engine_dante_device_with_virtual_soundcard():
    """Full app: Dante device + virtual sound cards; a Dante peer pulls audio an app plays."""
    import subprocess
    from aes67vsc.engine import Config, DanteCfg, Engine
    ip = default_interface_ip()
    cfg = Config(interface=ip, ptp_can_lead=True, nmos=False,
                 dante=DanteCfg(enabled=True, name="Engine Test", tx_channels=2, rx_channels=2))
    eng = Engine(cfg, log=lambda *a: None)
    eng.start()
    out = Collector()
    peer = DanteDevice(DeviceInfo("peer", ip, ["A", "B"], [], arc_port=26440, cmc_port=30800, flows_port=26455,
                                  info_port=30700), eng.clock, out_buffer=out, log=lambda *a: None)
    peer.start()
    play = None
    try:
        assert eng.dante
        _, body, _ = arc(4440, ip, 0x1002)
        assert body == eng.dante.info.name.encode() + b"\0"
        t = np.arange(RATE * 4) / RATE
        tone = np.repeat((0.3 * np.sin(2 * np.pi * 750 * t)).astype("<f4")[:, None], 2, 1)
        play = subprocess.Popen(["pacat", "-p", "-d", "aes67_send_engine_test_dante", "--format=float32le",
                                 "--rate=48000", "--channels=2", "--raw"], stdin=subprocess.PIPE)
        play.stdin.write(tone.tobytes())
        play.stdin.close()
        arc(26440, ip, 0x3010, subscribe_request([(1, "TX 1", eng.dante.info.name)]))
        for _ in range(60):
            if peer.subs[0] and peer.subs[0].status == RECEIVING_UNICAST and len(out.audio()) > RATE:
                break
            time.sleep(0.1)
        y = out.audio()[-RATE // 2:, 0]
        assert abs(np.argmax(np.abs(np.fft.rfft(y))) * RATE / len(y) - 750) < 5
    finally:
        if play:
            play.kill()
        peer.stop()
        eng.stop()


def test_factory_and_label_names_both_route(pair):
    ip, a, b, out = pair
    arc(25440, ip, 0x3010, subscribe_request([(1, "02", "vsc-tx"), (2, "Out 4", "vsc-tx")]))
    assert wait(lambda: all(s and s.status == RECEIVING_UNICAST for s in b.subs) and len(out.audio()) > RATE)
    time.sleep(0.4)
    y = out.audio()[-RATE // 2:]
    assert abs(peak_hz(y[:, 0]) - 660) < 5 and abs(peak_hz(y[:, 1]) - 1500) < 5


def rename_tx_request(ch, name):
    buf = proto.Buf(b"\0" * proto.HEADER)
    buf.u8(1)
    buf.u8(1)
    pos = buf.wpos
    buf.write(b"\0" * 6)
    off = buf.cstr(name)
    buf.b[pos:pos + 6] = struct.pack("!HHH", 0, ch, off)
    return buf.bytes()[proto.HEADER:]


def test_rename_channel_and_device_from_controller(pair):
    ip, a, b, out = pair
    code, _, _ = arc(24440, ip, 0x2013, rename_tx_request(3, "Kick"))
    assert code == 1 and a.info.tx_names[2] == "Kick"
    arc(25440, ip, 0x3010, subscribe_request([(1, "Kick", "vsc-tx")]))
    assert wait(lambda: b.subs[0] and b.subs[0].status == RECEIVING_UNICAST and len(out.audio()) > RATE // 2)
    # device rename: old name disappears, subscriptions by the new name resolve
    code, _, _ = arc(24440, ip, 0x1001, b"FOH-Desk\0")
    assert code == 1
    assert wait(lambda: a.info.name == "FOH-Desk")
    a.mdns.flush()
    arc(25440, ip, 0x3010, subscribe_request([(2, "01", "FOH-Desk")]))
    assert wait(lambda: b.subs[1] and b.subs[1].status == RECEIVING_UNICAST, 12)
    assert arc(24440, ip, 0x1002)[1] == b"FOH-Desk\0"
    assert arc(24440, ip, 0x1001, b"bad name!\0")[0] != 1  # invalid names are refused


def create_multicast_request(flow_id, channels):
    buf = proto.Buf(b"\0" * proto.HEADER)
    buf.u8(1)
    buf.u8(1)
    pos = buf.wpos
    buf.u16(0)
    off = buf.wpos
    buf.write(struct.pack("!HH10sH", flow_id, 2, b"", len(channels)))
    for ch in channels:
        buf.u16(ch)
    buf.u16(0)
    buf.b[pos:pos + 2] = struct.pack("!H", off)
    return buf.bytes()[proto.HEADER:]


def test_multicast_flow_create_receive_delete(pair):
    ip, a, b, out = pair
    code, body, _ = arc(24440, ip, 0x2201, create_multicast_request(5, [2, 4]))  # Out 2 + Out 4
    assert code == 1 and struct.unpack("!HHH", body) == (1, 0, 5)
    assert 4 in a.bundles and a.tx.flows[4].multicast
    a.mdns.flush()
    # the transmit flow listing shows it as multicast (type 2)
    _, body, pkt = arc(24440, ip, 0x2200, page_request())
    main = struct.unpack_from("!H", body, 2)[0]
    assert struct.unpack_from("!HH", pkt, main) == (5, 2)
    # a receiver subscribing to a channel in the bundle listens to the multicast instead of asking for unicast
    from aes67vsc.dante.device import RECEIVING_MULTICAST
    arc(25440, ip, 0x3010, subscribe_request([(1, "Out 4", "vsc-tx"), (2, "Out 1", "vsc-tx")]))
    assert wait(lambda: b.subs[0] and b.subs[0].status == RECEIVING_MULTICAST
                and b.subs[1] and b.subs[1].status == RECEIVING_UNICAST and len(out.audio()) > RATE, 12)
    time.sleep(0.5)
    y = out.audio()[-RATE // 2:]
    assert abs(peak_hz(y[:, 0]) - 1500) < 5 and abs(peak_hz(y[:, 1]) - 440) < 5
    assert sum(1 for f in a.tx.flows.values() if not f.multicast) == 1  # only "Out 1" needed unicast
    code, _, _ = arc(24440, ip, 0x2202, struct.pack("!HHH", 1, 0, 5))
    assert code == 1 and 4 not in a.bundles and 4 not in a.tx.flows


def test_state_survives_restart(tmp_path):
    ip = default_interface_ip()
    ports = dict(arc_port=27440, cmc_port=31800, flows_port=27455, info_port=31700)
    path = tmp_path / "dev.json"

    def make():
        return DanteDevice(DeviceInfo("keeper", ip, ["A", "B"], ["X", "Y"], **ports), FakeClock(),
                           source=Tones([100, 200]), out_buffer=Collector(), log=lambda *a: None, state_path=path)
    d = make()
    d.start()
    arc(27440, ip, 0x3010, subscribe_request([(2, "Src", "other-device")]))
    arc(27440, ip, 0x2013, rename_tx_request(1, "Vocals"))
    arc(27440, ip, 0x2201, create_multicast_request(3, [1, 2]))
    arc(27440, ip, 0x1101, bytes(22) + struct.pack("!II", 5_000_000, 5_000_000))  # set latency 5 ms
    group = d.bundles[2][0]
    d.stop()
    d = make()
    d.start()
    try:
        assert d.subs[1] and (d.subs[1].tx_channel, d.subs[1].tx_host) == ("Src", "other-device")
        assert d.info.tx_names == ["Vocals", "Y"] and d.info.latency_ns == 5_000_000
        assert d.bundles[2] == (group, [0, 1]) and d.tx.flows[2].multicast
        # Device View in Dante Controller reads latency + sample rate from 0x1100
        _, body, pkt = arc(27440, ip, 0x1100)
        props = {struct.unpack_from("!HH", body, 2 + 4 * k)[0]: struct.unpack_from("!HH", body, 2 + 4 * k)[1]
                 for k in range(body[1])}
        assert struct.unpack_from("!I", pkt, props[0x8020])[0] == RATE
        assert struct.unpack_from("!I", pkt, props[0x8205])[0] == 5_000_000
    finally:
        d.stop()


def test_late_packets_are_not_replayed():
    """A packet that arrives after its play time must be dropped, not played a ring-length later."""
    from aes67vsc.dante.flows import Receiver, make_audio_packet
    clk, out = FakeClock(), Collector()
    rx = Receiver(clk, RATE, 1, "127.0.0.1", out)
    f = rx.new_flow([[0]], 3, RATE // 100)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        time.sleep(0.2)
        now = clk.now_ns() * RATE // 1_000_000_000
        loud = np.full((48, 1), 0.9, np.float32)
        s.sendto(make_audio_packet(now - RATE // 2, RATE, loud, 3), ("127.0.0.1", f.port))  # 0.5 s too late
        assert wait(lambda: rx.late == 1, 2)
        time.sleep(1.6)  # longer than the ring (65536 samples = 1.37 s)
        assert np.abs(out.audio()).max() == 0
    finally:
        rx.stop()
        s.close()


def test_status_messages_and_meters(pair):
    """Sample-rate/encoding status and level meters, as Dante Controller listens for them."""
    ip, a, b, _ = pair
    from aes67vsc.net import multicast_rx
    info = multicast_rx("224.0.0.231", 8702, ip)
    hb = multicast_rx("224.0.0.233", 8708, ip)
    info.settimeout(3)
    hb.settimeout(3)
    try:
        req = proto.make_mcast(0xFFFF, 9, 0, bytes(8), b"Audinate", bytes([7, 0x2A, 0, 0x81, 0, 0, 0, 0]), bytes(8))
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.sendto(req, (ip, 28700))
        got = None
        for _ in range(20):
            pkt = info.recv(2048)
            if pkt[8:16] == a.info.device_id and pkt[27] == 0x80:
                got = pkt[32:]
                break
        assert got is not None
        size, count, current = struct.unpack_from("!HHI", got)
        assert (size, current) == (0x18, RATE) and RATE in struct.unpack_from(f"!{count}I", got, 16)
        meters = None
        for _ in range(20):
            pkt = hb.recv(2048)
            body = pkt[32:]
            if pkt[8:16] != a.info.device_id or len(body) < 40 or struct.unpack_from("!H", body, 18)[0] != 0x8002:
                continue
            ntx, _, nrx = struct.unpack_from("!HHH", body, 28)
            meters = body[40:40 + ntx + nrx]
            break
        assert meters is not None and len(meters) == 6
        assert all(m < 30 for m in meters[:4])   # four TX tones at 0.4 amplitude: ~8 dB below full scale
        assert all(m >= 0xFE for m in meters[4:])  # nothing routed to RX: silence
    finally:
        info.close()
        hb.close()
        s.close()


def test_resolve_tolerates_other_fpp_formats(pair):
    ip, a, b, _ = pair
    a.mdns.flush()
    r = b._resolve_channel("Out 1", "vsc-tx")
    assert r and r["id"] == 1 and (r["fpp_max"], r["fpp_min"]) == (32, 2)
    import re
    assert [int(x) for x in re.findall(r"\d+", "48,6-48")] == [48, 6, 48]
