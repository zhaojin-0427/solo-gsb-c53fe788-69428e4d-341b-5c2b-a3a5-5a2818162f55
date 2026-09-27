"""旧 SQLite 数据卷升级恢复回归：

旧卷里已有成功的 rename 审计（含源/目标版本、基线与 detail.plan_hash）和
产物版本，但升级后初始化只建了**空** rename_record 表。客户端用原 rev、
原 expected_rev 与同一映射重试，必须在事务内从历史审计核对并恢复已完成
关系后回放首次结果，而不是在 current_rev 已推进后返回 409。

覆盖：
- 旧卷升级（DROP rename_record 后重新初始化）时的启动恢复；
- 未触发启动恢复时，确认链路自身在事务内恢复；
- 同源同计划的重复审计：关联一致才归并，目标版本/映射冲突明确拒绝回放；
- 残缺（缺映射明细、目标版本悬空、产物摘要对不上、无计划摘要）的处理；
- 已有有效记录绝不覆盖；重复升级幂等；并发恢复只登记一次。
"""
import json
import os
import sqlite3
import tempfile
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import db as dbmod  # noqa: E402
from app import service  # noqa: E402
from app import rename as renamemod  # noqa: E402
from app import main as appmod  # noqa: E402
from app.faults import faults  # noqa: E402

_tmp = tempfile.mkdtemp()


@pytest.fixture
def client(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test_rename_recovery.db")
    monkeypatch.setattr(appmod, "DB_PATH", db_path)
    monkeypatch.setattr(appmod, "_conn", None)
    with TestClient(appmod.app) as c:
        yield c
    if appmod._conn is not None:
        appmod._conn.close()
        monkeypatch.setattr(appmod, "_conn", None)


@pytest.fixture(autouse=True)
def _reset_faults():
    faults.reset()
    yield
    faults.reset()


def _extra_conn(client):
    """独立连接 + init_db：升级后表结构已就绪（空 rename_record），
    但不运行全量启动恢复（恢复只在应用主连接 conn() 里触发）。"""
    c = dbmod.connect(appmod.DB_PATH)
    dbmod.init_db(c)
    return c


def _import(client, files, parents=None):
    body = {"base_locale": "en",
            "files": [{"filename": name, "content": content}
                      for name, content in files],
            "parents": parents if parents is not None
            else {"fr": "en", "de": "en", "ja": "en"}}
    r = client.post("/api/import-json", json=body)
    assert r.status_code == 200, r.text
    return r.json()["rev"]


def _simple_base(client):
    return _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "fa {@b}"})),
    ])


def _drop_rename_records(path):
    """模拟旧卷升级：rename_record 表在旧版本中不存在（升级时只建空表）。"""
    raw = sqlite3.connect(path)
    try:
        raw.execute("DROP TABLE IF EXISTS rename_record")
        raw.commit()
    finally:
        raw.close()


def _restart_app_conn():
    """关闭应用共享连接并清空缓存：下次请求重新 init_db（建空表）+ 启动恢复。"""
    if appmod._conn is not None:
        appmod._conn.close()
    appmod._conn = None


def _rename_audit_ids(conn, base_rev=None):
    rows = dbmod.list_rename_audits(conn, base_rev)
    return [a["id"] for a in rows]


def _insert_legacy_audit(conn, *, from_rev, to_rev, base_rev, detail):
    with dbmod.transaction(conn):
        return dbmod.insert_audit(
            conn, actor="legacy", action="rename", from_rev=from_rev,
            to_rev=to_rev, base_rev=base_rev, detail=detail)


def _delete_rename_audits(conn):
    with dbmod.transaction(conn):
        conn.execute("DELETE FROM rename_record")
        conn.execute("DELETE FROM audit WHERE action = 'rename'")


# ---------------------------------------------------------------- 启动恢复

def test_startup_recovers_completed_rename_from_legacy_volume(client):
    rev = _simple_base(client)
    mappings = [{"old_key": "b", "new_key": "bee"}]
    c1 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": mappings})
    assert c1.status_code == 200
    first = c1.json()
    result_rev = first["new_rev"]
    audit_id = _rename_audit_ids(_extra_conn(client), rev)[0]
    n_audits = len(client.get("/api/audit").json()["records"])
    n_revs = len(client.get("/api/revisions").json()["revisions"])

    # 模拟旧卷升级：rename_record 表不存在 -> 应用重启后只建空表
    _restart_app_conn()
    _drop_rename_records(appmod.DB_PATH)

    # 首个触库请求触发 init_db + 启动恢复（/api/health 不触库）
    assert client.get("/api/state").status_code == 200

    watcher = _extra_conn(client)
    try:
        rec = dbmod.find_rename_record(
            watcher, rev, renamemod.plan_hash(mappings))
        assert rec is not None
        assert rec["to_rev"] == result_rev and rec["from_rev"] == rev
        assert rec["audit_id"] == audit_id  # 关联到首次（最早）审计
        assert watcher.execute(
            "SELECT COUNT(*) AS n FROM rename_record").fetchone()["n"] == 1
    finally:
        watcher.close()

    # 缺陷回归：令牌已过期（current=result_rev），用原 rev/expected_rev/同一
    # 映射重试必须回放首次结果，而不是 409
    c2 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": mappings})
    assert c2.status_code == 200, c2.text
    body = c2.json()
    assert body["replayed"] is True
    assert body["from_rev"] == rev and body["new_rev"] == result_rev
    assert body["changeset"] == first["changeset"]
    # 不新建版本、不追加审计、不切换当前指针
    assert client.get("/api/state").json()["current_rev"] == result_rev
    assert len(client.get("/api/audit").json()["records"]) == n_audits
    assert len(client.get("/api/revisions").json()["revisions"]) == n_revs


def test_startup_recovery_is_idempotent_across_repeated_upgrades(client):
    rev = _simple_base(client)
    mappings = [{"old_key": "b", "new_key": "bee"}]
    result_rev = client.post(
        f"/api/revisions/{rev}/rename/commit",
        json={"expected_rev": rev, "mappings": mappings}).json()["new_rev"]

    _restart_app_conn()
    _drop_rename_records(appmod.DB_PATH)
    assert client.get("/api/state").status_code == 200  # 首次升级恢复

    watcher = _extra_conn(client)
    try:
        stats1 = service.recover_completed_renames(watcher)
        stats2 = service.recover_completed_renames(watcher)
        assert stats1["restored"] == 0 and stats1["skipped"] == 1
        assert stats2 == stats1
        n = watcher.execute("SELECT COUNT(*) AS n FROM rename_record"
                            ).fetchone()["n"]
        assert n == 1
    finally:
        watcher.close()
    assert client.get("/api/state").json()["current_rev"] == result_rev


def test_commit_link_restores_inside_transaction_without_startup_recovery(client):
    """确认链路自身的恢复：建空表后不跑全量恢复，直接用过期令牌重试。"""
    rev = _simple_base(client)
    mappings = [{"old_key": "b", "new_key": "bee"}]
    result_rev = client.post(
        f"/api/revisions/{rev}/rename/commit",
        json={"expected_rev": rev, "mappings": mappings}).json()["new_rev"]
    assert client.get("/api/state").json()["current_rev"] == result_rev

    _drop_rename_records(appmod.DB_PATH)
    legacy = _extra_conn(client)  # 空表，未做全量恢复
    try:
        out = service.rename_commit(
            legacy, rev=rev, expected_rev=rev, mappings=mappings)
        assert out["replayed"] is True and out["new_rev"] == result_rev
        rec = dbmod.find_rename_record(
            legacy, rev, renamemod.plan_hash(mappings))
        assert rec is not None and rec["to_rev"] == result_rev
    finally:
        legacy.close()

    # 指针未被回放改动，审计/版本数量不变
    assert client.get("/api/state").json()["current_rev"] == result_rev


# ---------------------------------------------------------------- 重复审计

def test_consistent_duplicate_audits_are_merged(client):
    rev = _simple_base(client)
    mappings = [{"old_key": "b", "new_key": "bee"}]
    result_rev = client.post(
        f"/api/revisions/{rev}/rename/commit",
        json={"expected_rev": rev, "mappings": mappings}).json()["new_rev"]
    watcher = _extra_conn(client)
    try:
        first_id = _rename_audit_ids(watcher, rev)[0]
        detail = dbmod.get_audit_by_id(watcher, first_id)["detail"]
        # 同源同计划同产物的重复审计（如旧版本重试时留下的多条记录）
        dup_id = _insert_legacy_audit(
            watcher, from_rev=rev, to_rev=result_rev, base_rev=rev,
            detail=dict(detail))
        assert dup_id > first_id
    finally:
        watcher.close()

    _restart_app_conn()
    _drop_rename_records(appmod.DB_PATH)
    watcher = _extra_conn(client)
    try:
        stats = service.recover_completed_renames(watcher)
        assert stats["restored"] == 1 and stats["rejected"] == 0
        rec = dbmod.find_rename_record(
            watcher, rev, renamemod.plan_hash(mappings))
        assert rec is not None
        # 归并到最早审计；只登记一条
        assert rec["audit_id"] == first_id
        assert watcher.execute(
            "SELECT COUNT(*) AS n FROM rename_record").fetchone()["n"] == 1
    finally:
        watcher.close()

    # 回放可用且无写入
    n_audits = len(client.get("/api/audit").json()["records"])
    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": mappings})
    assert r.status_code == 200 and r.json()["replayed"] is True
    assert r.json()["new_rev"] == result_rev
    assert len(client.get("/api/audit").json()["records"]) == n_audits


def test_conflicting_duplicate_audits_rejected(client):
    """同源同计划的重复审计目标版本不一致：明确拒绝回放，不登记。"""
    rev = _simple_base(client)
    bee = [{"old_key": "b", "new_key": "bee"}]
    bzz = [{"old_key": "b", "new_key": "bzz"}]
    first = client.post(
        f"/api/revisions/{rev}/rename/commit",
        json={"expected_rev": rev, "mappings": bee}).json()
    rev_bee, hash_bee = first["new_rev"], first["plan_hash"]

    # 切回旧基线，再做另一产物 bzz
    reset = _extra_conn(client)
    try:
        with dbmod.transaction(reset):
            dbmod.set_current_rev(reset, rev)
    finally:
        reset.close()
    second = client.post(
        f"/api/revisions/{rev}/rename/commit",
        json={"expected_rev": rev, "mappings": bzz}).json()
    rev_bzz = second["new_rev"]
    assert rev_bzz != rev_bee
    assert client.get("/api/state").json()["current_rev"] == rev_bzz

    # 伪造一条冲突审计：(base, hash_bee) 却指向 rev_bzz 产物
    bad = _extra_conn(client)
    try:
        _insert_legacy_audit(
            bad, from_rev=rev, to_rev=rev_bzz, base_rev=rev,
            detail={"plan_hash": hash_bee, "mappings": bee})
    finally:
        bad.close()

    _restart_app_conn()
    _drop_rename_records(appmod.DB_PATH)
    watcher = _extra_conn(client)
    try:
        stats = service.recover_completed_renames(watcher)
        # bzz 组正常恢复；bee 组（两个冲突目标）被拒绝
        assert stats["restored"] == 1 and stats["rejected"] == 1
        assert "冲突" in stats["rejections"][0]["reason"]
        assert dbmod.find_rename_record(
            watcher, rev, hash_bee) is None
    finally:
        watcher.close()

    # 启动恢复已拒绝该组；确认链路再次核对同样拒绝（500，明确报错）
    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": bee})
    assert r.status_code == 500
    assert "冲突" in r.json()["detail"]["error"]
    # 指针、审计、版本均无变化
    assert client.get("/api/state").json()["current_rev"] == rev_bzz
    assert dbmod.find_rename_record(
        _extra_conn(client), rev, hash_bee) is None


# ---------------------------------------------------------------- 残缺关系

def test_audit_without_mappings_detail_rejected(client):
    rev = _simple_base(client)
    mappings = [{"old_key": "b", "new_key": "bee"}]
    h = renamemod.plan_hash(mappings)
    client.post(f"/api/revisions/{rev}/rename/commit",
                json={"expected_rev": rev, "mappings": mappings})
    legacy = _extra_conn(client)
    try:
        _delete_rename_audits(legacy)
        # 残缺审计：有计划摘要但没有映射明细，无法核对计划与产物
        _insert_legacy_audit(
            legacy, from_rev=rev, to_rev=rev, base_rev=rev,
            detail={"plan_hash": h})
    finally:
        legacy.close()

    _restart_app_conn()
    _drop_rename_records(appmod.DB_PATH)
    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": mappings})
    assert r.status_code == 500
    assert "映射明细" in r.json()["detail"]["error"]


def test_dangling_product_revision_rejected(client):
    rev = _simple_base(client)
    mappings = [{"old_key": "b", "new_key": "bee"}]
    h = renamemod.plan_hash(mappings)
    client.post(f"/api/revisions/{rev}/rename/commit",
                json={"expected_rev": rev, "mappings": mappings})
    legacy = _extra_conn(client)
    try:
        _delete_rename_audits(legacy)
        _insert_legacy_audit(
            legacy, from_rev=rev, to_rev="rdead00000000", base_rev=rev,
            detail={"plan_hash": h, "mappings": mappings})
    finally:
        legacy.close()

    _restart_app_conn()
    _drop_rename_records(appmod.DB_PATH)
    watcher = _extra_conn(client)
    try:
        stats = service.recover_completed_renames(watcher)
        assert stats["rejected"] == 1 and stats["restored"] == 0
        reason = stats["rejections"][0]["reason"]
        assert "产物版本" in reason and "缺失" in reason
    finally:
        watcher.close()
    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": mappings})
    assert r.status_code == 500
    assert "产物版本" in r.json()["detail"]["error"]
    assert "缺失" in r.json()["detail"]["error"]


def test_product_digest_mismatch_rejected(client):
    """审计指向的产物与按源版本+审计计划重算的摘要不符：拒绝回放。"""
    rev = _simple_base(client)
    mappings = [{"old_key": "b", "new_key": "bee"}]
    h = renamemod.plan_hash(mappings)
    # 另一个不相关的版本（merge 产物）
    other = client.post("/api/merge", json={"base_rev": rev, "fixes": [
        {"op": "upsert", "locale": "fr", "key": "a", "value": "fa2"}]}).json()
    rev_other = other["rev"]
    legacy = _extra_conn(client)
    try:
        _insert_legacy_audit(
            legacy, from_rev=rev, to_rev=rev_other, base_rev=rev,
            detail={"plan_hash": h, "mappings": mappings})
    finally:
        legacy.close()

    watcher = _extra_conn(client)
    try:
        stats = service.recover_completed_renames(watcher)
        assert stats["rejected"] == 1
        assert "产物" in stats["rejections"][0]["reason"]
        assert dbmod.find_rename_record(watcher, rev, h) is None
    finally:
        watcher.close()


def test_audit_without_plan_hash_is_not_recovered_and_still_conflicts(client):
    """无计划摘要的旧 rename 审计无法识别：不恢复；过期请求仍 409。"""
    rev = _simple_base(client)
    other = _import(client, [
        ("en.json", json.dumps({"a": "other {@b}", "b": "B", "z": "z"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])
    assert client.get("/api/state").json()["current_rev"] == other
    legacy = _extra_conn(client)
    try:
        _insert_legacy_audit(
            legacy, from_rev=rev, to_rev=other, base_rev=rev, detail={})
        stats = service.recover_completed_renames(legacy)
        assert stats["restored"] == stats["rejected"] == 0
    finally:
        legacy.close()

    mappings = [{"old_key": "b", "new_key": "bee"}]
    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": mappings})
    assert r.status_code == 409  # 未完成的过期请求
    assert client.get("/api/state").json()["current_rev"] == other


# ---------------------------------------------------------------- 不覆盖/并发

def test_existing_valid_record_is_never_overwritten(client):
    rev = _simple_base(client)
    mappings = [{"old_key": "b", "new_key": "bee"}]
    result_rev = client.post(
        f"/api/revisions/{rev}/rename/commit",
        json={"expected_rev": rev, "mappings": mappings}).json()["new_rev"]
    rec_before = dbmod.find_rename_record(
        _extra_conn(client), rev, renamemod.plan_hash(mappings))

    # 在已有有效记录的情况下塞入冲突重复审计
    other = client.post("/api/merge", json={"base_rev": result_rev, "fixes": [
        {"op": "upsert", "locale": "fr", "key": "a", "value": "fa2"}]}).json()
    rev_other = other["rev"]
    bad = _extra_conn(client)
    try:
        _insert_legacy_audit(
            bad, from_rev=rev, to_rev=rev_other, base_rev=rev,
            detail={"plan_hash": renamemod.plan_hash(mappings),
                    "mappings": mappings})
        stats = service.recover_completed_renames(bad)
    finally:
        bad.close()
    # 已有记录：直接跳过，不去比较新审计，更不覆盖
    assert stats["skipped"] == 1 and stats["restored"] == 0
    rec_after = dbmod.find_rename_record(
        _extra_conn(client), rev, renamemod.plan_hash(mappings))
    assert rec_after == rec_before
    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": mappings})
    assert r.status_code == 200 and r.json()["replayed"] is True
    assert r.json()["new_rev"] == result_rev


def test_concurrent_legacy_recovery_registers_only_once(client):
    """并发重试旧卷（空 rename_record）：写锁内串行，只登记一次完成关系。"""
    rev = _simple_base(client)
    mappings = [{"old_key": "b", "new_key": "bee"}]
    result_rev = client.post(
        f"/api/revisions/{rev}/rename/commit",
        json={"expected_rev": rev, "mappings": mappings}).json()["new_rev"]
    n_rename_audits = len([
        a for a in client.get("/api/audit").json()["records"]
        if a["action"] == "rename"])

    _drop_rename_records(appmod.DB_PATH)
    conn_a = _extra_conn(client)
    conn_b = _extra_conn(client)
    entered = threading.Event()
    gate = threading.Event()

    def hook(conn, **kw):
        if conn is conn_a:
            entered.set()
            gate.wait(10)

    faults.set("rename.before_recheck", hook)
    outcomes = {}

    def do(tag, conn):
        try:
            out = service.rename_commit(
                conn, rev=rev, expected_rev=rev, mappings=mappings)
            outcomes[tag] = ("ok", out["new_rev"], out.get("replayed", False))
        except Exception as e:  # noqa: BLE001
            outcomes[tag] = ("error", repr(e), False)

    t_a = threading.Thread(target=do, args=("a", conn_a))
    t_b = threading.Thread(target=do, args=("b", conn_b))
    try:
        t_a.start()
        assert entered.wait(5)
        t_b.start()
        time.sleep(0.5)
        gate.set()
        t_a.join(5); t_b.join(5)
    finally:
        faults.reset()
        conn_a.close(); conn_b.close()

    assert set(outcomes) == {"a", "b"}, outcomes
    assert all(v[0] == "ok" and v[1] == result_rev for v in
               outcomes.values()), outcomes
    watcher = _extra_conn(client)
    try:
        rows = watcher.execute(
            "SELECT * FROM rename_record WHERE base_rev = ?",
            (rev,)).fetchall()
        assert len(rows) == 1 and rows[0]["to_rev"] == result_rev
        assert dbmod.get_current_rev(watcher) == result_rev
        assert len([a for a in dbmod.list_audit(watcher, 500)
                    if a["action"] == "rename"]) == n_rename_audits
    finally:
        watcher.close()


def test_deduplicated_audit_is_recovered_from_legacy_volume(client):
    """首次完成走内容哈希去重（deduplicated=True）的旧卷也能恢复回放。"""
    rev_p = _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"}))], parents={})
    rev_q = _import(client, [
        ("en.json", json.dumps({"a": "x {@b2}", "b2": "B"}))], parents={})
    mappings_p = [{"old_key": "b", "new_key": "bee"}]
    first_q = client.post(f"/api/revisions/{rev_q}/rename/commit",
                          json={"expected_rev": rev_q,
                                "mappings": [{"old_key": "b2",
                                              "new_key": "bee"}]})
    rev_a = first_q.json()["new_rev"]

    reset = _extra_conn(client)
    try:
        with dbmod.transaction(reset):
            dbmod.set_current_rev(reset, rev_p)
    finally:
        reset.close()
    first_p = client.post(f"/api/revisions/{rev_p}/rename/commit",
                          json={"expected_rev": rev_p, "mappings": mappings_p})
    assert first_p.json()["deduplicated"] is True

    _restart_app_conn()
    _drop_rename_records(appmod.DB_PATH)
    assert client.get("/api/state").status_code == 200
    again = client.post(f"/api/revisions/{rev_p}/rename/commit",
                        json={"expected_rev": rev_p, "mappings": mappings_p})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["replayed"] is True and body["new_rev"] == rev_a
    assert body["from_rev"] == rev_p
