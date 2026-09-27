"""SQLite 持久层：版本快照、审计记录、当前状态。

每个 revision 保存一次导入的不可变快照（规范化 JSON + 输入哈希）。
合并修复会产生新 revision；旧版本永久保留以支持差异报告与审计。

事务约定
--------
所有写操作（insert_revision / insert_audit / set_current_rev …）自身**不**
提交，必须包裹在 :func:`transaction` 中，由服务层决定事务边界。
``transaction`` 使用 ``BEGIN IMMEDIATE`` 立即取得 SQLite 写锁：
- 同一连接在进程内由 ``RLock`` 串行化（FastAPI 单连接可被多线程复用）；
- 跨进程/多连接由 SQLite 写锁串行化，后来者在事务内重新核对基线，
  保证同一 base_rev 只有一个合并成功提交。
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from contextlib import contextmanager
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS revision (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    rev TEXT UNIQUE NOT NULL,
    base_locale TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    label TEXT NOT NULL DEFAULT '',
    parent_json TEXT NOT NULL DEFAULT '{}',
    bundle_count INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS bundle (
    revision_id INTEGER NOT NULL,
    locale TEXT NOT NULL,
    filename TEXT NOT NULL,
    entries_json TEXT NOT NULL,
    invalid_json TEXT NOT NULL DEFAULT '[]',
    parse_error TEXT,
    PRIMARY KEY (revision_id, locale),
    FOREIGN KEY (revision_id) REFERENCES revision(id)
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    from_rev TEXT,
    to_rev TEXT,
    base_rev TEXT NOT NULL,
    detail_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS rename_record (
    base_rev TEXT NOT NULL,
    plan_hash TEXT NOT NULL,
    from_rev TEXT NOT NULL,
    to_rev TEXT NOT NULL,
    input_hash TEXT NOT NULL,
    audit_id INTEGER NOT NULL,
    PRIMARY KEY (base_rev, plan_hash),
    FOREIGN KEY (base_rev) REFERENCES revision(rev),
    FOREIGN KEY (to_rev) REFERENCES revision(rev),
    FOREIGN KEY (audit_id) REFERENCES audit(id)
);
CREATE TABLE IF NOT EXISTS state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    current_rev TEXT
);
INSERT OR IGNORE INTO state (id, current_rev) VALUES (1, NULL);
"""


class Connection:
    """sqlite3 连接包装：autocommit 模式 + 进程内事务串行锁。

    除 ``_lock`` 外所有属性透传给底层 sqlite3.Connection。服务层必须通过
    :func:`transaction` 显式管理事务，不使用驱动隐式事务。
    """

    def __init__(self, raw: sqlite3.Connection):
        self._raw = raw
        self._lock = threading.RLock()

    def __getattr__(self, name):
        return getattr(self._raw, name)


def connect(db_path: str) -> Connection:
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    raw = sqlite3.connect(db_path, timeout=30, check_same_thread=False,
                          isolation_level=None)  # autocommit：事务由我们显式控制
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON")
    raw.execute("PRAGMA journal_mode = WAL")
    raw.execute("PRAGMA busy_timeout = 30000")
    return Connection(raw)


def init_db(conn: Connection) -> None:
    conn.executescript(SCHEMA)


@contextmanager
def transaction(conn: Connection) -> Iterator[Connection]:
    """单一写事务边界：BEGIN IMMEDIATE … COMMIT，异常一律 ROLLBACK。

    同连接的线程由 RLock 串行；跨连接由 SQLite 写锁串行（busy_timeout 等待）。
    事务体内必须重新读取/核对任何并发前置条件（如 current_rev）。
    """
    with conn._lock:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.rollback()
            raise


def now_iso() -> str:
    # 固定 UTC，确定性展示
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# ---------------------------------------------------------------- 读写

def canonical_bundle_json(entries: dict[str, str]) -> str:
    """规范化：按键排序、ensure_ascii=False、统一分隔符。"""
    return json.dumps(entries, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def find_revision_by_hash(conn: Connection, input_hash: str) -> Optional[str]:
    row = conn.execute(
        "SELECT rev FROM revision WHERE input_hash = ?", (input_hash,)).fetchone()
    return row["rev"] if row else None


def insert_revision(conn: Connection, *, rev: str, base_locale: str,
                    input_hash: str, label: str,
                    parents: dict[str, Optional[str]],
                    bundles: list[dict]) -> None:
    """bundles: [{locale, filename, entries, invalid, parse_error}]

    不自行提交：调用方必须持有 :func:`transaction` 事务。
    """
    created = now_iso()
    cur = conn.execute(
        "INSERT INTO revision (rev, base_locale, input_hash, created_at,"
        " label, parent_json, bundle_count) VALUES (?,?,?,?,?,?,?)",
        (rev, base_locale, input_hash, created, label,
         json.dumps(parents, ensure_ascii=False, sort_keys=True),
         len(bundles)))
    rid = cur.lastrowid
    for b in bundles:
        conn.execute(
            "INSERT INTO bundle (revision_id, locale, filename,"
            " entries_json, invalid_json, parse_error) VALUES (?,?,?,?,?,?)",
            (rid, b["locale"], b["filename"],
             canonical_bundle_json(b["entries"]),
             json.dumps(b["invalid"], ensure_ascii=False, sort_keys=True),
             b.get("parse_error")))


def list_revisions(conn: Connection) -> list[dict]:
    rows = conn.execute(
        "SELECT r.rev, r.base_locale, r.input_hash, r.created_at, r.label,"
        " r.bundle_count, (SELECT rev FROM state WHERE id=1) AS current_rev"
        " FROM revision r ORDER BY r.id DESC").fetchall()
    return [dict(r) for r in rows]


def _revision_id(conn: Connection, rev: str) -> Optional[int]:
    row = conn.execute("SELECT id FROM revision WHERE rev = ?", (rev,)).fetchone()
    return row["id"] if row else None


def get_revision_meta(conn: Connection, rev: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM revision WHERE rev = ?", (rev,)).fetchone()
    return dict(row) if row else None


def load_revision(conn: Connection, rev: str) -> Optional[dict]:
    meta = get_revision_meta(conn, rev)
    if meta is None:
        return None
    rows = conn.execute(
        "SELECT locale, filename, entries_json, invalid_json, parse_error"
        " FROM bundle WHERE revision_id = ? ORDER BY locale",
        (meta["id"],)).fetchall()
    bundles = {}
    for r in rows:
        bundles[r["locale"]] = {
            "locale": r["locale"],
            "filename": r["filename"],
            "entries": json.loads(r["entries_json"]),
            "invalid": json.loads(r["invalid_json"]),
            "parse_error": r["parse_error"],
        }
    return {
        "meta": meta,
        "parents": json.loads(meta["parent_json"]),
        "bundles": bundles,
    }


def get_current_rev(conn: Connection) -> Optional[str]:
    row = conn.execute("SELECT current_rev FROM state WHERE id=1").fetchone()
    return row["current_rev"] if row else None


def set_current_rev(conn: Connection, rev: str) -> None:
    """不自行提交：调用方必须持有事务。"""
    conn.execute("UPDATE state SET current_rev = ? WHERE id=1", (rev,))


def insert_audit(conn: Connection, *, actor: str, action: str,
                 from_rev: Optional[str], to_rev: str, base_rev: str,
                 detail: dict) -> int:
    """不自行提交：调用方必须持有事务。返回新审计行 id。"""
    cur = conn.execute(
        "INSERT INTO audit (ts, actor, action, from_rev, to_rev,"
        " base_rev, detail_json) VALUES (?,?,?,?,?,?,?)",
        (now_iso(), actor, action, from_rev, to_rev, base_rev,
         json.dumps(detail, ensure_ascii=False, sort_keys=True)))
    return int(cur.lastrowid)


def find_rename_record(conn: Connection, base_rev: str,
                       plan_hash: str) -> Optional[dict]:
    """按源版本 + 规范化计划摘要查找已提交的重命名记录（不判断完整性）。"""
    row = conn.execute(
        "SELECT * FROM rename_record WHERE base_rev = ? AND plan_hash = ?",
        (base_rev, plan_hash)).fetchone()
    return dict(row) if row else None


def insert_rename_record(conn: Connection, *, base_rev: str, plan_hash: str,
                         from_rev: str, to_rev: str, input_hash: str,
                         audit_id: int) -> None:
    """登记一次成功提交的重命名。不自行提交：调用方必须持有同一事务，
    因此回滚的失败事务不可能留下记录，也绝不会被误认作「已完成」。"""
    conn.execute(
        "INSERT INTO rename_record (base_rev, plan_hash, from_rev, to_rev,"
        " input_hash, audit_id) VALUES (?,?,?,?,?,?)",
        (base_rev, plan_hash, from_rev, to_rev, input_hash, audit_id))


def get_audit_by_id(conn: Connection, audit_id: int) -> Optional[dict]:
    row = conn.execute(
        "SELECT * FROM audit WHERE id = ?", (audit_id,)).fetchone()
    if row is None:
        return None
    d = dict(row)
    d["detail"] = json.loads(d.pop("detail_json"))
    return d


def list_rename_audits(conn: Connection,
                       base_rev: Optional[str] = None) -> list[dict]:
    """全部（或指定源版本的）rename 审计，按 id 升序、detail 已解析。

    供旧数据卷升级时从历史审计恢复 rename_record：旧卷里成功迁移的审计
    （源/目标版本、基线、detail.plan_hash）与产物版本都在，只是升级后新建
    的 rename_record 是空表。detail_json 损坏时 detail 退化为空字典
    （该审计不具备可识别的计划摘要，不会被恢复逻辑采用）。
    """
    if base_rev is None:
        rows = conn.execute(
            "SELECT * FROM audit WHERE action = 'rename' ORDER BY id").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit WHERE action = 'rename' AND base_rev = ?"
            " ORDER BY id", (base_rev,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["detail"] = json.loads(d.pop("detail_json"))
        except (ValueError, TypeError):
            d["detail"] = {}
        out.append(d)
    return out


def list_audit(conn: Connection, limit: int = 100) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        d["detail"] = json.loads(d.pop("detail_json"))
        out.append(d)
    return out
