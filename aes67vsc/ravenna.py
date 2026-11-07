"""RAVENNA discovery: mDNS (_ravenna_session._sub._rtsp._tcp) + RTSP DESCRIBE for the SDP.

Used by Merging, Lawo, DirectOut, Neumann, ALC NetworX and other RAVENNA/AES67
gear that doesn't do SAP. We both advertise our streams and discover theirs.
"""
from __future__ import annotations

import socket
import socketserver
import threading
import time
import urllib.parse

from . import sdp

SESSION_TYPE = "_ravenna_session._sub._rtsp._tcp.local."
RTSP_TYPE = "_rtsp._tcp.local."


# ---------------------------------------------------------------- RTSP server
class _Handler(socketserver.StreamRequestHandler):
    def handle(self):
        while True:
            req = []
            while True:
                line = self.rfile.readline(4096)
                if not line:
                    return
                line = line.decode("latin-1").rstrip("\r\n")
                if not line:
                    break
                req.append(line)
            if not req:
                continue
            parts = req[0].split()
            if len(parts) < 3:
                return
            method, url = parts[0], parts[1]
            headers = {k.strip().lower(): v.strip() for k, _, v in (h.partition(":") for h in req[1:])}
            if int(headers.get("content-length", 0) or 0):
                self.rfile.read(int(headers["content-length"]))
            cseq = headers.get("cseq", "0")
            if method == "OPTIONS":
                self._reply(cseq, "200 OK", {"Public": "OPTIONS, DESCRIBE"})
            elif method == "DESCRIBE":
                text = self.server.lookup(urllib.parse.urlparse(url).path)
                if text is None:
                    self._reply(cseq, "404 Not Found")
                else:
                    self._reply(cseq, "200 OK", {"Content-Type": "application/sdp", "Content-Base": url}, text)
            else:
                self._reply(cseq, "501 Not Implemented")

    def _reply(self, cseq, status, headers=None, body=""):
        b = body.encode()
        h = {"CSeq": cseq, **(headers or {}), "Content-Length": str(len(b))}
        self.wfile.write((f"RTSP/1.0 {status}\r\n" + "".join(f"{k}: {v}\r\n" for k, v in h.items())
                          + "\r\n").encode() + b)


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr, streams):
        super().__init__(addr, _Handler)
        self.streams = streams

    def lookup(self, path: str):
        path = urllib.parse.unquote(path)
        for s in list(self.streams):
            if path in (f"/by-name/{s.name}", f"/by-id/{s.session_id}"):
                return sdp.build(s)
        return None


def describe(host: str, port: int, path: str, timeout: float = 2.0) -> str | None:
    """RTSP DESCRIBE -> SDP text."""
    url = f"rtsp://{host}:{port}{path}"
    try:
        with socket.create_connection((host, port), timeout) as c:
            c.sendall(f"DESCRIBE {url} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n".encode())
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = c.recv(4096)
                if not chunk:
                    return None
                data += chunk
            head, _, body = data.partition(b"\r\n\r\n")
            lines = head.decode("latin-1").split("\r\n")
            if " 200 " not in lines[0] + " ":
                return None
            length = 0
            for ln in lines[1:]:
                k, _, v = ln.partition(":")
                if k.strip().lower() == "content-length":
                    length = int(v.strip())
            while len(body) < length:
                chunk = c.recv(4096)
                if not chunk:
                    break
                body += chunk
            return body[:length].decode("utf-8", "replace")
    except OSError:
        return None


# ---------------------------------------------------------------- node
class Ravenna:
    """Advertises our streams and discovers others' via mDNS + RTSP."""

    def __init__(self, iface_ip: str, rtsp_port: int = 0, zc=None):
        from zeroconf import IPVersion, ServiceBrowser, Zeroconf
        self.ip = iface_ip
        self._own_zc = zc is None
        self.zc = zc or Zeroconf(interfaces=[iface_ip], ip_version=IPVersion.V4Only)
        self._our: list[sdp.Stream] = []
        self._infos = {}
        self.server = _Server((iface_ip, rtsp_port), self._our)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True, name="rtsp").start()
        self.streams: dict[str, tuple[float, sdp.Stream]] = {}
        self._lock = threading.Lock()
        self.browser = ServiceBrowser(self.zc, SESSION_TYPE, handlers=[self._on_change])

    # -- advertise
    def advertise(self, stream: sdp.Stream):
        from zeroconf import ServiceInfo
        self._our.append(stream)
        host = socket.gethostname().split(".")[0] or "aes67-vsc"
        info = ServiceInfo(SESSION_TYPE, f"{stream.name}.{RTSP_TYPE}", port=self.port,
                           addresses=[socket.inet_aton(self.ip)], server=f"{host}.local.")
        self._infos[stream.name] = info
        threading.Thread(target=self.zc.register_service, args=(info,),
                         kwargs={"allow_name_change": True}, daemon=True).start()

    # -- discover
    def _on_change(self, zeroconf, service_type, name, state_change):
        from zeroconf import ServiceStateChange
        instance = name[: -len(RTSP_TYPE) - 1] if name.endswith(RTSP_TYPE) else name.split(".")[0]
        if state_change is ServiceStateChange.Removed:
            with self._lock:
                self.streams.pop(name, None)
            return
        if instance in {s.name for s in self._our}:
            return
        threading.Thread(target=self._resolve, args=(name, instance), daemon=True).start()

    def _resolve(self, name, instance):
        info = self.zc.get_service_info(SESSION_TYPE, name, timeout=3000)
        if not info or not info.parsed_addresses():
            return
        host = info.parsed_addresses()[0]
        text = describe(host, info.port, "/by-name/" + urllib.parse.quote(instance))
        s = sdp.parse(text) if text else None
        if s:
            with self._lock:
                self.streams[name] = (time.monotonic(), s)

    def list(self) -> list[sdp.Stream]:
        with self._lock:
            return [s for _, s in self.streams.values()]

    def close(self):
        for info in self._infos.values():
            try:
                self.zc.unregister_service(info)
            except Exception:  # noqa: BLE001
                pass
        if self._own_zc:
            self.zc.close()
        self.server.shutdown()
