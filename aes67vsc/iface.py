"""Pick the right network interface automatically.

1. Listen briefly on every candidate for PTP and SAP traffic: the interface
   where the audio network actually is wins (PTP > SAP).
2. Otherwise prefer wired over Wi-Fi, real NICs over virtual ones, then the
   default-route interface.
"""
from __future__ import annotations

import ipaddress
import re
import select
import socket
import struct
import time
from dataclasses import dataclass

from .net import default_interface_ip

VIRTUAL = re.compile(r"docker|veth|br-|virbr|vmnet|vbox|virtualbox|hyper-v|vethernet|utun|tun|tap|"
                     r"tailscale|zerotier|wg|wireguard|awdl|llw|bridge|loopback|^lo|ifb|npcap|vpn", re.I)
WIFI = re.compile(r"wi-?fi|wlan|^wl|wireless|airport|802\.11", re.I)


@dataclass
class Iface:
    name: str
    ip: str
    netmask: str
    speed: int = 0

    @property
    def network(self):
        return ipaddress.IPv4Network(f"{self.ip}/{self.netmask}", strict=False)

    @property
    def wifi(self):
        return bool(WIFI.search(self.name))

    @property
    def virtual(self):
        return bool(VIRTUAL.search(self.name))


def candidates() -> list[Iface]:
    try:
        import psutil
    except ImportError:
        ip = default_interface_ip()
        return [Iface("default", ip, "255.255.255.0")]
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        st = stats.get(name)
        if st and not st.isup:
            continue
        for a in addrs:
            if a.family == socket.AF_INET and not a.address.startswith(("127.", "169.254.")) and a.netmask:
                out.append(Iface(name, a.address, a.netmask, st.speed if st else 0))
    return out


def _probe(ifaces: list[Iface], seconds: float) -> dict[str, int]:
    """Score each interface by audio-network traffic seen on it."""
    from .ptp import PTP_GROUP
    from .sap import SAP_GROUP, SAP_PORT
    score = {i.ip: 0 for i in ifaces}
    socks = []
    for group, port, weight in ((PTP_GROUP, 320, 10), (SAP_GROUP, SAP_PORT, 3)):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        if hasattr(socket, "SO_REUSEPORT"):
            try:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
            except OSError:
                pass
        try:
            s.bind(("", port))
        except OSError:
            s.close()
            continue
        for i in ifaces:
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP,
                             struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(i.ip)))
            except OSError:
                pass
        socks.append((s, weight))
    local = {i.ip for i in ifaces}
    end = time.monotonic() + seconds
    while socks and time.monotonic() < end:
        r, _, _ = select.select([s for s, _ in socks], [], [], max(0, end - time.monotonic()))
        for s in r:
            _, (src, _) = s.recvfrom(2048)
            if src in local:
                continue
            w = dict(socks)[s]
            for i in ifaces:
                if ipaddress.IPv4Address(src) in i.network:
                    score[i.ip] += w
    for s, _ in socks:
        s.close()
    return score


def choose(probe_seconds: float = 2.0, verbose: bool = False) -> str:
    ifaces = [i for i in candidates()]
    if not ifaces:
        return default_interface_ip()
    if len(ifaces) == 1:
        return ifaces[0].ip
    traffic = _probe(ifaces, probe_seconds) if probe_seconds > 0 else {}
    default = default_interface_ip()

    def rank(i: Iface):
        return (-traffic.get(i.ip, 0), i.virtual, i.wifi, i.ip != default, -i.speed)

    best = sorted(ifaces, key=rank)
    if verbose:
        for i in best:
            print(f"  {i.name:16} {i.ip:15} traffic={traffic.get(i.ip, 0):<4} "
                  f"{'wifi ' if i.wifi else ''}{'virtual' if i.virtual else ''}")
    return best[0].ip
