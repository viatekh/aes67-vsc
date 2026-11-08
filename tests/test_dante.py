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


@pytest.fixture
def pair():
    ip = default_interface_ip()
    clk = FakeClock()
    a = DanteDevice(DeviceInfo("vsc-tx", ip, ["In 1", "In 2"], ["Out 1", "Out 2", "Out 3", "Out 4"],
                               arc_port=24440, cmc_port=28800, flows_port=24455, info_port=28700),
                    clk, source=Tones([440, 660, 1000, 1500]), log=lambda *a: None)
    out = Collector()
    b = DanteDevice(DeviceInfo("vsc-rx", ip, ["Mic L", "Mic R"], ["Send 1"],
                               arc_port=34440, cmc_port=38800, flows_port=34455, info_port=38700),
                    clk, source=Tones([300]), out_buffer=out, log=lambda *a: None)
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
    assert names == ["Out 1", "Out 2", "Out 3", "Out 4"]
    common = struct.unpack_from("!HHHH", body, 2)[2]
    assert struct.unpack_from("!I", pkt, common)[0] == RATE
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
    code, _, _ = arc(34440, ip, 0x3010, subscribe_request([(1, "Out 3", "vsc-tx"), (2, "Out 1", "vsc-tx")]))
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
    _, body, pkt = arc(34440, ip, 0x3000, page_request())
    ch_id, _, _, tco, tho, _, status, _ = struct.unpack_from("!HHHHHHII", body, 2)
    assert (ch_id, proto.read_cstr(pkt, tco), proto.read_cstr(pkt, tho), status) == \
        (1, "Out 3", "vsc-tx", RECEIVING_UNICAST)
    # flow listings on both sides
    _, body, _ = arc(24440, ip, 0x2200, page_request())
    assert body[1] == 1 and len(a.tx.flows) == 1
    _, body, _ = arc(34440, ip, 0x3200, page_request())
    assert body[1] == 1
    # unsubscribe everything: transmitter flow is stopped
    arc(34440, ip, 0x3010, subscribe_request([(1, None, None), (2, None, None)]))
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
                 dante=DanteCfg(enabled=True, name="Studio Mac", tx_channels=2, rx_channels=2))
    eng = Engine(cfg, log=lambda *a: None)
    eng.start()
    out = Collector()
    peer = DanteDevice(DeviceInfo("peer", ip, ["A", "B"], [], arc_port=44440, cmc_port=48800, flows_port=44455,
                                  info_port=48700), eng.clock, out_buffer=out, log=lambda *a: None)
    peer.start()
    play = None
    try:
        assert eng.dante and eng.dante.info.name == "Studio-Mac"
        _, body, _ = arc(4440, ip, 0x1002)
        assert body == b"Studio-Mac\0"
        t = np.arange(RATE * 4) / RATE
        tone = np.repeat((0.3 * np.sin(2 * np.pi * 750 * t)).astype("<f4")[:, None], 2, 1)
        play = subprocess.Popen(["pacat", "-p", "-d", "aes67_send_studio_mac_dante", "--format=float32le",
                                 "--rate=48000", "--channels=2", "--raw"], stdin=subprocess.PIPE)
        play.stdin.write(tone.tobytes())
        play.stdin.close()
        arc(44440, ip, 0x3010, subscribe_request([(1, "TX 1", "Studio-Mac")]))
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
