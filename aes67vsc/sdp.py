"""Minimal SDP for AES67 streams."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class Stream:
    name: str
    address: str
    port: int
    encoding: str = "L24"
    rate: int = 48000
    channels: int = 2
    ptime: float = 1.0
    payload_type: int = 96
    origin_ip: str = ""
    session_id: int = 0
    ptp_gmid: str = "00-00-00-00-00-00-00-00"
    ptp_domain: int = 0

    @property
    def frames_per_packet(self) -> int:
        return int(round(self.rate * self.ptime / 1000))


def build(s: Stream) -> str:
    lines = [
        "v=0",
        f"o=- {s.session_id} 0 IN IP4 {s.origin_ip}",
        f"s={s.name}",
        f"c=IN IP4 {s.address}/32",
        "t=0 0",
        f"m=audio {s.port} RTP/AVP {s.payload_type}",
        f"i={s.channels} channels",
        f"a=rtpmap:{s.payload_type} {s.encoding}/{s.rate}/{s.channels}",
        "a=recvonly",
        f"a=ptime:{s.ptime:g}",
        f"a=ts-refclk:ptp=IEEE1588-2008:{s.ptp_gmid}:{s.ptp_domain}",
        "a=mediaclk:direct=0",
    ]
    return "\r\n".join(lines) + "\r\n"


def parse(text: str) -> Stream | None:
    """Parse the first audio media section. Returns None if unusable."""
    name, addr, port, origin, sess = "", None, None, "", 0
    pt, enc, rate, ch, ptime = None, None, 48000, 1, 1.0
    gmid, dom = "00-00-00-00-00-00-00-00", 0
    in_media = False
    for line in text.replace("\r", "").split("\n"):
        k, _, v = line.partition("=")
        if k == "s":
            name = v.strip()
        elif k == "o":
            p = v.split()
            if len(p) >= 6:
                sess = int(p[1]) if p[1].isdigit() else 0
                origin = p[5]
        elif k == "c":
            p = v.split()
            if len(p) >= 3:
                addr = p[2].split("/")[0]
        elif k == "m":
            if in_media:
                break  # only first media section
            p = v.split()
            if p and p[0] == "audio":
                in_media = True
                port = int(p[1].split("/")[0])
                pt = int(p[3])
        elif k == "a" and in_media or k == "a" and v.startswith("ts-refclk"):
            m = re.match(r"rtpmap:(\d+) (\w+)/(\d+)(?:/(\d+))?", v)
            if m and int(m.group(1)) == pt:
                enc, rate, ch = m.group(2).upper(), int(m.group(3)), int(m.group(4) or 1)
            m = re.match(r"ptime:([\d.]+)", v)
            if m:
                ptime = float(m.group(1))
            m = re.match(r"ts-refclk:ptp=IEEE1588-\d+:([0-9A-Fa-f-]+)(?::(\d+))?", v)
            if m:
                gmid, dom = m.group(1).upper(), int(m.group(2) or 0)
    if addr is None or port is None or enc not in ("L16", "L24"):
        return None
    return Stream(name, addr, port, enc, rate, ch, ptime, pt, origin, sess, gmid, dom)
