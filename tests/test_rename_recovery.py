"""旧数据卷升级后的重命名完成记录恢复：旧库、重复审计与异常关系回归。

场景：旧版本数据卷里已有成功的 rename 审计（含源/目标版本、基线与
detail.plan_hash）和产物版本，却没有 rename_record（该表后建）。升级后
init_db 只建空表；启动恢复与确认链路必须在事务内从历史审计重建登记关系，
使客户端用原 rev、原 expected_rev、同一映射重试时回放首次结果而不是 409。
"""
import json
import sqlite3
import threading
import time

import pytest

from fastapi.testclient import TestClient

from app import db as dbmod
from app import service
from app import main as appmod
from app.faults import faults

MAPPINGS = [{"old_key": "b", "new_key": "bee"}]


@pytest.fixture
def client(tmp_path, monkeypatch):
    """每个用例独立的 SQLite 文件：避免共享库里的版本/审计/指针相互污染。"""
    db_path = str(tmp_path / "test_recovery.db")
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


def _import(client, files, parents=None):
    body = {"base_locale": "en",
            "files": [{"filename": n, "content": c} for n, c in files],
            "parents": parents if parents is not None else {"fr": "en"}}
    r = client.post("/api/import-json", json=body)
    assert r.status_code == 200, r.text
    return r.json()["rev"]


def _setup_completed_rename(client):
    """导入并完成一次 b->bee 迁移，返回 (源版本, 首次确认响应)。"""
    rev = _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "fa {@b}"})),
    ])
    c1 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": MAPPINGS})
    assert c1.status_code == 200, c1.text
    return rev, c1.json()


def _legacyize():
    """把当前库回退为「旧卷」：删除全部 rename_record（保留审计与产物）。"""
    raw = sqlite3.connect(appmod.DB_PATH)
    try:
        raw.execute("DELETE FROM rename_record")
        raw.commit()
    finally:
        raw.close()


def _restart_app():
    """模拟升级重启：关闭并丢弃连接，下次请求重新 init_db + 启动恢复。"""
    if appmod._conn is not None:
        appmod._conn.close()
        appmod._conn = None


def _extra_conn():
    return dbmod.connect(appmod.DB_PATH)


# ---------------------------------------------------------------- 旧库升级恢复

def test_upgrade_recovers_completed_rename_from_history(client):
    """旧卷升级：启动恢复重建登记后，原 rev/expected_rev/映射重试回放而非 409。"""
    rev, first = _setup_completed_rename(client)
    product = first["new_rev"]
    plan_hash = first["plan_hash"]
    _legacyize()
    assert client.get("/api/state").json()["current_rev"] == product

    # 升级重启：init_db 建空表后，启动恢复在事务内从历史审计重建登记
    _restart_app()
    # 触发连接重建（/api/state 访问数据库）
    assert client.get("/api/state").json()["current_rev"] == product

    rec = dbmod.find_rename_record(appmod._conn, rev, plan_hash)
    assert rec is not None
    assert (rec["from_rev"], rec["to_rev"], rec["base_rev"]) == \
        (rev, product, rev)

    audits_before = client.get("/api/audit").json()["records"]
    revs_before = client.get("/api/revisions").json()["revisions"]

    # 客户端用原 rev、原 expected_rev、同一映射重试：回放首次结果
    c2 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": MAPPINGS})
    assert c2.status_code == 200, c2.text
    body = c2.json()
    assert body["replayed"] is True
    assert body["from_rev"] == rev and body["new_rev"] == product
    # 回放首次的变更清单（与首次确认逐键一致）
    assert body["changeset"] == first["changeset"]
    # 不新增版本或审计、不切换当前指针
    assert client.get("/api/audit").json()["records"] == audits_before
    assert client.get("/api/revisions").json()["revisions"] == revs_before
    assert client.get("/api/state").json()["current_rev"] == product


def test_old_schema_volume_upgraded_with_recovery(tmp_path):
    """旧卷连 rename_record 表都没有：init_db 只建空表，恢复负责补登记。"""
    path = str(tmp_path / "old_volume.db")
    conn = dbmod.connect(path)
    dbmod.init_db(conn)
    try:
        rev = service.import_snapshot(
            conn, base_locale="en",
            files=[{"filename": "en.json",
                    "content": json.dumps({"a": "x {@b}", "b": "B"})}],
            parents_form={})["rev"]
        out = service.rename_commit(conn, rev=rev, expected_rev=rev,
                                    mappings=MAPPINGS)
        product = out["new_rev"]
        with dbmod.transaction(conn):
            conn.execute("DROP TABLE rename_record")  # 回退到旧 schema
    finally:
        conn.close()

    # 升级：重新打开 -> init_db 只建空表 -> 恢复补登记
    conn2 = dbmod.connect(path)
    dbmod.init_db(conn2)
    try:
        assert conn2.execute(
            "SELECT COUNT(*) c FROM rename_record").fetchone()["c"] == 0
        report = service.recover_rename_records(conn2)
        assert len(report["recovered"]) == 1 and not report["rejected"]
        rec = report["recovered"][0]
        assert rec["from_rev"] == rev and rec["to_rev"] == product
        # 恢复后过期重试回放成功
        out2 = service.rename_commit(conn2, rev=rev, expected_rev=rev,
                                     mappings=MAPPINGS)
        assert out2["replayed"] is True and out2["new_rev"] == product
    finally:
        conn2.close()


def test_recovery_is_idempotent_across_repeated_upgrades(client):
    """重复升级不得重复登记：第二次恢复全部跳过，记录仍只有一条。"""
    rev, first = _setup_completed_rename(client)
    _legacyize()

    conn = _extra_conn()
    try:
        r1 = service.recover_rename_records(conn)
        assert len(r1["recovered"]) == 1 and not r1["rejected"]
        r2 = service.recover_rename_records(conn)
        assert not r2["recovered"] and not r2["rejected"]
        assert len(r2["skipped_existing"]) == 1
        rows = conn.execute("SELECT * FROM rename_record").fetchall()
        assert len(rows) == 1
        assert rows[0]["to_rev"] == first["new_rev"]
    finally:
        conn.close()


def test_confirm_path_recovers_when_startup_recovery_not_run(client):
    """确认链路：即使启动恢复未运行，提交事务内也会定点恢复登记并回放。"""
    rev, first = _setup_completed_rename(client)
    product = first["new_rev"]
    _legacyize()
    # 不重启（不跑启动恢复）：确认链路在事务内自行恢复
    n_audits = len(client.get("/api/audit").json()["records"])
    n_revs = len(client.get("/api/revisions").json()["revisions"])

    c2 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": MAPPINGS})
    assert c2.status_code == 200, c2.text
    body = c2.json()
    assert body["replayed"] is True and body["new_rev"] == product
    assert body["changeset"] == first["changeset"]
    # 恢复只补登记记录：不新增版本/审计、不切换指针
    assert len(client.get("/api/audit").json()["records"]) == n_audits
    assert len(client.get("/api/revisions").json()["revisions"]) == n_revs
    assert client.get("/api/state").json()["current_rev"] == product
    rec = dbmod.find_rename_record(appmod._conn, rev, first["plan_hash"])
    assert rec is not None and rec["to_rev"] == product

    # 再次重试：走既有记录回放，仍无任何写入
    c3 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": MAPPINGS})
    assert c3.status_code == 200 and c3.json()["replayed"] is True
    assert len(client.get("/api/audit").json()["records"]) == n_audits


def test_stale_request_without_matching_history_still_409(client):
    """未完成的过期请求仍返回 409：没有可匹配的历史审计可恢复。"""
    rev, first = _setup_completed_rename(client)
    _legacyize()
    _restart_app()
    # 触发连接重建（/api/state 访问数据库）
    assert client.get("/api/state").status_code == 200

    # 从未完成过的计划（不同映射）+ 过期令牌：无历史可恢复 -> 409
    r = client.post(f"/api/revisions/{rev}/rename/commit", json={
        "expected_rev": rev,
        "mappings": [{"old_key": "a", "new_key": "ah"}]})
    assert r.status_code == 409
    # 没有留下任何新登记（只有启动恢复重建的那一条）
    conn = _extra_conn()
    try:
        rows = conn.execute("SELECT * FROM rename_record").fetchall()
        assert len(rows) == 1 and rows[0]["to_rev"] == first["new_rev"]
    finally:
        conn.close()


def test_recovers_multiple_completed_plans_from_same_source(client):
    """同一源版本完成过多个不同计划的旧卷：各自独立恢复、各自可回放。"""
    files = [("en.json", json.dumps({"a": "x {@b}", "b": "B"})),
             ("fr.json", json.dumps({"a": "fa {@b}"}))]
    rev = _import(client, files)
    c1 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": MAPPINGS})
    assert c1.status_code == 200
    p1 = c1.json()["new_rev"]
    # 重新导入相同内容把指针切回源版本（导入幂等切换），再完成第二个计划
    assert _import(client, files) == rev
    c2 = client.post(f"/api/revisions/{rev}/rename/commit", json={
        "expected_rev": rev,
        "mappings": [{"old_key": "a", "new_key": "ah"}]})
    assert c2.status_code == 200
    p2 = c2.json()["new_rev"]
    assert p1 != p2
    _legacyize()

    conn = _extra_conn()
    try:
        report = service.recover_rename_records(conn)
        assert len(report["recovered"]) == 2 and not report["rejected"]
        assert conn.execute(
            "SELECT COUNT(*) c FROM rename_record").fetchone()["c"] == 2
    finally:
        conn.close()

    r1 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": MAPPINGS})
    assert r1.status_code == 200 and r1.json()["replayed"] is True
    assert r1.json()["new_rev"] == p1
    r2 = client.post(f"/api/revisions/{rev}/rename/commit", json={
        "expected_rev": rev,
        "mappings": [{"old_key": "a", "new_key": "ah"}]})
    assert r2.status_code == 200 and r2.json()["replayed"] is True
    assert r2.json()["new_rev"] == p2
    # 回放不切换指针（仍停在第二个产物）
    assert client.get("/api/state").json()["current_rev"] == p2


# ---------------------------------------------------------------- 重复审计归并

def test_consistent_duplicate_audits_merge_into_single_record(client):
    """同源同计划、关联一致的重复审计：归并为一条记录，锚定首次审计。"""
    rev, first = _setup_completed_rename(client)
    product = first["new_rev"]
    plan_hash = first["plan_hash"]
    _legacyize()

    conn = _extra_conn()
    try:
        first_audit_id = conn.execute(
            "SELECT id FROM audit WHERE action = 'rename' ORDER BY id"
        ).fetchone()["id"]
        # 旧卷中同一（源版本, 计划摘要）的第二条审计（关联完全一致）
        with dbmod.transaction(conn):
            dbmod.insert_audit(
                conn, actor="user", action="rename", from_rev=rev,
                to_rev=product, base_rev=rev,
                detail={"plan_hash": plan_hash, "mappings": MAPPINGS,
                        "deduplicated": True})
        report = service.recover_rename_records(conn)
        assert len(report["recovered"]) == 1 and not report["rejected"]
        rec = report["recovered"][0]
        assert rec["audit_id"] == first_audit_id  # 归并到首次审计
        assert conn.execute(
            "SELECT COUNT(*) c FROM rename_record").fetchone()["c"] == 1
    finally:
        conn.close()

    c2 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": MAPPINGS})
    assert c2.status_code == 200 and c2.json()["replayed"] is True
    assert c2.json()["new_rev"] == product


def test_conflicting_duplicate_audits_are_rejected(client):
    """同源同计划但目标版本冲突的重复审计：拒绝归并、明确拒绝回放。"""
    rev, first = _setup_completed_rename(client)
    plan_hash = first["plan_hash"]
    _legacyize()
    # 另一个真实存在的版本，作为冲突审计的目标
    other = _import(client, [
        ("en.json", json.dumps({"a": "other {@b}", "b": "B", "z": "z"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])

    conn = _extra_conn()
    try:
        with dbmod.transaction(conn):
            dbmod.insert_audit(
                conn, actor="user", action="rename", from_rev=rev,
                to_rev=other, base_rev=rev,
                detail={"plan_hash": plan_hash, "mappings": MAPPINGS})
        report = service.recover_rename_records(conn)
        assert not report["recovered"]
        assert len(report["rejected"]) == 1
        assert any("不一致" in r for r in report["rejected"][0]["reasons"])
        assert dbmod.find_rename_record(conn, rev, plan_hash) is None
        # 确认链路：明确拒绝回放（完整性故障），而不是当作一般过期 409
        with pytest.raises(service.RenameReplayError, match="拒绝"):
            service.rename_commit(conn, rev=rev, expected_rev=rev,
                                  mappings=MAPPINGS)
        # 拒绝回放不留任何登记
        assert dbmod.find_rename_record(conn, rev, plan_hash) is None
    finally:
        conn.close()


def test_existing_valid_record_is_not_overwritten_by_conflicting_audit(client):
    """已有有效记录时不覆盖：同组冲突审计被跳过，回放仍走既有记录。"""
    rev, first = _setup_completed_rename(client)
    product = first["new_rev"]
    plan_hash = first["plan_hash"]
    other = _import(client, [
        ("en.json", json.dumps({"a": "other {@b}", "b": "B", "z": "z"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])

    conn = _extra_conn()
    try:
        before = dbmod.find_rename_record(conn, rev, plan_hash)
        assert before is not None
        # 注入一条同组但目标不同的冲突审计（记录仍在，恢复不得覆盖）
        with dbmod.transaction(conn):
            dbmod.insert_audit(
                conn, actor="user", action="rename", from_rev=rev,
                to_rev=other, base_rev=rev,
                detail={"plan_hash": plan_hash, "mappings": MAPPINGS})
        report = service.recover_rename_records(conn)
        assert not report["recovered"] and not report["rejected"]
        assert len(report["skipped_existing"]) == 1
        assert dbmod.find_rename_record(conn, rev, plan_hash) == before
    finally:
        conn.close()

    c = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": MAPPINGS})
    assert c.status_code == 200 and c.json()["replayed"] is True
    assert c.json()["new_rev"] == product
    # 回放不切换指针（仍停在 other）
    assert client.get("/api/state").json()["current_rev"] == other


# ---------------------------------------------------------------- 异常关系拒绝

def test_recovery_rejects_missing_product(client):
    """产物版本缺失的异常关系：拒绝恢复与回放，不登记。"""
    rev, first = _setup_completed_rename(client)
    product = first["new_rev"]
    plan_hash = first["plan_hash"]
    _legacyize()
    raw = sqlite3.connect(appmod.DB_PATH)
    try:
        raw.execute("PRAGMA foreign_keys = OFF")
        raw.execute("DELETE FROM revision WHERE rev = ?", (product,))
        raw.commit()
    finally:
        raw.close()

    conn = _extra_conn()
    try:
        report = service.recover_rename_records(conn)
        assert not report["recovered"]
        assert any("产物版本缺失" in r
                   for r in report["rejected"][0]["reasons"])
        with pytest.raises(service.RenameReplayError):
            service.rename_commit(conn, rev=rev, expected_rev=rev,
                                  mappings=MAPPINGS)
        assert dbmod.find_rename_record(conn, rev, plan_hash) is None
    finally:
        conn.close()


def test_recovery_rejects_product_digest_mismatch(client):
    """审计目标指向内容不符的版本：产物摘要核对失败，拒绝恢复。"""
    rev, first = _setup_completed_rename(client)
    plan_hash = first["plan_hash"]
    other = _import(client, [
        ("en.json", json.dumps({"a": "other {@b}", "b": "B", "z": "z"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])
    _legacyize()

    conn = _extra_conn()
    try:
        # 破坏审计关系：to_rev 指向另一个内容不同的既有版本
        with dbmod.transaction(conn):
            conn.execute(
                "UPDATE audit SET to_rev = ? WHERE action = 'rename'",
                (other,))
        report = service.recover_rename_records(conn)
        assert not report["recovered"]
        assert any("产物摘要" in r for r in report["rejected"][0]["reasons"])
        with pytest.raises(service.RenameReplayError):
            service.rename_commit(conn, rev=rev, expected_rev=rev,
                                  mappings=MAPPINGS)
        assert dbmod.find_rename_record(conn, rev, plan_hash) is None
    finally:
        conn.close()


def test_recovery_rejects_source_baseline_mismatch(client):
    """审计源版本与基线不一致的异常关系：拒绝恢复。"""
    rev, first = _setup_completed_rename(client)
    plan_hash = first["plan_hash"]
    _legacyize()

    conn = _extra_conn()
    try:
        with dbmod.transaction(conn):
            conn.execute(
                "UPDATE audit SET from_rev = 'rghost000000'"
                " WHERE action = 'rename'")
        report = service.recover_rename_records(conn)
        assert not report["recovered"]
        assert any("基线" in r for r in report["rejected"][0]["reasons"])
        assert dbmod.find_rename_record(conn, rev, plan_hash) is None
    finally:
        conn.close()


def test_recovery_rejects_plan_hash_mismatching_mappings(client):
    """审计映射重算的计划摘要与审计不符：拒绝恢复。"""
    rev, first = _setup_completed_rename(client)
    plan_hash = first["plan_hash"]
    _legacyize()

    conn = _extra_conn()
    try:
        # detail 里 mappings 被换成另一个计划，但 plan_hash 仍是原值
        bad_detail = {"plan_hash": plan_hash,
                      "mappings": [{"old_key": "a", "new_key": "ah"}]}
        with dbmod.transaction(conn):
            conn.execute(
                "UPDATE audit SET detail_json = ? WHERE action = 'rename'",
                (json.dumps(bad_detail, ensure_ascii=False, sort_keys=True),))
        report = service.recover_rename_records(conn)
        assert not report["recovered"]
        assert any("计划摘要" in r for r in report["rejected"][0]["reasons"])
        assert dbmod.find_rename_record(conn, rev, plan_hash) is None
    finally:
        conn.close()


def test_recovery_rejects_audit_without_plan_hash(client):
    """缺少计划摘要的残缺审计：启动恢复拒绝；确认链路无匹配审计仍 409。"""
    rev, first = _setup_completed_rename(client)
    _legacyize()

    conn = _extra_conn()
    try:
        with dbmod.transaction(conn):
            conn.execute(
                "UPDATE audit SET detail_json = ? WHERE action = 'rename'",
                (json.dumps({"mappings": MAPPINGS}, ensure_ascii=False),))
        report = service.recover_rename_records(conn)
        assert not report["recovered"]
        assert any("计划摘要" in r for r in report["rejected"][0]["reasons"])
        # 确认链路：没有可匹配的历史审计，按一般过期请求处理 -> 409
        with pytest.raises(service.ConflictError):
            service.rename_commit(conn, rev=rev, expected_rev=rev,
                                  mappings=MAPPINGS)
    finally:
        conn.close()


# ---------------------------------------------------------------- 并发与原子性

def test_concurrent_retries_on_legacy_volume_register_once(client):
    """旧卷上的并发重试：恢复登记只发生一次，两个客户端都拿到回放。"""
    rev, first = _setup_completed_rename(client)
    product = first["new_rev"]
    _legacyize()

    conn_a, conn_b = _extra_conn(), _extra_conn()
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
                conn, rev=rev, expected_rev=rev, mappings=MAPPINGS)
            outcomes[tag] = ("ok", out["new_rev"], out.get("replayed"))
        except service.ConflictError:
            outcomes[tag] = ("conflict", None, None)
        except Exception as e:  # noqa: BLE001 - 记录异常以便断言
            outcomes[tag] = ("error", repr(e), None)

    t_a = threading.Thread(target=do, args=("a", conn_a))
    t_b = threading.Thread(target=do, args=("b", conn_b))
    try:
        t_a.start()
        assert entered.wait(5)
        t_b.start()
        time.sleep(0.5)
        gate.set()
        t_a.join(5)
        t_b.join(5)
    finally:
        faults.reset()
        conn_a.close()
        conn_b.close()

    # 两个并发重试都回放成功（写锁内串行：先到者恢复登记，后到者命中记录）
    assert {v[0] for v in outcomes.values()} == {"ok"}, outcomes
    assert all(v[1] == product and v[2] for v in outcomes.values())

    watcher = _extra_conn()
    try:
        rows = watcher.execute("SELECT * FROM rename_record").fetchall()
        assert len(rows) == 1 and rows[0]["to_rev"] == product
        # 不追加审计、不新建版本
        rename_audits = [a for a in dbmod.list_audit(watcher, 500)
                         if a["action"] == "rename"]
        assert len(rename_audits) == 1
        assert dbmod.get_current_rev(watcher) == product
    finally:
        watcher.close()


def test_recovery_registration_rolls_back_with_transaction(client):
    """恢复登记与确认共享事务：故障注入导致回滚后不留记录，可干净重试。"""
    rev, first = _setup_completed_rename(client)
    plan_hash = first["plan_hash"]
    _legacyize()

    conn = _extra_conn()
    try:
        faults.fail_once("rename.recover.after_insert_record")
        with pytest.raises(RuntimeError):
            service.rename_commit(conn, rev=rev, expected_rev=rev,
                                  mappings=MAPPINGS)
        # 回滚：没有留下登记记录
        assert dbmod.find_rename_record(conn, rev, plan_hash) is None
        # 故障清除后重试：恢复 + 回放成功
        out = service.rename_commit(conn, rev=rev, expected_rev=rev,
                                    mappings=MAPPINGS)
        assert out["replayed"] is True and out["new_rev"] == first["new_rev"]
        assert dbmod.find_rename_record(conn, rev, plan_hash) is not None
    finally:
        conn.close()
