"""Web 服务：本地 HTTP 界面 + REST API，用于检索已抓取的流量。

零第三方依赖，基于 http.server.ThreadingHTTPServer。
"""
from __future__ import annotations

import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .store import FlowStore

STATIC_DIR = Path(__file__).parent / "static"


class ApiHandler(BaseHTTPRequestHandler):
    server_version = "ProxyScope/1.0"
    protocol_version = "HTTP/1.1"

    store: FlowStore
    proxy_stats = None

    # ---------------- 基础 ----------------
    def log_message(self, fmt, *args):  # 静音
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, ctype: str,
              extra: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode(),
                   "application/json; charset=utf-8")

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"error": msg}, code)

    # ---------------- 路由 ----------------
    def do_GET(self):
        url = urlsplit(self.path)
        qs = parse_qs(url.query, keep_blank_values=True)
        path = url.path

        if path in ("/", "/index.html"):
            return self._static("index.html", "text/html; charset=utf-8")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/flows":
            return self._api_list(qs)
        if path.startswith("/api/flows/"):
            return self._api_detail(path)
        if path == "/api/stats":
            return self._json({
                "store": self.store.stats(),
                "proxy": (self.proxy_stats() if self.proxy_stats else {}),
            })
        if path == "/api/ca.crt":
            ca = getattr(self.server, "ca_pem", None)
            if not ca:
                return self._err("CA 未启用", 404)
            return self._send(200, ca, "application/x-x509-ca-cert", {
                "Content-Disposition": 'attachment; filename="proxyscope-ca.crt"'
            })
        if path == "/api/health":
            return self._json({"ok": True, "ts": time.time()})
        return self._err("not found", 404)

    def do_POST(self):
        url = urlsplit(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            payload = {}

        if url.path == "/api/flows/delete":
            ids = payload.get("ids") or []
            return self._json({"deleted": self.store.delete(ids)})
        if url.path == "/api/flows/clear":
            self.store.clear()
            return self._json({"ok": True})
        return self._err("not found", 404)

    def do_HEAD(self):
        self.do_GET()

    # ---------------- 具体实现 ----------------
    def _static(self, rel: str, ctype: str | None = None) -> None:
        target = (STATIC_DIR / rel).resolve()
        if not str(target).startswith(str(STATIC_DIR.resolve())) or not target.is_file():
            return self._err("not found", 404)
        if ctype is None:
            ctype = {
                ".html": "text/html; charset=utf-8",
                ".js": "application/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8",
                ".svg": "image/svg+xml",
                ".png": "image/png",
                ".ico": "image/x-icon",
            }.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), ctype)

    def _api_list(self, qs: dict[str, list[str]]) -> None:
        def g(name: str, default: str = "") -> str:
            return (qs.get(name) or [default])[0]

        def f(name: str) -> float | None:
            raw = g(name)
            if not raw:
                return None
            try:
                return float(raw)
            except ValueError:
                return None

        try:
            limit = int(g("limit", "100") or 100)
            offset = int(g("offset", "0") or 0)
        except ValueError:
            return self._err("limit/offset 需要整数")
        data = self.store.list(
            q=g("q"), host=g("host"), method=g("method"), status=g("status"),
            scheme=g("scheme"), mime=g("mime"),
            since=f("since"), until=f("until"),
            limit=limit, offset=offset, order=g("order", "id_desc"),
        )
        return self._json(data)

    def _api_detail(self, path: str) -> None:
        tail = path[len("/api/flows/"):].strip("/")
        if not tail.isdigit():
            return self._err("无效 id", 400)
        flow = self.store.get(int(tail))
        if flow is None:
            return self._err("记录不存在", 404)
        return self._json(flow)


class WebUI:
    def __init__(self, store: FlowStore, host: str = "127.0.0.1",
                 port: int = 8787, ca_pem: bytes | None = None,
                 proxy_stats=None, verbose: bool = False):
        self.store = store
        self.host = host
        self.port = port
        self.ca_pem = ca_pem
        self.proxy_stats = proxy_stats
        self.verbose = verbose
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        handler = type("Handler", (ApiHandler,), {
            "store": self.store,
            "proxy_stats": staticmethod(self.proxy_stats) if self.proxy_stats else None,
        })
        self._httpd = ThreadingHTTPServer((self.host, self.port), handler)
        self._httpd.daemon_threads = True
        self._httpd.verbose = self.verbose
        self._httpd.ca_pem = self.ca_pem
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="proxyscope-web", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
