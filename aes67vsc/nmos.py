"""NMOS IS-04 (discovery/registration, v1.3) + IS-05 (connection management, v1.1).

Makes the app visible to broadcast control systems and NMOS controllers
(e.g. Riedel, Lawo VSM, Grass Valley, Sony, easy-nmos). Each send stream is
an NMOS sender; each receive slot is an NMOS receiver that a controller can
patch by PATCHing an SDP to it.

If an NMOS registry is advertised on mDNS (_nmos-register._tcp) we register
and heartbeat; otherwise we advertise peer-to-peer (_nmos-node._tcp).
"""
from __future__ import annotations

import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import sdp

NS = uuid.UUID("6d3c1a4e-9c1f-4f0b-8f5e-a1e67c5c0000")
IS04, IS05 = "v1.3", "v1.1"
MCAST = "urn:x-nmos:transport:rtp.mcast"


def _id(*parts) -> str:
    return str(uuid.uuid5(NS, "/".join(str(p) for p in parts)))


def _version() -> str:
    t = time.time_ns() + 37_000_000_000
    return f"{t // 1_000_000_000}:{t % 1_000_000_000}"


class Node:
    def __init__(self, engine, port: int = 0, zc=None):
        self.engine = engine
        self.ip = engine.ip
        self.host = socket.gethostname().split(".")[0] or "aes67-vsc"
        self.node_id = _id(self.host, "node")
        self.device_id = _id(self.host, "device")
        self.ver = _version()
        self._lock = threading.Lock()
        self.rx_state: dict[int, dict] = {}  # receiver slot -> IS-05 active params
        self.server = ThreadingHTTPServer((self.ip, port), self._handler())
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="nmos-http").start()
        self.registry: str | None = None
        self._registered = False
        self._stop = threading.Event()
        self.zc, self._own_zc = zc, False
        self._start_mdns()
        threading.Thread(target=self._registration_loop, daemon=True, name="nmos-reg").start()

    # ------------------------------------------------------------- resources
    def _base(self, rid, label, desc=""):
        return {"id": rid, "version": self.ver, "label": label, "description": desc, "tags": {}}

    def node(self):
        clk = self.engine.clock
        return {**self._base(self.node_id, self.host, "AES67 virtual soundcard"),
                "href": f"http://{self.ip}:{self.port}/", "hostname": self.host,
                "api": {"versions": [IS04], "endpoints": [{"host": self.ip, "port": self.port, "protocol": "http",
                                                          "authorization": False}]},
                "caps": {}, "services": [],
                "clocks": [{"name": "clk0", "ref_type": "ptp", "traceable": False, "version": "IEEE1588-2008",
                            "gmid": (clk.gm_id or "00-00-00-00-00-00-00-00").lower(),
                            "locked": bool(clk.locked)}],
                "interfaces": [{"name": "eth0", "chassis_id": None,
                                "port_id": "-".join(f"{b:02x}" for b in uuid.getnode().to_bytes(6, "big"))}]}

    def device(self):
        return {**self._base(self.device_id, f"{self.host} AES67"), "type": "urn:x-nmos:device:generic",
                "node_id": self.node_id,
                "senders": [self._ids("sender", i)[2] for i in range(len(self.engine.senders))],
                "receivers": [self._ids("receiver", i)[0] for i in range(len(self.engine.cfg.receives))],
                "controls": [{"href": f"http://{self.ip}:{self.port}/x-nmos/connection/{IS05}/",
                              "type": f"urn:x-nmos:control:sr-ctrl/{IS05}", "authorization": False}]}

    def _ids(self, kind, i):
        return _id(self.host, kind, i, "a"), _id(self.host, kind, i, "b"), _id(self.host, kind, i, "c")

    def sources_flows_senders(self):
        out = []
        for i, tx in enumerate(self.engine.senders):
            s = tx.stream
            src_id, flow_id, snd_id = self._ids("sender", i)
            src = {**self._base(src_id, s.name), "format": "urn:x-nmos:format:audio", "caps": {},
                   "device_id": self.device_id, "parents": [], "clock_name": "clk0",
                   "channels": [{"label": f"Ch {c + 1}"} for c in range(s.channels)]}
            flow = {**self._base(flow_id, s.name), "format": "urn:x-nmos:format:audio", "source_id": src_id,
                    "device_id": self.device_id, "parents": [], "sample_rate": {"numerator": s.rate},
                    "media_type": f"audio/{s.encoding}", "bit_depth": 24 if s.encoding == "L24" else 16}
            snd = {**self._base(snd_id, s.name), "flow_id": flow_id, "transport": MCAST,
                   "device_id": self.device_id, "interface_bindings": ["eth0"],
                   "manifest_href": f"http://{self.ip}:{self.port}/x-nmos/connection/{IS05}/single/senders/"
                                    f"{snd_id}/transportfile",
                   "subscription": {"receiver_id": None, "active": True}}
            out.append((src, flow, snd))
        return out

    def receivers(self):
        out = []
        for i, r in enumerate(self.engine.cfg.receives):
            rid = self._ids("receiver", i)[0]
            st = self.rx_state.get(i, {})
            out.append({**self._base(rid, r.stream or f"Input {i + 1}"), "format": "urn:x-nmos:format:audio",
                        "caps": {"media_types": ["audio/L24", "audio/L16"]}, "device_id": self.device_id,
                        "transport": MCAST, "interface_bindings": ["eth0"],
                        "subscription": {"sender_id": st.get("sender_id"), "active": bool(st.get("master_enable"))}})
        return out

    def all_resources(self):
        res = [("node", self.node()), ("device", self.device())]
        for src, flow, snd in self.sources_flows_senders():
            res += [("source", src), ("flow", flow), ("sender", snd)]
        res += [("receiver", r) for r in self.receivers()]
        return res

    # ------------------------------------------------------------- IS-05
    def _sender_by_id(self, sid):
        for i, tx in enumerate(self.engine.senders):
            if self._ids("sender", i)[2] == sid:
                return i, tx
        return None, None

    def _receiver_slot(self, rid):
        for i in range(len(self.engine.cfg.receives)):
            if self._ids("receiver", i)[0] == rid:
                return i
        return None

    def _sender_params(self, tx):
        s = tx.stream
        return {"sender_id": None, "receiver_id": None, "master_enable": True,
                "activation": {"mode": None, "requested_time": None, "activation_time": None},
                "transport_params": [{"source_ip": self.ip, "destination_ip": s.address, "source_port": s.port,
                                      "destination_port": s.port, "rtp_enabled": True}]}

    def _rx_params(self, i):
        d = {"sender_id": None, "master_enable": False,
             "activation": {"mode": None, "requested_time": None, "activation_time": None},
             "transport_file": {"data": None, "type": None},
             "transport_params": [{"source_ip": None, "multicast_ip": None, "interface_ip": self.ip,
                                   "destination_port": 5004, "rtp_enabled": True}]}
        d.update(self.rx_state.get(i, {}))
        return d

    def _patch_receiver(self, i, body):
        cur = self._rx_params(i)
        for k in ("sender_id", "master_enable", "transport_file"):
            if k in body:
                cur[k] = body[k]
        if "transport_params" in body and body["transport_params"]:
            cur["transport_params"] = [{**cur["transport_params"][0], **body["transport_params"][0]}]
        act = body.get("activation") or {}
        if act.get("mode"):  # all modes are applied immediately
            cur["activation"] = {"mode": act["mode"], "requested_time": act.get("requested_time"),
                                 "activation_time": _version()}
            self._apply_receiver(i, cur)
        with self._lock:
            self.rx_state[i] = cur
        self._bump()
        return cur

    def _apply_receiver(self, i, p):
        stream = None
        if p.get("master_enable"):
            tf = (p.get("transport_file") or {}).get("data")
            stream = sdp.parse(tf) if tf else None
            tp = p["transport_params"][0]
            if stream is None and tp.get("multicast_ip"):
                stream = sdp.Stream(f"Input {i + 1}", tp["multicast_ip"], int(tp.get("destination_port") or 5004))
            elif stream is not None and tp.get("multicast_ip"):
                stream.address = tp["multicast_ip"]
        self.engine.set_override(i, stream)

    # ------------------------------------------------------------- HTTP
    def _handler(self):
        node = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj=None, ctype="application/json"):
                body = (obj if isinstance(obj, str) else json.dumps(obj)).encode() if obj is not None else b""
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_OPTIONS(self):
                self.send_response(200)
                for k, v in (("Access-Control-Allow-Origin", "*"),
                             ("Access-Control-Allow-Methods", "GET, PUT, POST, PATCH, HEAD, OPTIONS, DELETE"),
                             ("Access-Control-Allow-Headers", "Content-Type, Accept"),
                             ("Content-Length", "0")):
                    self.send_header(k, v)
                self.end_headers()

            def do_GET(self):
                self._route("GET")

            def do_PATCH(self):
                self._route("PATCH")

            def _route(self, method):
                parts = [p for p in self.path.split("?")[0].split("/") if p]
                try:
                    r = node._dispatch(method, parts, self._body() if method == "PATCH" else None)
                except ValueError as e:
                    return self._send(400, {"code": 400, "error": str(e), "debug": None})
                if r is None:
                    return self._send(404, {"code": 404, "error": "Not found", "debug": None})
                code, obj, *ct = r
                self._send(code, obj, *(ct or ["application/json"]))

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    return json.loads(self.rfile.read(n) or b"{}")
                except json.JSONDecodeError as e:
                    raise ValueError(str(e))

        return H

    def _dispatch(self, method, p, body):
        if not p:
            return 200, ["x-nmos/"]
        if p[0] != "x-nmos":
            return None
        if len(p) == 1:
            return 200, ["node/", "connection/"]
        if p[1] == "node":
            if len(p) == 2:
                return 200, [f"{IS04}/"]
            if p[2] != IS04:
                return None
            if len(p) == 3:
                return 200, ["self/", "devices/", "sources/", "flows/", "senders/", "receivers/"]
            res = {"self": [self.node()], "devices": [self.device()]}
            sfs = self.sources_flows_senders()
            res["sources"] = [x[0] for x in sfs]
            res["flows"] = [x[1] for x in sfs]
            res["senders"] = [x[2] for x in sfs]
            res["receivers"] = self.receivers()
            if p[3] == "self":
                return 200, res["self"][0]
            if p[3] not in res:
                return None
            if len(p) == 4:
                return 200, res[p[3]]
            for r in res[p[3]]:
                if r["id"] == p[4]:
                    return 200, r
            return None
        if p[1] == "connection":
            if len(p) == 2:
                return 200, [f"{IS05}/"]
            if p[2] != IS05:
                return None
            if len(p) == 3:
                return 200, ["single/", "bulk/"]
            if p[3] != "single":
                return None
            if len(p) == 4:
                return 200, ["senders/", "receivers/"]
            kind = p[4]
            if len(p) == 5:
                if kind == "senders":
                    return 200, [self._ids("sender", i)[2] + "/" for i in range(len(self.engine.senders))]
                if kind == "receivers":
                    return 200, [self._ids("receiver", i)[0] + "/" for i in range(len(self.engine.cfg.receives))]
                return None
            rid, leaf = p[5], (p[6] if len(p) > 6 else None)
            if kind == "senders":
                i, tx = self._sender_by_id(rid)
                if tx is None:
                    return None
                if leaf is None:
                    return 200, ["constraints/", "staged/", "active/", "transportfile/", "transporttype/"]
                if leaf == "transporttype":
                    return 200, MCAST
                if leaf == "transportfile":
                    return 200, sdp.build(tx.stream), "application/sdp"
                if leaf == "constraints":
                    return 200, [{"source_ip": {"enum": [self.ip]}, "destination_ip": {}, "source_port": {},
                                  "destination_port": {}, "rtp_enabled": {}}]
                if leaf in ("staged", "active"):
                    if method == "PATCH":  # accept but keep our addressing (read-only for now)
                        params = self._sender_params(tx)
                        params["activation"] = {**params["activation"], **(body or {}).get("activation", {})}
                        return 200, params
                    return 200, self._sender_params(tx)
                return None
            if kind == "receivers":
                i = self._receiver_slot(rid)
                if i is None:
                    return None
                if leaf is None:
                    return 200, ["constraints/", "staged/", "active/", "transporttype/"]
                if leaf == "transporttype":
                    return 200, MCAST
                if leaf == "constraints":
                    return 200, [{"source_ip": {}, "multicast_ip": {}, "interface_ip": {"enum": [self.ip]},
                                  "destination_port": {}, "rtp_enabled": {}}]
                if leaf == "staged" and method == "PATCH":
                    return 200, self._patch_receiver(i, body or {})
                if leaf in ("staged", "active"):
                    return 200, self._rx_params(i)
        return None

    # ------------------------------------------------------------- mDNS + registration
    def _start_mdns(self):
        try:
            from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, Zeroconf
        except ImportError:
            return
        if self.zc is None:
            self.zc, self._own_zc = Zeroconf(interfaces=[self.ip], ip_version=IPVersion.V4Only), True
        self._registries: dict[str, tuple[int, str]] = {}
        self._node_info = ServiceInfo("_nmos-node._tcp.local.", f"{self.host} aes67-vsc._nmos-node._tcp.local.",
                                      port=self.port, addresses=[socket.inet_aton(self.ip)],
                                      properties={"api_proto": "http", "api_ver": IS04, "api_auth": "false",
                                                  "pri": "100"},
                                      server=f"{self.host}.local.")
        threading.Thread(target=self.zc.register_service, args=(self._node_info,),
                         kwargs={"allow_name_change": True}, daemon=True).start()
        for t in ("_nmos-register._tcp.local.", "_nmos-registration._tcp.local."):
            ServiceBrowser(self.zc, t, handlers=[self._on_registry])

    def _on_registry(self, zeroconf, service_type, name, state_change):
        from zeroconf import ServiceStateChange

        def resolve():
            info = zeroconf.get_service_info(service_type, name, timeout=3000)
            if not info or not info.parsed_addresses():
                return
            props = {k.decode(): (v or b"").decode() for k, v in info.properties.items()}
            if props.get("api_proto", "http") != "http" or IS04 not in props.get("api_ver", IS04):
                return
            url = f"http://{info.parsed_addresses()[0]}:{info.port}/x-nmos/registration/{IS04}"
            self._registries[name] = (int(props.get("pri", 100) or 100), url)

        if state_change is ServiceStateChange.Removed:
            self._registries.pop(name, None)
        else:
            threading.Thread(target=resolve, daemon=True).start()

    def _post(self, url, obj):
        req = urllib.request.Request(url, json.dumps(obj).encode(), {"Content-Type": "application/json"},
                                     method="POST")
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status

    def _registration_loop(self):
        while not self._stop.wait(5 if self._registered else 1):
            regs = sorted(getattr(self, "_registries", {}).values())
            url = regs[0][1] if regs else None
            if url != self.registry:
                self.registry, self._registered = url, False
            if not url:
                continue
            try:
                if not self._registered:
                    for kind, data in self.all_resources():
                        self._post(f"{url}/resource", {"type": kind, "data": data})
                    self._registered = True
                    self.engine.log(f"NMOS: registered with {url}")
                else:
                    self._post(f"{url}/health/nodes/{self.node_id}", {})
            except urllib.error.HTTPError as e:
                if e.code == 404:  # registry forgot us: re-register
                    self._registered = False
            except OSError:
                self._registered = False

    def _bump(self):
        """Resources changed: new version + re-register so the registry sees it."""
        self.ver = _version()
        self._registered = False

    def close(self):
        self._stop.set()
        try:
            if self.zc and getattr(self, "_node_info", None):
                self.zc.unregister_service(self._node_info)
            if self._own_zc:
                self.zc.close()
        except Exception:  # noqa: BLE001
            pass
        self.server.shutdown()
