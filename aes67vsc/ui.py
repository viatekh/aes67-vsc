"""Local web control panel (http://127.0.0.1:8067) + optional tray icon."""
from __future__ import annotations

import json
import threading
import webbrowser
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources

from . import system
from .engine import Config, Engine, RecvCfg, SendCfg

UI_PORT = 8067


class App:
    def __init__(self, cfg_path=None, port: int = UI_PORT):
        self.cfg_path = cfg_path
        self.cfg = Config.load(cfg_path)
        if cfg_path is None or not cfg_path.exists():
            self.cfg.save(cfg_path)
        self.logs: list[str] = []
        self.engine: Engine | None = None
        self._lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}/"

    def log(self, msg):
        self.logs = (self.logs + [msg])[-200:]
        print(msg)

    def start(self):
        threading.Thread(target=self.server.serve_forever, daemon=True, name="ui").start()
        self.restart()

    def restart(self):
        with self._lock:
            if self.engine:
                self.engine.stop()
            self.engine = Engine(self.cfg, log=self.log)
            threading.Thread(target=self.engine.start, daemon=True).start()

    def stop(self):
        if self.engine:
            self.engine.stop()
        self.server.shutdown()

    # ----------------------------------------------------------- API
    def state(self):
        e = self.engine
        rx = []
        for i, r in enumerate(self.cfg.receives):
            cur = e.receivers.get(i) if e else None
            rx.append({**asdict(r), "connected": cur[2].name if cur else None,
                       "packets": cur[0].packets if cur else 0, "lost": cur[0].lost if cur else 0,
                       "drift_ppm": round((cur[0].buf.ratio - 1) * 1e6) if cur else 0})
        tx = [{**asdict(s), "packets": t.packets, "address": t.stream.address}
              for s, t in zip(self.cfg.sends, e.senders if e else [])]
        clk = e.clock if e and e.clock else None
        return {
            "interface": e.ip if e else None,
            "ptp": {"state": clk.state if clk else "starting", "gm": clk.gm_id if clk else None,
                    "locked": bool(clk and clk.locked), "error": clk.error if clk else None},
            "sends": tx or [asdict(s) for s in self.cfg.sends], "receives": rx,
            "streams": [asdict(s) for s in (e.discovered() if e and e.browser else [])],
            "nmos": f"http://{e.ip}:{e.nmos.port}/x-nmos/" if e and e.nmos else None,
            "autostart": system.autostart_enabled(), "ptp_ports_ok": system.ptp_ports_allowed(),
            "config": asdict(self.cfg), "log": self.logs[-30:],
        }

    def devices(self):
        try:
            import sounddevice as sd
            return [{"index": i, "name": d["name"], "in": d["max_input_channels"], "out": d["max_output_channels"]}
                    for i, d in enumerate(sd.query_devices())]
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}

    def apply(self, body):
        if "config" in body:
            c = body["config"]
            self.cfg = Config(**{k: v for k, v in c.items() if k not in ("sends", "receives")},
                              sends=[SendCfg(**s) for s in c.get("sends", [])],
                              receives=[RecvCfg(**r) for r in c.get("receives", [])])
            self.cfg.save(self.cfg_path)
            self.restart()
        if "autostart" in body:
            system.set_autostart(bool(body["autostart"]))
        if "setup" in body:
            return {"ok": system.setup(self.log)}
        return {"ok": True}

    def _handler(self):
        app = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body: bytes, ctype):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    html = resources.files("aes67vsc").joinpath("ui.html").read_bytes()
                    return self._send(200, html, "text/html; charset=utf-8")
                if self.path == "/api/state":
                    return self._send(200, json.dumps(app.state()).encode(), "application/json")
                if self.path == "/api/devices":
                    return self._send(200, json.dumps(app.devices()).encode(), "application/json")
                self._send(404, b"not found", "text/plain")

            def do_POST(self):
                # Reject cross-site requests: only our own page may change settings.
                if self.headers.get("Origin") not in (None, app.url.rstrip("/")):
                    return self._send(403, b"forbidden", "text/plain")
                if self.path != "/api/apply":
                    return self._send(404, b"not found", "text/plain")
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    body = json.loads(self.rfile.read(n) or b"{}")
                    out = app.apply(body)
                except (ValueError, TypeError) as e:
                    return self._send(400, json.dumps({"error": str(e)}).encode(), "application/json")
                self._send(200, json.dumps(out).encode(), "application/json")

        return H


def run_app(background: bool = False, cfg_path=None, tray: bool = True):
    app = App(cfg_path)
    app.start()
    print(f"Control panel: {app.url}")
    if not background:
        webbrowser.open(app.url)
    if tray:
        try:
            _run_tray(app)
            return
        except Exception as e:  # noqa: BLE001 - no display / no tray support
            print(f"(no tray icon: {e})")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop()


def _icon():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2, 2, 62, 62), 14, fill=(32, 96, 200, 255))
    for i, h in enumerate((18, 34, 46, 30, 22)):
        x = 10 + i * 10
        d.rounded_rectangle((x, 32 - h // 2, x + 5, 32 + h // 2), 2, fill=(255, 255, 255, 255))
    return img


def _run_tray(app: App):
    import pystray

    def quit_(icon, _):
        icon.stop()
        app.stop()

    menu = pystray.Menu(
        pystray.MenuItem("Open control panel", lambda *_: webbrowser.open(app.url), default=True),
        pystray.MenuItem(lambda _: f"Clock: {app.engine.clock.state if app.engine and app.engine.clock else '...'}",
                         None, enabled=False),
        pystray.MenuItem("Start at login", lambda *_: system.set_autostart(not system.autostart_enabled()),
                         checked=lambda _: system.autostart_enabled()),
        pystray.MenuItem("Quit", quit_),
    )
    pystray.Icon("aes67-vsc", _icon(), "AES67 Virtual Soundcard", menu).run()
