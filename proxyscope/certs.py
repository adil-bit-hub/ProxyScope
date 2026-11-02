"""CA 证书管理与按域名动态签发证书（HTTPS 中间人解密）。

依赖 openssl 命令行，避免引入 cryptography 编译依赖。
生成的证书被缓存在内存中，同一域名复用。
"""
from __future__ import annotations

import ipaddress
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

_OPENSSL_CANDIDATES = [
    r"C:\Program Files\Git\usr\bin\openssl.exe",
    r"C:\Program Files\Git\mingw64\bin\openssl.exe",
    r"C:\Program Files\OpenSSL-Win64\bin\openssl.exe",
    r"C:\Program Files\OpenSSL\bin\openssl.exe",
    r"C:\Program Files (x86)\OpenSSL-Win32\bin\openssl.exe",
]

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def find_openssl() -> str:
    env = os.environ.get("PROXYSCOPE_OPENSSL")
    if env and Path(env).is_file():
        return env
    found = shutil.which("openssl")
    if found:
        return found
    for cand in _OPENSSL_CANDIDATES:
        if Path(cand).is_file():
            return cand
    raise RuntimeError(
        "未找到 openssl，HTTPS 解密无法工作。\n"
        "  Linux : sudo apt install openssl   /   sudo yum install openssl\n"
        "  macOS : brew install openssl\n"
        "  Windows: 安装 Git for Windows\n"
        "也可用环境变量 PROXYSCOPE_OPENSSL 指定 openssl 可执行文件路径。"
    )


class CertAuthority:
    """自签 CA + 按域名签发叶子证书。"""

    def __init__(self, cert_dir: Path, common_name: str = "ProxyScope Root CA"):
        self.cert_dir = Path(cert_dir)
        self.cert_dir.mkdir(parents=True, exist_ok=True)
        self.ca_cert = self.cert_dir / "proxyscope-ca.crt"
        self.ca_key = self.cert_dir / "proxyscope-ca.key"
        self.leaf_dir = self.cert_dir / "leaf"
        self.leaf_dir.mkdir(parents=True, exist_ok=True)
        self.common_name = common_name
        self.openssl = find_openssl()
        self._cache: dict[str, tuple[str, str]] = {}
        self._lock = threading.Lock()
        self._ensure_ca()

    # ---------------- CA ----------------
    def _ensure_ca(self) -> None:
        if self.ca_cert.is_file() and self.ca_key.is_file():
            return
        ext = self.cert_dir / "_ca_ext.cnf"
        ext.write_text(
            "basicConstraints=critical,CA:TRUE\n"
            "keyUsage=critical,digitalSignature,keyCertSign,cRLSign\n"
            "subjectKeyIdentifier=hash\n",
            encoding="utf-8",
        )
        cmd = [
            self.openssl, "req", "-x509", "-newkey", "rsa:2048", "-nodes",
            "-keyout", str(self.ca_key), "-out", str(self.ca_cert),
            "-days", "3650", "-sha256",
            "-subj", f"/CN={self.common_name}/O=ProxyScope",
            "-addext", "basicConstraints=critical,CA:TRUE",
            "-addext", "keyUsage=critical,digitalSignature,keyCertSign,cRLSign",
            "-addext", "subjectKeyIdentifier=hash",
        ]
        self._run(cmd)
        ext.unlink(missing_ok=True)

    # ---------------- 叶子证书 ----------------
    def get_leaf(self, host: str) -> tuple[str, str]:
        """返回 (cert_path, key_path)。host 为域名或 IP。"""
        host = (host or "").strip().strip("[]").lower().split(":")[0]
        if not host:
            host = "localhost"
        with self._lock:
            hit = self._cache.get(host)
            if hit and Path(hit[0]).is_file():
                return hit
            cert, key = self._issue(host)
            self._cache[host] = (cert, key)
            return cert, key

    def _issue(self, host: str) -> tuple[str, str]:
        safe = _SAFE_NAME.sub("_", host)[:80] or "host"
        leaf_crt = self.leaf_dir / f"{safe}.crt"
        leaf_key = self.leaf_dir / f"{safe}.key"

        try:
            ip = ipaddress.ip_address(host)
            alt = f"IP:{ip}" if ip.version == 4 else f"IP:{ip}"
            cn = str(ip)
        except ValueError:
            cn = host
            alt = f"DNS:{host}"
            if not host.startswith("*."):
                alt += f",DNS:*.{host}" if host.count(".") == 1 else ""

        ext_file = self.leaf_dir / f"{safe}.__ext.cnf"
        ext_file.write_text(
            "basicConstraints=critical,CA:FALSE\n"
            "keyUsage=critical,digitalSignature,keyEncipherment\n"
            "extendedKeyUsage=serverAuth\n"
            f"subjectAltName={alt}\n"
            "subjectKeyIdentifier=hash\n"
            "authorityKeyIdentifier=keyid,issuer\n",
            encoding="utf-8",
        )
        csr = self.leaf_dir / f"{safe}.csr"
        try:
            self._run([
                self.openssl, "req", "-newkey", "rsa:2048", "-nodes",
                "-keyout", str(leaf_key), "-out", str(csr),
                "-subj", f"/CN={cn}/O=ProxyScope",
            ])
            self._run([
                self.openssl, "x509", "-req", "-in", str(csr),
                "-CA", str(self.ca_cert), "-CAkey", str(self.ca_key),
                "-set_serial", str(self._serial()),
                "-out", str(leaf_crt), "-days", "825", "-sha256",
                "-extfile", str(ext_file),
            ])
        finally:
            csr.unlink(missing_ok=True)
            ext_file.unlink(missing_ok=True)

        # 拼接 key + cert，方便 ssl.SSLContext.load_cert_chain 单文件加载
        bundle = self.leaf_dir / f"{safe}.pem"
        bundle.write_bytes(leaf_key.read_bytes() + b"\n" + leaf_crt.read_bytes())
        return str(bundle), str(leaf_key)

    @staticmethod
    def _serial() -> int:
        return int.from_bytes(os.urandom(16), "big") >> 1

    def _run(self, cmd: list[str]) -> None:
        env = os.environ.copy()
        env.setdefault("RANDFILE", str(self.cert_dir / ".rnd"))
        proc = subprocess.run(
            cmd, capture_output=True, text=False, env=env,
            cwd=str(self.cert_dir),
        )
        if proc.returncode != 0:
            err = (proc.stderr or b"").decode("utf-8", "replace")
            raise RuntimeError(f"openssl 执行失败: {' '.join(cmd[:3])}\n{err}")

    @property
    def ca_pem(self) -> bytes:
        return self.ca_cert.read_bytes()
