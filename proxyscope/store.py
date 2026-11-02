"""流量存储层：SQLite 持久化 + 全文/条件检索。

只存元数据 + 正文（可截断），不依赖外部服务。
"""
from __future__ import annotations

import base64
import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS flows (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL,
    scheme        TEXT    NOT NULL,
    method        TEXT    NOT NULL,
    host          TEXT    NOT NULL,
    port          INTEGER NOT NULL,
    path          TEXT    NOT NULL,
    query         TEXT    NOT NULL DEFAULT '',
    http_version  TEXT    NOT NULL DEFAULT 'HTTP/1.1',
    status        INTEGER,
    status_text   TEXT    NOT NULL DEFAULT '',
    mime          TEXT    NOT NULL DEFAULT '',
    req_headers   TEXT    NOT NULL DEFAULT '[]',
    req_body      BLOB,
    req_size      INTEGER NOT NULL DEFAULT 0,
    req_truncated INTEGER NOT NULL DEFAULT 0,
    resp_headers  TEXT    NOT NULL DEFAULT '[]',
    resp_body     BLOB,
    resp_size     INTEGER NOT NULL DEFAULT 0,
    resp_truncated INTEGER NOT NULL DEFAULT 0,
    duration_ms   REAL    NOT NULL DEFAULT 0,
    tls           INTEGER NOT NULL DEFAULT 0,
    client_addr   TEXT    NOT NULL DEFAULT '',
    error         TEXT    NOT NULL DEFAULT '',
    note          TEXT    NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_flows_ts     ON flows(ts DESC);
CREATE INDEX IF NOT EXISTS idx_flows_host   ON flows(host);
CREATE INDEX IF NOT EXISTS idx_flows_status ON flows(status);
CREATE INDEX IF NOT EXISTS idx_flows_method ON flows(method);
"""

FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS flows_fts USING fts5(
    host, path, req_text, resp_text, tokenize='unicode61'
);
"""

# 关键字检索时可搜的正文上限，避免一次扫太多
TEXT_PREVIEW_LIMIT = 200_000


def _to_text(blob: bytes | None) -> str:
    if not blob:
        return ""
    return blob.decode("utf-8", "replace")


class FlowStore:
    def __init__(self, db_path: Path, max_body: int = 2 * 1024 * 1024,
                 max_flows: int = 200_000):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.max_body = max_body
        self.max_flows = max_flows
        self._lock = threading.RLock()
        self._local = threading.local()
        self._fts_ok = False
        with self._lock:
            conn = self._conn()
            conn.executescript(SCHEMA)
            try:
                conn.executescript(FTS_SCHEMA)
                self._fts_ok = True
            except sqlite3.DatabaseError:
                self._fts_ok = False
            conn.commit()

    # ---------------- 连接 ----------------
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.db_path), timeout=30,
                                   check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            self._local.conn = conn
        return conn

    # ---------------- 写入 ----------------
    def add(self, flow: dict[str, Any]) -> int:
        req_body: bytes | None = flow.get("req_body")
        resp_body: bytes | None = flow.get("resp_body")
        req_trunc = 0
        resp_trunc = 0
        if req_body is not None and len(req_body) > self.max_body:
            req_body, req_trunc = req_body[: self.max_body], 1
        if resp_body is not None and len(resp_body) > self.max_body:
            resp_body, resp_trunc = resp_body[: self.max_body], 1

        row = (
            flow.get("ts", time.time()),
            flow.get("scheme", "http"),
            flow.get("method", "GET"),
            flow.get("host", ""),
            int(flow.get("port", 80)),
            flow.get("path", "/"),
            flow.get("query", ""),
            flow.get("http_version", "HTTP/1.1"),
            flow.get("status"),
            flow.get("status_text", ""),
            flow.get("mime", ""),
            json.dumps(flow.get("req_headers", []), ensure_ascii=False),
            req_body,
            len(flow.get("req_body") or b""),
            req_trunc,
            json.dumps(flow.get("resp_headers", []), ensure_ascii=False),
            resp_body,
            len(flow.get("resp_body") or b""),
            resp_trunc,
            float(flow.get("duration_ms", 0)),
            1 if flow.get("tls") else 0,
            flow.get("client_addr", ""),
            flow.get("error", ""),
            flow.get("note", ""),
        )
        with self._lock:
            conn = self._conn()
            cur = conn.execute(
                """INSERT INTO flows (
                    ts,scheme,method,host,port,path,query,http_version,
                    status,status_text,mime,req_headers,req_body,req_size,
                    req_truncated,resp_headers,resp_body,resp_size,
                    resp_truncated,duration_ms,tls,client_addr,error,note
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                row,
            )
            flow_id = int(cur.lastrowid or 0)
            if self._fts_ok:
                try:
                    conn.execute(
                        "INSERT INTO flows_fts(rowid,host,path,req_text,resp_text)"
                        " VALUES (?,?,?,?,?)",
                        (
                            flow_id,
                            flow.get("host", ""),
                            flow.get("path", "") + (
                                "?" + flow["query"] if flow.get("query") else ""
                            ),
                            _to_text(req_body)[:TEXT_PREVIEW_LIMIT],
                            _to_text(resp_body)[:TEXT_PREVIEW_LIMIT],
                        ),
                    )
                except sqlite3.DatabaseError:
                    pass
            conn.commit()
        self._maybe_trim()
        return flow_id

    def _maybe_trim(self) -> None:
        if self.max_flows <= 0:
            return
        if getattr(self._local, "_writes", 0) < 200:
            self._local._writes = getattr(self._local, "_writes", 0) + 1
            return
        self._local._writes = 0
        with self._lock:
            conn = self._conn()
            cnt = conn.execute("SELECT COUNT(*) FROM flows").fetchone()[0]
            if cnt <= self.max_flows:
                self._maybe_checkpoint()
                return
            cut = cnt - self.max_flows
            ids = [r[0] for r in conn.execute(
                "SELECT id FROM flows ORDER BY id ASC LIMIT ?", (cut,)
            )]
            if ids:
                conn.executemany("DELETE FROM flows WHERE id=?", [(i,) for i in ids])
                if self._fts_ok:
                    conn.executemany("DELETE FROM flows_fts WHERE rowid=?",
                                     [(i,) for i in ids])
                conn.commit()
            self._maybe_checkpoint()

    def _maybe_checkpoint(self) -> None:
        """WAL 涨到一定体积就合并回主库，避免 -wal 文件长期膨胀。"""
        try:
            wal = Path(str(self.db_path) + "-wal")
            if wal.is_file() and wal.stat().st_size > 64 * 1024 * 1024:
                self._conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except (OSError, sqlite3.DatabaseError):
            pass

    # ---------------- 查询 ----------------
    def list(self, *, q: str = "", host: str = "", method: str = "",
             status: str = "", scheme: str = "", mime: str = "",
             since: float | None = None, until: float | None = None,
             limit: int = 100, offset: int = 0,
             order: str = "id_desc") -> dict[str, Any]:
        where: list[str] = []
        args: list[Any] = []

        if host:
            where.append("host LIKE ? ESCAPE '\\'")
            args.append("%" + _like_escape(host) + "%")
        if method:
            methods = [m.strip().upper() for m in method.split(",") if m.strip()]
            if methods:
                where.append("method IN (%s)" % ",".join("?" * len(methods)))
                args.extend(methods)
        if scheme:
            where.append("scheme = ?")
            args.append(scheme.lower())
        if mime:
            where.append("mime LIKE ? ESCAPE '\\'")
            args.append("%" + _like_escape(mime) + "%")
        if since is not None:
            where.append("ts >= ?")
            args.append(since)
        if until is not None:
            where.append("ts <= ?")
            args.append(until)
        if status:
            expr = _status_clause(status)
            if expr:
                where.append(expr[0])
                args.extend(expr[1])

        conn = self._conn()
        ids: list[int] | None = None
        if q:
            ids = self._search_ids(q, 3000)
            if not ids:
                return {"total": 0, "items": [], "fts": self._fts_ok}
            where.append("id IN (%s)" % ",".join("?" * len(ids)))
            args.extend(ids)

        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        order_sql = "id ASC" if order == "id_asc" else "id DESC"
        total = conn.execute(
            f"SELECT COUNT(*) FROM flows{sql_where}", args
        ).fetchone()[0]
        rows = conn.execute(
            f"""SELECT id,ts,scheme,method,host,port,path,query,status,
                       status_text,mime,req_size,resp_size,duration_ms,tls,
                       error,client_addr
                FROM flows{sql_where}
                ORDER BY {order_sql} LIMIT ? OFFSET ?""",
            args + [max(1, min(int(limit), 500)), max(0, int(offset))],
        ).fetchall()
        return {"total": total, "items": [dict(r) for r in rows],
                "fts": self._fts_ok}

    def _search_ids(self, q: str, limit: int) -> list[int]:
        conn = self._conn()
        if self._fts_ok:
            try:
                rows = conn.execute(
                    "SELECT rowid FROM flows_fts WHERE flows_fts MATCH ?"
                    " ORDER BY rowid DESC LIMIT ?",
                    (_fts_query(q), limit),
                ).fetchall()
                if rows:
                    return [int(r[0]) for r in rows]
            except sqlite3.DatabaseError:
                pass
        # 退化：LIKE 匹配
        like = "%" + _like_escape(q) + "%"
        rows = conn.execute(
            "SELECT id FROM flows WHERE host LIKE ? ESCAPE '\\'"
            " OR path LIKE ? ESCAPE '\\' ORDER BY id DESC LIMIT ?",
            (like, like, limit),
        ).fetchall()
        return [int(r[0]) for r in rows]

    def get(self, flow_id: int) -> dict[str, Any] | None:
        conn = self._conn()
        row = conn.execute("SELECT * FROM flows WHERE id=?", (flow_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data["req_headers"] = json.loads(data["req_headers"] or "[]")
        data["resp_headers"] = json.loads(data["resp_headers"] or "[]")
        data["req_body_b64"] = _b64(data.pop("req_body", None))
        data["resp_body_b64"] = _b64(data.pop("resp_body", None))
        return data

    def delete(self, ids: Iterable[int]) -> int:
        ids = [int(i) for i in ids]
        if not ids:
            return 0
        with self._lock:
            conn = self._conn()
            conn.executemany("DELETE FROM flows WHERE id=?", [(i,) for i in ids])
            if self._fts_ok:
                conn.executemany("DELETE FROM flows_fts WHERE rowid=?",
                                 [(i,) for i in ids])
            conn.commit()
        return len(ids)

    def clear(self) -> None:
        with self._lock:
            conn = self._conn()
            conn.execute("DELETE FROM flows")
            if self._fts_ok:
                conn.execute("DELETE FROM flows_fts")
            conn.commit()
            # VACUUM 不能在事务中执行，必须单独提交后再跑
            try:
                conn.execute("VACUUM")
            except sqlite3.OperationalError:
                pass

    def compact(self) -> None:
        """把 WAL 合并回主库并重置日志，避免 -wal 文件无限增长。"""
        with self._lock:
            conn = self._conn()
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.DatabaseError:
                pass

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            try:
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
            except Exception:
                pass
            self._local.conn = None

    def stats(self) -> dict[str, Any]:
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(req_size),0) rq,"
            " COALESCE(SUM(resp_size),0) rs, MIN(ts) first, MAX(ts) last"
            " FROM flows"
        ).fetchone()
        hosts = conn.execute(
            "SELECT host, COUNT(*) c FROM flows GROUP BY host"
            " ORDER BY c DESC LIMIT 15"
        ).fetchall()
        return {
            "count": row["n"], "req_bytes": row["rq"], "resp_bytes": row["rs"],
            "first_ts": row["first"], "last_ts": row["last"],
            "top_hosts": [{"host": r["host"], "count": r["c"]} for r in hosts],
        }


def _b64(blob: bytes | None) -> str | None:
    if blob is None:
        return None
    return base64.b64encode(blob).decode("ascii")


def _like_escape(s: str) -> str:
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _fts_query(q: str) -> str:
    terms = [t for t in q.replace('"', " ").split() if t]
    if not terms:
        return '""'
    return " ".join('"%s"' % t for t in terms)


def _status_clause(status: str) -> tuple[str, list[Any]] | None:
    """支持 200 / 2xx / 4xx,5xx / >=400 / <300 写法。"""
    status = status.strip().replace(" ", "")
    if not status:
        return None
    parts = [p for p in status.split(",") if p]
    exprs: list[str] = []
    args: list[Any] = []
    for p in parts:
        if p.endswith("xx") and len(p) == 3 and p[0].isdigit():
            exprs.append("(status >= ? AND status <= ?)")
            base = int(p[0]) * 100
            args.extend([base, base + 99])
        elif p.startswith(">=") and p[2:].isdigit():
            exprs.append("status >= ?")
            args.append(int(p[2:]))
        elif p.startswith("<=") and p[2:].isdigit():
            exprs.append("status <= ?")
            args.append(int(p[2:]))
        elif p.startswith(">") and p[1:].isdigit():
            exprs.append("status > ?")
            args.append(int(p[1:]))
        elif p.startswith("<") and p[1:].isdigit():
            exprs.append("status < ?")
            args.append(int(p[1:]))
        elif p.isdigit():
            exprs.append("status = ?")
            args.append(int(p))
        elif "-" in p and all(x.isdigit() for x in p.split("-", 1)):
            a, b = p.split("-", 1)
            exprs.append("(status >= ? AND status <= ?)")
            args.extend([int(a), int(b)])
    if not exprs:
        return None
    return "(" + " OR ".join(exprs) + ")", args
