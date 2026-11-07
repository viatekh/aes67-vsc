import struct
import time

import numpy as np

from aes67vsc import ptp, rtp, sap, sdp
from aes67vsc.audio import Ring, ToneSource


def test_pcm_roundtrip():
    x = np.random.uniform(-1, 1, (48, 2)).astype(np.float32)
    for enc, tol in (("L24", 2e-7), ("L16", 3.1e-5)):
        y = rtp.decode(rtp.encode(x, enc), enc, 2)
        assert np.abs(x - y).max() < tol
    assert rtp.encode(np.array([[-1.0]], np.float32), "L24") == b"\x80\x00\x00"


def test_rtp_header():
    pkt = rtp.pack(96, 65535, 2**32 + 5, 1234, b"abc")
    assert rtp.unpack(pkt) == (96, 65535, 5, 1234, b"abc")


def test_sdp_sap_roundtrip():
    s = sdp.Stream("Test PC", "239.69.1.2", 5004, "L24", 48000, 8, 1.0, 96, "10.0.0.5", 42,
                   "00-1D-C1-FF-FE-12-34-56", 0)
    pkt = sap.build(sdp.build(s), s.origin_ip)
    delete, origin, _, text = sap.parse(pkt)
    assert not delete and origin == "10.0.0.5"
    assert sdp.parse(text) == s


def test_parse_dante_style_sdp():
    text = ("v=0\r\no=- 1311738121 1311738121 IN IP4 192.168.1.20\r\ns=AVIO-123 : 32\r\n"
            "c=IN IP4 239.69.83.67/32\r\nt=0 0\r\na=keywds:Dante\r\nm=audio 5004 RTP/AVP 97\r\n"
            "i=2 channels: Left, Right\r\na=recvonly\r\na=rtpmap:97 L24/48000/2\r\na=ptime:1\r\n"
            "a=ts-refclk:ptp=IEEE1588-2008:00-1D-C1-FF-FE-0E-10-C4:0\r\na=mediaclk:direct=750129868\r\n")
    s = sdp.parse(text)
    assert (s.name, s.address, s.port, s.payload_type, s.channels) == ("AVIO-123 : 32", "239.69.83.67", 5004, 97, 2)
    assert s.ptp_gmid == "00-1D-C1-FF-FE-0E-10-C4"


def test_ptp_two_step_offset():
    f = ptp.Follower("127.0.0.1")
    def hdr(mtype, seq, flags=0):
        h = bytearray(34)
        h[0], h[1], h[4], h[6] = mtype, 2, 0, flags
        h[20:28] = bytes.fromhex("001dc1fffe0e10c4")
        struct.pack_into("!H", h, 30, seq)
        return h
    sync = bytes(hdr(0, 7, 0x02)) + bytes(10)
    master_ns = 1_700_000_037_123_456_789
    fu = bytes(hdr(8, 7)) + (master_ns // 10**9).to_bytes(6, "big") + (master_ns % 10**9).to_bytes(4, "big")
    f._master_port = bytes.fromhex("001dc1fffe0e10c4") + b"\x00\x00"
    f.state = "follower"
    f._handle(sync, 1_000)
    f._handle(fu, 2_000)
    assert f.offset_ns == master_ns - 1_000


def test_ring_wraps():
    r = Ring(1, 10)
    r.write(np.arange(8, dtype=np.float32)[:, None])
    r.read(6)
    r.write(np.arange(8, 14, dtype=np.float32)[:, None])
    assert r.read(100)[:, 0].tolist() == list(range(6, 14))


def test_loopback_send_discover_receive():
    """Full path: tone -> RTP multicast + SAP -> discovery -> receive -> verify 1 kHz."""
    from aes67vsc.receiver import Receiver
    from aes67vsc.sender import Sender

    ip = "127.0.0.1"
    clk = ptp.Follower(ip)  # not started: falls back to system clock
    s = sdp.Stream("loop test", "239.69.250.1", 5004, "L24", 48000, 2, 1.0, 96, ip, 99)
    browser = sap.Browser(ip).start()
    tx = Sender(s, ToneSource(2, 48000, 1000.0, 0.5), clk, ip)
    tx.start()
    try:
        for _ in range(40):
            found = [x for x in browser.list() if x.name == "loop test"]
            if found:
                break
            time.sleep(0.1)
        assert found, "SAP announcement not discovered"
        rx = Receiver(found[0], ip, latency_ms=200)
        rx.start()
        time.sleep(1.0)
        rx.stop()
    finally:
        tx.stop()
        browser.stop()
    assert rx.packets > 800, rx.packets
    audio = rx.buf.ring.read(len(rx.buf.ring))
    assert len(audio) >= 480
    spectrum = np.abs(np.fft.rfft(audio[:, 0]))
    peak_hz = np.argmax(spectrum) * 48000 / len(audio)
    assert abs(peak_hz - 1000) < 10, peak_hz
    assert 0.45 < np.abs(audio[:, 0]).max() < 0.55
