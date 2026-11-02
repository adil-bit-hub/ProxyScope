"""代理核心：HTTP/1.1 正向代理 + CONNECT/TLS 中间人解密。

设计要点
- 纯 asyncio，单进程，无外部框架依赖；
- 支持 CONNECT 隧道解密、chunked、gzip/deflate/br/zstd（入库前解码）；
- WebSocket 升级后转为双向隧道，仅记录握手；
- 每条事务经 FlowStore 落库，供 Web 界面检索。
"""
from __future__ import annotations

import asyncio
import gzip
import io
import ssl
import time
import zlib
from typing import Any, Callable
from urllib.parse import urlsplit

from .certs import CertAuthority

try:  # 可选依赖，缺失时只是不解码对应编码
    import brotli  # type: ignore
except Exception:  # pragma: no cover
    brotli = None
try:
    import zstandard  # type: ignore
except Exception:  # pragma: no cover
    zstandard = None

MAX_HEAD = 128 * 1024
MAX_BODY = 64 * 1024 * 1024
HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade", "proxy-connection",
}


class Headers:
    """保留顺序与重复项的轻量头容器。"""

    __slots__ = ("items",)

    def __init__(self, items: list[tuple[str, str]] | None = None):
        self.items: list[tuple[str, str]] = list(items or [])

    def get(self, name: str, default: str | None = None) -> str | None:
        low = name.lower()
        for k, v in self.items:
            if k.lower() == low:
                return v
        return default

    def get_all(self, name: str) -> list[str]:
        low = name.lower()
        return [v for k, v in self.items if k.lower() == low]

    def set(self, name: str, value: str) -> None:
        low = name.lower()
        self.items = [(k, v) for k, v in self.items if k.lower() != low]
        self.items.append((name, value))

    def remove(self, name: str) -> None:
        low = name.lower()
        self.items = [(k, v) for k, v in self.items if k.lower() != low]

    def as_list(self) -> list[list[str]]:
        return [[k, v] for k, v in self.items]

    def connection_tokens(self) -> set[str]:
        toks: set[str] = set()
        for v in self.get_all("connection"):
            for p in v.split(","):
                p = p.strip().lower()
                if p:
                    toks.add(p)
        return toks


def _split_head(raw: bytes) -> tuple[str, Headers]:
    text = raw.decode("latin-1")
    norm = text.replace("\r\n", "\n")
    lines = norm.split("\n")
    start = lines[0]
    headers = Headers()
    for line in lines[1:]:
        if not line:
            continue
        if line[0] in " \t" and headers.items:  # obs-fold 折行
            k, v = headers.items[-1]
            headers.items[-1] = (k, v + " " + line.strip())
            continue
        if ":" not in line:
            continue
        k, v = line.split(":", 1)
        headers.items.append((k.strip(), v.strip()))
    return start, headers


def parse_request_head(raw: bytes) -> tuple[str, str, str, Headers]:
    start, headers = _split_head(raw)
    parts = start.split(" ")
    if len(parts) < 3:
        raise ValueError(f"非法请求行: {start!r}")
    return parts[0], parts[1], parts[2], headers


def parse_response_head(raw: bytes) -> tuple[str, int, str, Headers]:
    start, headers = _split_head(raw)
    parts = start.split(" ", 2)
    if len(parts) < 2:
        raise ValueError(f"非法状态行: {start!r}")
    try:
        status = int(parts[1])
    except ValueError:
        status = 0
    text = parts[2] if len(parts) > 2 else ""
    return parts[0], status, text, headers


def encode_head(first_line: str, headers: Headers) -> bytes:
    out = [first_line]
    out.extend(f"{k}: {v}" for k, v in headers.items)
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1", "replace")


def decompress(body: bytes, encoding: str | None) -> bytes:
    if not body or not encoding:
        return body
    enc = encoding.strip().lower()
    try:
        if enc in ("gzip", "x-gzip"):
            return gzip.decompress(body)
        if enc == "deflate":
            try:
                return zlib.decompress(body)
            except zlib.error:
                return zlib.decompress(body, -zlib.MAX_WBITS)
        if enc == "br" and brotli is not None:
            return brotli.decompress(body)
        if enc == "zstd" and zstandard is not None:
            return zstandard.ZstdDecompressor().decompress(
                body, max_output_size=MAX_BODY
            )
    except Exception:
        return body
    return body


def guess_mime(headers: Headers) -> str:
    ct = headers.get("content-type") or ""
    return ct.split(";")[0].strip().lower()


async def start_tls_server(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    ctx: ssl.SSLContext,
) -> tuple[asyncio.StreamReader | None, asyncio.StreamWriter | None]:
    """把已建立的明文连接就地升级为服务端 TLS（MITM 接管 CONNECT 隧道）。

    Python 3.11+ 有 StreamWriter.start_tls()，原地升级即可，返回 (None, None)。
    Python 3.10 及更早没有该 API：直接对底层 socket 完成 TLS 握手，
    再用同一 socket 重建一对待读写流，返回给调用方替换旧的 reader/writer。

    做法是让 asyncio 自己处理加解密：把 socket 交给
    loop.start_tls()，由事件循环接管，比手工驱动 SSLObject 稳定得多。
    """
    if hasattr(writer, "start_tls"):
        await writer.start_tls(ctx)          # 3.11+：原地升级
        return None, None

    # ---- Python <= 3.10 兼容路径 ----
    transport = writer.transport
    protocol = transport.get_protocol()
    raw_sock = transport.get_extra_info("socket")
    if raw_sock is None:
        raise RuntimeError("无法获取底层 socket，TLS 升级失败")

    loop = asyncio.get_running_loop()

    # 让事件循环把这个 transport 升级成 TLS，返回新的 transport；
    # 底层的 StreamReader/Protocol 会被复用，因此 reader 无需更换。
    new_transport = await loop.start_tls(
        transport, protocol, ctx, server_side=True,
    )
    if new_transport is None:
        raise RuntimeError("TLS 升级失败")

    # 绑定新的 transport 到原有 writer，后续读写都走加密通道
    writer._transport = new_transport            # type: ignore[attr-defined]
    return None, None


class ProxyServer:
    def __init__(
        self,
        ca: CertAuthority,
        store: Any,
        *,
        host: str = "127.0.0.1",
        port: int = 8080,
        upstream_timeout: float = 60.0,
        verify_upstream: bool = False,
        on_flow: Callable[[int, dict], None] | None = None,
        debug: bool = False,
    ):
        self.ca = ca
        self.store = store
        self.host = host
        self.port = port
        self.upstream_timeout = upstream_timeout
        self.on_flow = on_flow
        self.debug = debug
        self._tls_cache: dict[str, ssl.SSLContext] = {}
        self._server: asyncio.AbstractServer | None = None
        self._running = False
        self._active = 0
        self._total = 0

        self._upstream_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        self._upstream_ctx.check_hostname = verify_upstream
        self._upstream_ctx.verify_mode = (
            ssl.CERT_REQUIRED if verify_upstream else ssl.CERT_NONE
        )
        try:
            self._upstream_ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
        except ssl.SSLError:  # pragma: no cover
            pass

    # ---------------- 生命周期 ----------------
    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port, limit=MAX_HEAD,
            reuse_address=True, backlog=2048,
        )
        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:
                pass
            self._server = None

    @property
    def stats(self) -> dict[str, int]:
        return {"active": self._active, "total": self._total}

    # ---------------- 连接处理 ----------------
    async def _handle_client(self, reader: asyncio.StreamReader,
                             writer: asyncio.StreamWriter) -> None:
        self._active += 1
        peer = writer.get_extra_info("peername") or ("", 0)
        client_addr = f"{peer[0]}:{peer[1]}" if peer and peer[0] else ""
        try:
            await self._session(reader, writer, client_addr)
        except (asyncio.IncompleteReadError, ConnectionResetError,
                BrokenPipeError, ssl.SSLError, TimeoutError, asyncio.CancelledError):
            pass
        except Exception as exc:  # pragma: no cover
            if self.debug:
                print(f"[proxy] session error: {exc!r}")
        finally:
            self._active -= 1
            try:
                writer.close()
            except Exception:
                pass

    async def _session(self, reader: asyncio.StreamReader,
                       writer: asyncio.StreamWriter, client_addr: str) -> None:
        tls_host: str | None = None
        tls_on = False

        while self._running:
            try:
                raw = await asyncio.wait_for(
                    reader.readuntil(b"\r\n\r\n"), timeout=self.upstream_timeout
                )
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                    TimeoutError):
                return
            if not raw.strip():
                return
            try:
                method, target, version, headers = parse_request_head(raw)
            except ValueError:
                return

            if method.upper() == "CONNECT":
                host, _, port_s = target.partition(":")
                host = host.strip()
                port = int(port_s) if port_s.strip().isdigit() else 443
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                await writer.drain()
                try:
                    new_reader, new_writer = await start_tls_server(
                        reader, writer, self._tls_for(host)
                    )
                except Exception as exc:
                    if self.debug:
                        print(f"[proxy] tls handshake failed for {host}: {exc!r}")
                    return
                # Python < 3.11 时对象被替换，必须让后续循环用新的流
                if new_reader is not None:
                    reader, writer = new_reader, new_writer
                tls_on = True
                tls_host = host
                continue

            scheme = "https" if tls_on else "http"
            keep = await self._forward(
                reader, writer, method, target, version, headers,
                scheme, client_addr, tls_host,
            )
            if not keep:
                return

    @staticmethod
    def _client_wants_keepalive(headers: Headers, version: str) -> bool:
        conn = (headers.get("connection") or "").lower()
        if "close" in conn:
            return False
        if version.upper() == "HTTP/1.0":
            return "keep-alive" in conn
        return True

    def _tls_for(self, host: str) -> ssl.SSLContext:
        ctx = self._tls_cache.get(host)
        if ctx is None:
            bundle, _ = self.ca.get_leaf(host)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(bundle)
            ctx.set_alpn_protocols(["http/1.1"])
            self._tls_cache[host] = ctx
        return ctx

    # ---------------- 转发 ----------------
    async def _forward(self, reader: asyncio.StreamReader,
                       client_writer: asyncio.StreamWriter,
                       method: str, target: str, version: str,
                       headers: Headers, scheme: str, client_addr: str,
                       tunnel_host: str | None) -> bool:
        """返回 True 表示客户端连接可继续复用。"""
        started = time.time()
        parsed = urlsplit(target) if "://" in target else None
        if parsed and parsed.scheme in ("http", "https"):
            scheme = parsed.scheme
            host = parsed.hostname or ""
            port = parsed.port or (443 if scheme == "https" else 80)
            path = parsed.path or "/"
            query = parsed.query
        else:
            host_hdr = headers.get("host") or tunnel_host or ""
            if host_hdr.startswith("["):  # IPv6 字面量
                close = host_hdr.find("]")
                host = host_hdr[1:close] if close > 0 else host_hdr
                rest = host_hdr[close + 1:] if close > 0 else ""
                port_s = rest[1:] if rest.startswith(":") else ""
            else:
                host, _, port_s = host_hdr.partition(":")
            host = host or tunnel_host or ""
            port = int(port_s) if port_s.isdigit() else (
                443 if scheme == "https" else 80)
            path, _, query = target.partition("?")
            if not path:
                path = "/"

        try:
            req_body = await self._read_body(reader, headers)
        except Exception:
            req_body = b""

        out_headers = Headers([
            (k, v) for k, v in headers.items
            if k.lower() not in HOP_BY_HOP
            and k.lower() not in headers.connection_tokens()
        ])
        out_headers.set(
            "Host", f"{host}:{port}" if port not in (80, 443) else host
        )
        if req_body:
            out_headers.set("Content-Length", str(len(req_body)))
        elif method.upper() in ("POST", "PUT", "PATCH"):
            out_headers.remove("content-length")
            out_headers.set("Content-Length", "0")
        out_headers.set("Connection", "close")

        target_path = path + ("?" + query if query else "")
        payload = encode_head(f"{method} {target_path} {version}", out_headers)
        if req_body:
            payload += req_body

        resp_headers = Headers()
        resp_body = b""
        status = 0
        status_text = ""
        error = ""
        keep = self._client_wants_keepalive(headers, version)

        try:
            use_tls = scheme == "https"
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host, port,
                    ssl=self._upstream_ctx if use_tls else None,
                    server_hostname=host if use_tls else None,
                    limit=MAX_HEAD,
                ),
                timeout=self.upstream_timeout,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return await self._reply_error(
                client_writer, started, scheme, method, host, port, path,
                query, version, headers, req_body, client_addr, error,
            )

        try:
            up_writer.write(payload)
            await up_writer.drain()
            head = await asyncio.wait_for(
                up_reader.readuntil(b"\r\n\r\n"), timeout=self.upstream_timeout
            )
            _, status, status_text, resp_headers = parse_response_head(head)

            if (resp_headers.get("upgrade") or "").lower() == "websocket":
                client_writer.write(head)
                await client_writer.drain()
                await self._tunnel(reader, client_writer, up_reader, up_writer)
                self._emit(dict(
                    ts=started, scheme=scheme, method=method, host=host,
                    port=port, path=path, query=query, http_version=version,
                    status=status, status_text=status_text, mime="websocket",
                    req_headers=headers.as_list(), req_body=req_body,
                    resp_headers=resp_headers.as_list(),
                    resp_body="[WebSocket 隧道，未记录帧内容]".encode(),
                    duration_ms=(time.time() - started) * 1000,
                    tls=scheme == "https", client_addr=client_addr, error="",
                ))
                return False

            resp_body = await self._read_body(up_reader, resp_headers)
            no_body = (method.upper() == "HEAD" or status in (204, 304)
                       or status < 200)

            reply_headers = Headers([
                (k, v) for k, v in resp_headers.items
                if k.lower() not in HOP_BY_HOP
                and k.lower() not in resp_headers.connection_tokens()
            ])
            if no_body:
                keep = False  # 帧边界不明确，直接断开最安全
            else:
                reply_headers.set("Content-Length", str(len(resp_body)))
            reply_headers.set("Connection", "keep-alive" if keep else "close")
            client_writer.write(
                encode_head(f"HTTP/1.1 {status} {status_text}", reply_headers)
                + (b"" if no_body else resp_body)
            )
            await client_writer.drain()
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            keep = False
            if self.debug:
                print(f"[proxy] upstream {host} error: {error}")
        finally:
            try:
                up_writer.close()
            except Exception:
                pass

        decoded = decompress(resp_body, resp_headers.get("content-encoding"))
        self._emit(dict(
            ts=started, scheme=scheme, method=method, host=host, port=port,
            path=path, query=query, http_version=version,
            status=status or None, status_text=status_text,
            mime=guess_mime(resp_headers),
            req_headers=headers.as_list(), req_body=req_body,
            resp_headers=resp_headers.as_list(), resp_body=decoded,
            duration_ms=(time.time() - started) * 1000,
            tls=scheme == "https", client_addr=client_addr, error=error,
        ))
        return keep

    async def _reply_error(self, writer, started, scheme, method, host, port,
                           path, query, version, headers, req_body,
                           client_addr, error) -> bool:
        body = f"ProxyScope 无法连接 {host}:{port}\n{error}\n".encode()
        try:
            writer.write(
                b"HTTP/1.1 502 Bad Gateway\r\n"
                b"Content-Type: text/plain; charset=utf-8\r\n"
                + f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except Exception:
            pass
        self._emit(dict(
            ts=started, scheme=scheme, method=method, host=host, port=port,
            path=path, query=query, http_version=version,
            status=502, status_text="Bad Gateway", mime="text/plain",
            req_headers=headers.as_list(), req_body=req_body,
            resp_headers=[], resp_body=body,
            duration_ms=(time.time() - started) * 1000,
            tls=scheme == "https", client_addr=client_addr, error=error,
        ))
        return False

    async def _tunnel(self, c_reader, c_writer, u_reader, u_writer) -> None:
        async def pump(src, dst):
            try:
                while True:
                    data = await src.read(65536)
                    if not data:
                        break
                    dst.write(data)
                    await dst.drain()
            except Exception:
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass

        await asyncio.gather(
            pump(c_reader, u_writer), pump(u_reader, c_writer),
            return_exceptions=True,
        )

    async def _read_body(self, reader: asyncio.StreamReader,
                         headers: Headers) -> bytes:
        te = (headers.get("transfer-encoding") or "").lower()
        if "chunked" in te:
            return await self._read_chunked(reader)
        cl = (headers.get("content-length") or "").strip()
        if cl.isdigit():
            n = int(cl)
            if n <= 0:
                return b""
            return await reader.readexactly(min(n, MAX_BODY))
        return b""

    async def _read_chunked(self, reader: asyncio.StreamReader) -> bytes:
        buf = io.BytesIO()
        total = 0
        while True:
            line = await reader.readuntil(b"\r\n")
            size_s = line.strip().split(b";")[0]
            try:
                size = int(size_s, 16)
            except ValueError:
                break
            if size == 0:
                while True:
                    t = await reader.readuntil(b"\r\n")
                    if t in (b"\r\n", b""):
                        break
                break
            data = await reader.readexactly(size)
            total += size
            if total <= MAX_BODY:
                buf.write(data)
            await reader.readexactly(2)
        return buf.getvalue()

    def _emit(self, flow: dict[str, Any]) -> None:
        self._total += 1
        try:
            fid = self.store.add(flow)
        except Exception as exc:  # pragma: no cover
            if self.debug:
                print(f"[store] add failed: {exc!r}")
            return
        if self.on_flow is not None:
            try:
                self.on_flow(fid, flow)
            except Exception:
                pass
