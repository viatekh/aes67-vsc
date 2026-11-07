import json
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from aes67vsc.engine import Config, Engine, RecvCfg, SendCfg
from aes67vsc.net import default_interface_ip


def _get(url):
    with urllib.request.urlopen(url, timeout=3) as r:
        body = r.read().decode()
        return json.loads(body) if r.headers["Content-Type"] == "application/json" else body


def _patch(url, obj):
    req = urllib.request.Request(url, json.dumps(obj).encode(), {"Content-Type": "application/json"}, method="PATCH")
    with urllib.request.urlopen(req, timeout=3) as r:
        return json.loads(r.read())


def test_nmos_registry_and_is05_patch():
    from zeroconf import ServiceInfo
    ip = default_interface_ip()
    posted = []

    class Reg(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0)
            posted.append((self.path, json.loads(self.rfile.read(n) or b"{}")))
            self.send_response(201 if self.path.endswith("/resource") else 200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    reg = ThreadingHTTPServer((ip, 0), Reg)
    threading.Thread(target=reg.serve_forever, daemon=True).start()

    cfg = Config(interface=ip, ptp_can_lead=False, sends=[SendCfg("nmos tone", tone=750)],
                 receives=[RecvCfg("", null=True, latency_ms=100)])
    eng = Engine(cfg, log=lambda *a: None)
    eng.start()
    info = ServiceInfo("_nmos-register._tcp.local.", "test reg._nmos-register._tcp.local.", port=reg.server_address[1],
                       addresses=[socket.inet_aton(ip)], properties={"api_proto": "http", "api_ver": "v1.3", "pri": "10"},
                       server="testreg.local.")
    eng.ravenna.zc.register_service(info)
    try:
        base = f"http://{ip}:{eng.nmos.port}/x-nmos"
        node = _get(f"{base}/node/v1.3/self")
        assert node["api"]["endpoints"][0]["port"] == eng.nmos.port
        senders = _get(f"{base}/node/v1.3/senders")
        receivers = _get(f"{base}/node/v1.3/receivers")
        assert len(senders) == 1 and len(receivers) == 1
        sdp_text = _get(senders[0]["manifest_href"])
        assert "a=rtpmap:96 L24/48000/2" in sdp_text

        rid = receivers[0]["id"]
        r = _patch(f"{base}/connection/v1.1/single/receivers/{rid}/staged",
                   {"sender_id": senders[0]["id"], "master_enable": True,
                    "transport_file": {"data": sdp_text, "type": "application/sdp"},
                    "activation": {"mode": "activate_immediate"}})
        assert r["activation"]["activation_time"]
        time.sleep(1.5)
        rx, _, st = eng.receivers[0]
        assert st.name == "nmos tone" and rx.packets > 500
        assert _get(f"{base}/node/v1.3/receivers")[0]["subscription"]["active"] is True

        for _ in range(40):  # registration via mDNS-discovered registry
            types = {b["type"] for p, b in posted if p.endswith("/resource")}
            if {"node", "device", "source", "flow", "sender", "receiver"} <= types:
                break
            time.sleep(0.25)
        assert {"node", "device", "source", "flow", "sender", "receiver"} <= types

        _patch(f"{base}/connection/v1.1/single/receivers/{rid}/staged",
               {"master_enable": False, "activation": {"mode": "activate_immediate"}})
        time.sleep(1.2)
        assert 0 not in eng.receivers
    finally:
        eng.ravenna.zc.unregister_service(info)
        eng.stop()
        reg.shutdown()
