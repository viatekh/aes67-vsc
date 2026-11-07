"""aes67-vsc command line."""
from __future__ import annotations

import argparse
import random
import socket
import sys
import time

from . import sap, sdp
from .net import default_interface_ip
from .ptp import Follower


def _clock(args) -> Follower:
    clk = Follower(args.interface, args.ptp_domain)
    if not clk.start():
        print(f"! PTP unavailable ({clk.error}). Run with sudo/admin for PTP sync.", file=sys.stderr)
        return clk
    print("Waiting for PTP master...", end="", flush=True)
    for _ in range(30):
        if clk.locked:
            break
        time.sleep(0.1)
    print(f" locked to {clk.gm_id}" if clk.locked else " none found (using system clock)")
    return clk


def _wait(status):
    try:
        while True:
            time.sleep(1)
            print("\r" + status(), end="   ", flush=True)
    except KeyboardInterrupt:
        print()


def cmd_devices(args):
    from .audio import list_devices
    print(list_devices())


def _discover(iface, timeout, want=None):
    """Print streams as announced; return early once `want` (a name) is seen."""
    b = sap.Browser(iface).start()
    seen, end = [], time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            for s in b.list():
                if s not in seen:
                    seen.append(s)
                    print(f"  {s.name!r}  {s.address}:{s.port}  {s.encoding}/{s.rate}/{s.channels}ch "
                          f"ptime={s.ptime:g}ms  from {s.origin_ip}")
                    if want is not None and s.name == want:
                        return seen
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    finally:
        b.stop()
    return seen


def cmd_list(args):
    print(f"Listening for AES67 (SAP) streams on {args.interface} for {args.time:g}s (Ctrl+C to stop)...")
    if not _discover(args.interface, args.time):
        print("No streams found. In Dante Controller: enable AES67 mode on the device, "
              "then create an AES67 multicast transmit flow.")


def cmd_receive(args):
    from .receiver import Receiver
    if not args.address and not args.stream:
        sys.exit("Give a stream name (see `aes67-vsc list`) or --address.")
    if args.address:
        s = sdp.Stream("manual", args.address, args.port, args.encoding, args.rate, args.channels, args.ptime)
    else:
        print(f"Looking for stream {args.stream!r}...")
        match = [x for x in _discover(args.interface, args.time, args.stream) if x.name == args.stream]
        if not match:
            sys.exit(f"Stream {args.stream!r} not found. Try `aes67-vsc list`.")
        s = match[0]
    print(f"Receiving {s.name!r} {s.address}:{s.port} {s.encoding}/{s.rate}/{s.channels}ch")
    rx = Receiver(s, args.interface, args.latency)
    rx.start()
    sink = None
    if not args.null:
        from .audio import DeviceSink
        sink = DeviceSink(rx.ring, s.rate, rx.target, args.device)
    _wait(lambda: f"packets={rx.packets} lost={rx.lost} buffered={len(rx.ring) * 1000 // s.rate}ms")
    rx.stop()
    if sink:
        sink.close()


def cmd_send(args):
    from .audio import DeviceSource, ToneSource
    from .sender import Sender
    clk = _clock(args)
    ip = args.interface
    addr = args.address or f"239.69.{ip.split('.')[2]}.{ip.split('.')[3]}"
    s = sdp.Stream(args.name, addr, args.port, args.encoding, 48000, args.channels, args.ptime,
                   96, ip, random.getrandbits(31), clk.gm_id or "00-00-00-00-00-00-00-00", args.ptp_domain)
    src = (ToneSource(s.channels, s.rate, args.tone) if args.tone
           else DeviceSource(s.channels, s.rate, args.device))
    tx = Sender(s, src, clk, ip)
    tx.start()
    print(f"Sending {s.name!r} to {s.address}:{s.port} ({s.encoding}/48000/{s.channels}ch, "
          f"{s.ptime:g}ms). Ctrl+C to stop.")
    _wait(lambda: f"packets={tx.packets} ptp={'locked' if clk.locked else 'UNSYNCED'}")
    tx.stop()


def main(argv=None):
    p = argparse.ArgumentParser(prog="aes67-vsc", description="Basic AES67 virtual soundcard "
                                "(talks to Dante gear in AES67 mode).")
    p.add_argument("-i", "--interface", default=None, help="IP of the network interface to use")
    p.add_argument("--ptp-domain", type=int, default=0)
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list sound cards").set_defaults(fn=cmd_devices)

    q = sub.add_parser("list", help="discover AES67 streams on the network")
    q.add_argument("-t", "--time", type=float, default=35, help="seconds to listen (default 35)")
    q.set_defaults(fn=cmd_list)

    q = sub.add_parser("receive", help="play a network stream on a sound card")
    q.add_argument("stream", nargs="?", help="stream name as shown by `list`")
    q.add_argument("-d", "--device", default=None, help="output device (name or index)")
    q.add_argument("-l", "--latency", type=float, default=10, help="buffer in ms (default 10)")
    q.add_argument("-t", "--time", type=float, default=60, help="max seconds to wait for the stream")
    q.add_argument("--null", action="store_true", help="don't play audio, just show stats")
    m = q.add_argument_group("manual stream (skip discovery)")
    m.add_argument("--address")
    m.add_argument("--port", type=int, default=5004)
    m.add_argument("--channels", type=int, default=2)
    m.add_argument("--rate", type=int, default=48000)
    m.add_argument("--encoding", default="L24", choices=["L16", "L24"])
    m.add_argument("--ptime", type=float, default=1.0)
    q.set_defaults(fn=cmd_receive)

    q = sub.add_parser("send", help="send a sound card input (or test tone) to the network")
    q.add_argument("-n", "--name", default=socket.gethostname()[:30])
    q.add_argument("-c", "--channels", type=int, default=2, help="1-8 (Dante AES67 max is 8)")
    q.add_argument("-d", "--device", default=None, help="input device (name or index)")
    q.add_argument("--tone", type=float, default=None, metavar="HZ", help="send a test tone instead")
    q.add_argument("--address", help="multicast address (default 239.69.x.y from your IP)")
    q.add_argument("--port", type=int, default=5004)
    q.add_argument("--encoding", default="L24", choices=["L16", "L24"])
    q.add_argument("--ptime", type=float, default=1.0)
    q.set_defaults(fn=cmd_send)

    args = p.parse_args(argv)
    args.interface = args.interface or default_interface_ip()
    if getattr(args, "device", None) is not None and str(args.device).isdigit():
        args.device = int(args.device)
    args.fn(args)


if __name__ == "__main__":
    main()
