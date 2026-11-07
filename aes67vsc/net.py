"""Socket helpers."""
import socket
import sys
import struct


def default_interface_ip() -> str:
    """Best guess at the LAN IP (no packets are actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("239.255.255.255", 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


def multicast_rx(group: str, port: int, iface_ip: str) -> socket.socket:
    """UDP socket bound to `port` and joined to `group` on `iface_ip`."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    if hasattr(socket, "SO_REUSEPORT"):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
        except OSError:
            pass
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1 << 20)
    # Bind to the group on POSIX so sockets on the same port only get their own group.
    # Windows can't bind to a multicast address, but already filters by membership.
    s.bind(("" if sys.platform == "win32" else group, port))
    if sys.platform.startswith("linux"):
        s.setsockopt(socket.IPPROTO_IP, 49, 0)  # IP_MULTICAST_ALL = 0
    mreq = struct.pack("4s4s", socket.inet_aton(group), socket.inet_aton(iface_ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return s


def multicast_tx(iface_ip: str, ttl: int = 15) -> socket.socket:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, ttl)
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(iface_ip))
    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    # DSCP EF (46) - what AES67/Dante use for audio
    try:
        s.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, 46 << 2)
    except OSError:
        pass
    return s
