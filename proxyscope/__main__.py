"""ProxyScope 入口：同时拉起 MITM 代理与本地 Web 检索界面。"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

from .certs import CertAuthority
from .proxy import ProxyServer
from .store import FlowStore
from .webui import WebUI

BANNER = r"""
 ____                       ____
|  _ \ _ __ _____  ___   _ / ___|  ___ ___  _ __   ___
| |_) | '__/ _ \ \/ / | | |\___ \ / __/ _ \| '_ \ / _ \
|  __/| | | (_) >  <| |_| | ___) | (_| (_) | |_) |  __/
|_|   |_|  \___/_/\_\\__, | |____/ \___\___/| .__/ \___|
                     |___/                 |_|
"""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="proxyscope",
        description="轻量 MITM 抓包代理 + 本地 Web 检索界面",
    )
    p.add_argument("--listen", default="127.0.0.1", help="代理监听地址（默认 127.0.0.1）")
    p.add_argument("--port", type=int, default=8080, help="代理端口（默认 8080）")
    p.add_argument("--web-host", default="127.0.0.1", help="Web 界面地址（默认 127.0.0.1）")
    p.add_argument("--web-port", type=int, default=8787, help="Web 界面端口（默认 8787）")
    p.add_argument("--data-dir", default=str(Path.cwd() / "data"),
                   help="数据目录（证书与数据库）")
    p.add_argument("--verify-upstream", action="store_true",
                   help="校验上游证书（默认关闭，便于抓自签名站点）")
    p.add_argument("--max-flows", type=int, default=200_000,
                   help="最多保留条数，超出自动清理最旧记录")
    p.add_argument("--timeout", type=float, default=60.0, help="上游超时秒数")
    p.add_argument("--debug", action="store_true", help="输出调试日志")
    p.add_argument("--no-web", action="store_true", help="只跑代理，不启 Web 界面")
    return p


async def run(args) -> int:
    data_dir = Path(args.data_dir).expanduser().resolve()
    data_dir.mkdir(parents=True, exist_ok=True)

    print(BANNER)
    ca = CertAuthority(data_dir / "certs")
    store = FlowStore(data_dir / "flows.db", max_flows=args.max_flows)

    proxy = ProxyServer(
        ca, store,
        host=args.listen, port=args.port,
        upstream_timeout=args.timeout,
        verify_upstream=args.verify_upstream,
        debug=args.debug,
    )
    await proxy.start()

    web = None
    if not args.no_web:
        web = WebUI(
            store, host=args.web_host, port=args.web_port,
            ca_pem=ca.ca_pem, proxy_stats=lambda: proxy.stats,
            verbose=args.debug,
        )
        web.start()

    print(f"  代理监听   : {args.listen}:{args.port}  (HTTP/HTTPS 通用)")
    if web:
        print(f"  Web 界面   : http://{args.web_host}:{args.web_port}")
        print(f"  CA 证书    : http://{args.web_host}:{args.web_port}/api/ca.crt")
    print(f"  CA 文件    : {ca.ca_cert}")
    print(f"  数据目录   : {data_dir}")
    print("\n  按 Ctrl+C 停止。\n")

    stop_event = asyncio.Event()

    def _stop(*_):
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _stop)
        except (NotImplementedError, RuntimeError, ValueError):
            signal.signal(sig, _stop)

    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass

    print("\n正在停止……")
    await proxy.stop()
    if web:
        web.stop()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
