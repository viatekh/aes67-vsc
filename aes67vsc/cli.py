"""aes67-vsc command line."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from . import iface, sap
from .engine import Config, Engine, RecvCfg, SendCfg, config_path


def _ip(args) -> str:
    return iface.choose() if args.interface in (None, "auto") else args.interface


def _run(cfg: Config):
    eng = Engine(cfg)
    eng.start()
    try:
        while True:
            time.sleep(1)
            print("\r" + eng.status(), end="   ", flush=True)
    except KeyboardInterrupt:
        print()
    finally:
        eng.stop()


def cmd_devices(args):
    from .audio import list_devices
    print(list_devices())


def cmd_interfaces(args):
    print("Probing interfaces for PTP/SAP traffic...")
    print("Chosen:", iface.choose(3, verbose=True))


def cmd_list(args):
    ip = _ip(args)
    print(f"Listening for AES67 (SAP) streams on {ip} for {args.time:g}s (Ctrl+C to stop)...")
    b = sap.Browser(ip).start()
    seen, end = [], time.monotonic() + args.time
    try:
        while time.monotonic() < end:
            for s in b.list():
                if s not in seen:
                    seen.append(s)
                    print(f"  {s.name!r}  {s.address}:{s.port}  {s.encoding}/{s.rate}/{s.channels}ch "
                          f"ptime={s.ptime:g}ms  from {s.origin_ip}")
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass
    b.stop()
    if not seen:
        print("No streams found. In Dante Controller: enable AES67 mode on the device, "
              "then create an AES67 multicast transmit flow.")


def _base_cfg(args) -> Config:
    return Config(interface=args.interface or "auto", ptp_domain=args.ptp_domain,
                  ptp_can_lead=not args.no_lead)


def cmd_send(args):
    cfg = _base_cfg(args)
    cfg.sends.append(SendCfg(args.name, args.channels, args.device, args.tone, args.rate,
                             args.encoding, args.ptime, args.address, args.port))
    _run(cfg)


def cmd_receive(args):
    cfg = _base_cfg(args)
    cfg.receives.append(RecvCfg(args.stream, args.device, args.latency, args.null))
    _run(cfg)


def cmd_run(args):
    path = Path(args.config) if args.config else config_path()
    cfg = Config.load(path)
    if not path.exists():
        cfg.save(path)
        print(f"Created default config at {path}")
    if args.interface:
        cfg.interface = args.interface
    _run(cfg)


def cmd_config(args):
    path = config_path()
    if not path.exists():
        Config.load(path).save(path)
    print(path)
    print(path.read_text())


def main(argv=None):
    p = argparse.ArgumentParser(prog="aes67-vsc", description="AES67 virtual soundcard "
                                "(plug-and-play with Dante gear in AES67 mode).")
    p.add_argument("-i", "--interface", default=None, help="interface IP (default: auto-detect)")
    p.add_argument("--ptp-domain", type=int, default=0)
    p.add_argument("--no-lead", action="store_true", help="never act as PTP clock leader")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list sound cards").set_defaults(fn=cmd_devices)
    sub.add_parser("interfaces", help="show network interface auto-detection").set_defaults(fn=cmd_interfaces)
    sub.add_parser("config", help="show config file path and contents").set_defaults(fn=cmd_config)

    q = sub.add_parser("run", help="run everything in the config file (default command for daily use)")
    q.add_argument("config", nargs="?", help="config file (default: per-user config)")
    q.set_defaults(fn=cmd_run)

    q = sub.add_parser("list", help="discover AES67 streams on the network")
    q.add_argument("-t", "--time", type=float, default=35, help="seconds to listen (default 35)")
    q.set_defaults(fn=cmd_list)

    q = sub.add_parser("receive", help="play a network stream (auto-connects when it appears)")
    q.add_argument("stream", help="stream name as shown by `list`")
    q.add_argument("-d", "--device", default=None, help="output device (name or index)")
    q.add_argument("-l", "--latency", type=float, default=10, help="buffer in ms (default 10)")
    q.add_argument("--null", action="store_true", help="don't play audio, just show stats")
    q.set_defaults(fn=cmd_receive)

    q = sub.add_parser("send", help="send a sound card input (or test tone) to the network")
    q.add_argument("-n", "--name", default=None, help="stream name (default: computer name)")
    q.add_argument("-c", "--channels", type=int, default=2, help="1-8 (Dante AES67 max is 8)")
    q.add_argument("-d", "--device", default=None, help="input device (name or index)")
    q.add_argument("-r", "--rate", type=int, default=48000, choices=[44100, 48000, 96000])
    q.add_argument("--tone", type=float, default=None, metavar="HZ", help="send a test tone instead")
    q.add_argument("--address", help="multicast address (default: auto in 239.69.x.x)")
    q.add_argument("--port", type=int, default=5004)
    q.add_argument("--encoding", default="L24", choices=["L16", "L24"])
    q.add_argument("--ptime", type=float, default=1.0)
    q.set_defaults(fn=cmd_send)

    args = p.parse_args(argv)
    if getattr(args, "device", None) is not None and str(args.device).isdigit():
        args.device = int(args.device)
    if getattr(args, "name", "") is None:
        import socket
        args.name = socket.gethostname()[:28] or "aes67-vsc"
    args.fn(args)


if __name__ == "__main__":
    main()
