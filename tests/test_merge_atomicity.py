"""合并事务的原子性与乐观并发回归测试。

覆盖：
- 故障注入：新快照 / 审计 / current_rev 任一步写入失败时整体回滚，
  版本列表、审计、current_rev 三者保持一致，连接可继续使用；
- 去重合并路径与导入路径同样在单一事务内，失败不留痕；
- 并发回归：两个连接（以及同一应用连接上的两个 HTTP 请求）以同一 base_rev
  并发合并时，恰有一个成功，另一个在事务内重新核对基线后得到 409，
  陈旧结果不会覆盖当前指针。
"""
import json
import os
import tempfile
import threading
import time

import pytest

_tmp = tempfile.mkdtemp()
os.environ["LRC_DB_PATH"] = os.path.join(_tmp, f"test_atomic_{os.getpid()}.db")

from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import service  # noqa: E402
from app.faults import faults  # noqa: E402
from app.main import DB_PATH, app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_faults():
    faults.reset()
    yield
    faults.reset()


def _extra_conn():
    """同一数据库文件的独立连接：用于跨连接并发/故障注入验证。"""
    return dbmod.connect(DB_PATH)


def _import_base(client, files, parents=None):
    body = {"base_locale": "en",
            "files": [{"filename": name, "content": content}
                      for name, content in files],
            "parents": parents or {"fr": "en"}}
    r = client.post("/api/import-json", json=body)
    assert r.status_code == 200, r.text
    return r.json()["rev"]


def _unique_base(client, tag):
    """每个用例唯一内容，避免导入幂等去重让多个用例共享同一基线。"""
    return _import_base(client, [
        ("en.json", json.dumps({"a": "{x}", "_tag": tag})),
        ("fr.json", json.dumps({"a": "{y}", "_tag": tag})),
    ])


def _revs(conn):
    return {r["rev"] for r in dbmod.list_revisions(conn)}


def _merge_audits(conn):
    return [a for a in dbmod.list_audit(conn, limit=500)
            if a["action"] == "merge"]


# ---------------------------------------------------------------- 故障注入

def test_fault_after_insert_revision_rolls_back_everything(client):
    base = _unique_base(client, "fault-rev")
    conn = _extra_conn()
    try:
        faults.fail_once("merge.after_insert_revision")
        with pytest.raises(RuntimeError, match="fault injected"):
            service.apply_merge(
                conn, base_rev=base,
                fixes=[{"op": "upsert", "locale": "fr", "key": "a",
                        "value": "BROKEN"}])

        # 全部回滚：当前指针不变、新版本不存在、没有 merge 审计
        assert dbmod.get_current_rev(conn) == base
        revs = _revs(conn)
        assert base in revs
        assert all(a["base_rev"] != base or a["action"] != "merge"
                   for a in dbmod.list_audit(conn, limit=500))

        # 连接仍可用：清掉故障后重试成功，审计/版本/状态一致
        result = service.apply_merge(
            conn, base_rev=base,
            fixes=[{"op": "upsert", "locale": "fr", "key": "a",
                    "value": "FIXED"}])
        new_rev = result["rev"]
        assert dbmod.get_current_rev(conn) == new_rev
        assert new_rev in _revs(conn)
        rec = next(a for a in _merge_audits(conn) if a["to_rev"] == new_rev)
        assert rec["base_rev"] == base and rec["from_rev"] == base
        # 应用单连接视角同样一致
        assert client.get("/api/state").json()["current_rev"] == new_rev
    finally:
        conn.close()


def test_fault_after_insert_audit_rolls_back_snapshot_and_state(client):
    base = _unique_base(client, "fault-audit")
    conn = _extra_conn()
    try:
        faults.fail_once("merge.after_insert_audit")
        with pytest.raises(RuntimeError):
            service.apply_merge(
                conn, base_rev=base,
                fixes=[{"op": "upsert", "locale": "fr", "key": "a",
                        "value": "Z1"}])
        assert dbmod.get_current_rev(conn) == base
        # 本次合并审计回滚（按本基线过滤，避免其他用例数据干扰）
        assert not [a for a in _merge_audits(conn) if a["base_rev"] == base]
        # 新快照未留下：版本表里不存在包含 Z1 的 fr 条目
        for rr in dbmod.list_revisions(conn):
            snap = dbmod.load_revision(conn, rr["rev"])
            assert snap["bundles"]["fr"]["entries"].get("a") != "Z1"
    finally:
        conn.close()


def test_fault_after_set_current_rolls_back_pointer_too(client):
    base = _unique_base(client, "fault-state")
    conn = _extra_conn()
    try:
        faults.fail_once("merge.after_set_current")
        with pytest.raises(RuntimeError):
            service.apply_merge(
                conn, base_rev=base,
                fixes=[{"op": "upsert", "locale": "fr", "key": "a",
                        "value": "Z2"}])
        # 即使 state 更新语句已执行，提交前失败也必须回滚指针
        assert dbmod.get_current_rev(conn) == base
        assert client.get("/api/state").json()["current_rev"] == base
        assert not [a for a in _merge_audits(conn) if a["base_rev"] == base]
    finally:
        conn.close()


def test_fault_on_deduplicated_merge_path_rolls_back(client):
    """去重合并（结果与既有版本相同）的审计与指针切换也在同一事务内。"""
    base = _import_base(client, [
        ("en.json", json.dumps({"a": "A"})),
        ("fr.json", json.dumps({"a": "A0"})),
    ])
    # r0(base) -> rX(value X) -> rY(value Y)；再合并回 X 会命 rX 去重
    r_x = client.post("/api/merge", json={"base_rev": base, "fixes": [
        {"op": "upsert", "locale": "fr", "key": "a", "value": "X"}]}).json()["rev"]
    r_y = client.post("/api/merge", json={"base_rev": r_x, "fixes": [
        {"op": "upsert", "locale": "fr", "key": "a", "value": "Y"}]}).json()["rev"]
    audits_before = len(_merge_audits(_extra_conn()))

    conn = _extra_conn()
    try:
        faults.fail_once("merge.dedup.after_audit")
        with pytest.raises(RuntimeError):
            service.apply_merge(
                conn, base_rev=r_y,
                fixes=[{"op": "upsert", "locale": "fr", "key": "a",
                        "value": "X"}])
        # 去重审计回滚、指针仍指向 rY
        assert dbmod.get_current_rev(conn) == r_y
        assert len(_merge_audits(conn)) == audits_before

        # 清故障后重放：去重生效，只新增一条审计，不新增版本
        result = service.apply_merge(
            conn, base_rev=r_y,
            fixes=[{"op": "upsert", "locale": "fr", "key": "a",
                    "value": "X"}])
        assert result["deduplicated"] is True and result["rev"] == r_x
        assert dbmod.get_current_rev(conn) == r_x
        assert len(_merge_audits(conn)) == audits_before + 1
    finally:
        conn.close()


def test_fault_during_import_rolls_back(client):
    prior = client.get("/api/state").json()["current_rev"]
    conn = _extra_conn()
    try:
        faults.fail_once("import.after_insert_audit")
        with pytest.raises(RuntimeError):
            service.import_snapshot(
                conn, base_locale="en",
                files=[{"filename": "en.json",
                        "content": json.dumps({"unique": "v"})},
                       {"filename": "fr.json",
                        "content": json.dumps({"unique": "vf"})}],
                parents_form={"fr": "en"})
        # 版本与审计都不留痕，当前指针不变
        assert dbmod.get_current_rev(conn) == prior
        assert all(r["label"] == "" for r in dbmod.list_revisions(conn))
        assert not any(a["action"] == "import" and
                       "unique" in str(a["detail"])
                       for a in dbmod.list_audit(conn, limit=500))
    finally:
        conn.close()


# ---------------------------------------------------------------- 并发回归

@pytest.mark.parametrize("round_no", range(5))
def test_concurrent_merges_same_base_only_one_wins(client, round_no):
    """两个独立连接以同一 base_rev 并发合并：一个 200，一个 409。"""
    base = _import_base(client, [
        ("en.json", json.dumps({"k": f"base{round_no}"})),
        ("fr.json", json.dumps({"k": f"old{round_no}"})),
    ])
    conn_a = _extra_conn()
    conn_b = _extra_conn()
    entered = threading.Event()
    gate = threading.Event()

    def hook(conn, **kw):
        # A 拿到写锁进入事务后停住，把 B 挡在 BEGIN IMMEDIATE 上
        if conn is conn_a:
            entered.set()
            gate.wait(10)

    faults.set("merge.before_recheck", hook)
    outcomes = {}
    revs = {}

    def do_merge(tag, conn, value):
        try:
            out = service.apply_merge(
                conn, base_rev=base,
                fixes=[{"op": "upsert", "locale": "fr", "key": "k",
                        "value": value}])
            outcomes[tag] = ("ok", out["rev"])
            revs[tag] = out["rev"]
        except service.ConflictError as e:
            outcomes[tag] = ("conflict", str(e))
        except Exception as e:  # 不允许出现其他错误（如 database is locked）
            outcomes[tag] = ("error", repr(e))

    t_a = threading.Thread(
        target=do_merge, args=("a", conn_a, f"WIN_{round_no}"))
    t_b = threading.Thread(
        target=do_merge, args=("b", conn_b, f"STALE_{round_no}"))
    try:
        t_a.start()
        assert entered.wait(5), "A 未能进入合并事务"
        t_b.start()
        # 等 B 抵达写锁等待
        time.sleep(0.5)
        gate.set()
        t_a.join(5)
        t_b.join(5)
    finally:
        faults.reset()
        conn_a.close()
        conn_b.close()

    assert not t_a.is_alive() and not t_b.is_alive()
    statuses = {tag: oc[0] for tag, oc in outcomes.items()}
    assert statuses == {"a": "ok", "b": "conflict"}, outcomes

    # 指针是赢家的版本；输家的陈旧结果从未落库
    winner_rev = revs["a"]
    watcher = _extra_conn()
    try:
        assert dbmod.get_current_rev(watcher) == winner_rev
        all_revs = _revs(watcher)
        assert winner_rev in all_revs and base in all_revs
        merge_recs = [a for a in _merge_audits(watcher)
                      if a["base_rev"] == base]
        assert len(merge_recs) == 1
        assert merge_recs[0]["to_rev"] == winner_rev
        val = dbmod.load_revision(watcher, winner_rev)["bundles"]["fr"]["entries"]["k"]
        assert val == f"WIN_{round_no}"
    finally:
        watcher.close()
    assert client.get("/api/state").json()["current_rev"] == winner_rev


def test_concurrent_http_requests_sharing_app_connection(client):
    """同一应用单连接上的两个并发请求：进程内 RLock + 事务内复核保证不双写。"""
    base = _import_base(client, [
        ("en.json", json.dumps({"k": "http-base"})),
        ("fr.json", json.dumps({"k": "http-old"})),
    ])
    entered = threading.Event()
    gate = threading.Event()
    state: dict = {}

    def hook(**kw):
        # 第一个进入事务的请求线程充当 A，停在临界区内
        state.setdefault("a", threading.get_ident())
        if threading.get_ident() == state["a"]:
            entered.set()
            gate.wait(10)

    faults.set("merge.before_recheck", hook)
    results = {}

    def do_post(tag, value):
        r = client.post("/api/merge", json={
            "base_rev": base,
            "fixes": [{"op": "upsert", "locale": "fr", "key": "k",
                       "value": value}]})
        results[tag] = (r.status_code, r.json())

    t1 = threading.Thread(target=do_post, args=("a", "HTTP-WIN"))
    t2 = threading.Thread(target=do_post, args=("b", "HTTP-STALE"))
    t1.start()
    assert entered.wait(5)
    t2.start()
    time.sleep(0.5)
    gate.set()
    t1.join(10)
    t2.join(10)
    faults.reset()

    statuses = sorted(results[t][0] for t in ("a", "b"))
    assert statuses == [200, 409], results
    ok = next(v for code, v in results.values() if code == 200)
    assert client.get("/api/state").json()["current_rev"] == ok["rev"]
    entries = client.get(
        f"/api/revisions/{ok['rev']}/entries/fr").json()["entries"]
    assert entries["k"] == "HTTP-WIN"
