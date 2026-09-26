"""资源键重命名迁移：试运行校验、引用改写、确认原子性与幂等。"""
import json
import os
import sqlite3
import tempfile
import threading
import time

import pytest

_tmp = tempfile.mkdtemp()

from fastapi.testclient import TestClient  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import service  # noqa: E402
from app import rename as renamemod  # noqa: E402
from app import main as appmod  # noqa: E402
from app.faults import faults  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    """每个用例独立的 SQLite 文件：避免共享库里的版本/审计/指针相互污染。"""
    db_path = str(tmp_path / "test_rename.db")
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
    return dbmod.connect(appmod.DB_PATH)


def _import(client, files, parents=None):
    body = {"base_locale": "en",
            "files": [{"filename": name, "content": content}
                      for name, content in files],
            "parents": parents or {"fr": "en", "de": "en", "ja": "en"}}
    r = client.post("/api/import-json", json=body)
    assert r.status_code == 200, r.text
    return r.json()["rev"]


def _sample_rev(client):
    base = os.path.join(os.path.dirname(__file__), "..", "samples")
    files = []
    for name in ["en.json", "fr.json", "de.json", "ja.json"]:
        with open(os.path.join(base, name), encoding="utf-8") as f:
            files.append((name, f.read()))
    return _import(client, files)


# ---------------------------------------------------------------- 校验

def test_validation_all_error_classes(client):
    rev = _sample_rev(client)
    # 单请求聚合返回全部错误类别（稳定排序）
    r = client.post(f"/api/revisions/{rev}/rename/plan", json={"mappings": [
        {"old_key": "appTitle", "new_key": "appTitle"},       # SELF_RENAME
        {"old_key": "no.such.key", "new_key": "x"},           # SOURCE_NOT_FOUND
        {"old_key": "appTitle", "new_key": "dup"},            # DUPLICATE_SOURCE
        {"old_key": "refA", "new_key": "dup"},                # DUPLICATE_TARGET
        {"old_key": "refB", "new_key": "price"},              # TARGET_CONFLICT（price 非迁移源）
        {"old_key": "refA", "new_key": "greeting"},           # CHAIN (refA→greeting→good.key)
        {"old_key": "greeting", "new_key": "good.key"},
        {"old_key": "ja", "new_key": "bad key!"},             # INVALID_KEY_CHAR（ja 不存在）
    ]})
    assert r.status_code == 400
    codes = {e["code"] for e in r.json()["detail"]["errors"]}
    assert {"SELF_RENAME", "SOURCE_NOT_FOUND", "DUPLICATE_SOURCE",
            "DUPLICATE_TARGET", "TARGET_CONFLICT", "CHAIN_AMBIGUOUS",
            "INVALID_KEY_CHAR"} <= codes
    # 源键缺失与非法字符会在同一条映射上聚合报告
    src = [e for e in r.json()["detail"]["errors"]
           if e["code"] == "SOURCE_NOT_FOUND" and e["old_key"] == "ja"]
    assert src and any(e["code"] == "INVALID_KEY_CHAR" and e["old_key"] == "ja"
                       for e in r.json()["detail"]["errors"])


def test_empty_plan_and_bad_mapping(client):
    rev = _sample_rev(client)
    r = client.post(f"/api/revisions/{rev}/rename/plan", json={"mappings": []})
    assert r.status_code == 400
    assert r.json()["detail"]["errors"][0]["code"] == "EMPTY_PLAN"
    r = client.post(f"/api/revisions/{rev}/rename/plan",
                    json={"mappings": [{"old_key": "a"}]})
    assert r.status_code == 422  # pydantic 入参校验


def test_swap_two_keys_is_allowed(client):
    """a->b 与 b->a 若二者都是源键：new_key 都在 old 集合中，不是目标冲突；
    但构成链式歧义（环），必须拒绝。"""
    rev = _import(client, [
        ("en.json", json.dumps({"a": "{@b}", "b": "{@a}"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])
    r = client.post(f"/api/revisions/{rev}/rename/plan", json={"mappings": [
        {"old_key": "a", "new_key": "b"}, {"old_key": "b", "new_key": "a"}]})
    assert r.status_code == 400
    assert {e["code"] for e in r.json()["detail"]["errors"]} == {"CHAIN_AMBIGUOUS"}


# ---------------------------------------------------------------- 试运行

def test_dry_run_renames_keys_and_rewrites_refs_along_chains(client):
    rev = _sample_rev(client)
    r = client.post(f"/api/revisions/{rev}/rename/plan", json={"mappings": [
        {"old_key": "appTitle", "new_key": "app.title"},
        {"old_key": "refA", "new_key": "ref.a"},
        {"old_key": "refB", "new_key": "ref.b"},
    ]})
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["rev"] == rev and p["revision"]["is_current"] is True

    # 键改名条目按 old_key 排序
    assert [k["old_key"] for k in p["key_renames"]] == \
        ["appTitle", "refA", "refB"]
    appt = p["key_renames"][0]
    assert appt["locales"] == ["de", "en", "fr", "ja"]
    # appTitle 在全部语言都有；refB 仅 en/ja 有键（ja 通过引用但条目在 en）
    refb = next(k for k in p["key_renames"] if k["old_key"] == "refB")
    assert refb["locales"] == ["en"]
    assert "ja" in refb["absent_locales"]

    # 引用改写：fr.refA {@appTitle} 按 fr 链解析到 fr.appTitle（遮蔽）
    fr_refa = next(x for x in p["reference_rewrites"]
                   if x["locale"] == "fr" and x["key"] == "refA")
    assert fr_refa["old_key"] == "appTitle" and fr_refa["new_key"] == "app.title"
    assert fr_refa["resolved_locale"] == "fr"

    # de.refC {@refA}：de 无 refA，沿 de->en 落到 en.refA
    de_refc = next(x for x in p["reference_rewrites"]
                   if x["locale"] == "de" and x["key"] == "refC")
    assert de_refc["resolved_locale"] == "en"
    assert (de_refc["old_key"], de_refc["new_key"]) == ("refA", "ref.a")

    # ja.jaGuide {@refB}：沿 ja->en 落 en.refB
    ja_guide = next(x for x in p["reference_rewrites"]
                    if x["locale"] == "ja" and x["key"] == "jaGuide")
    assert ja_guide["resolved_locale"] == "en"
    assert (ja_guide["old_key"], ja_guide["new_key"]) == ("refB", "ref.b")

    # 稳定排序：(语言, 键, 位置)
    order = [(x["locale"], x["key"], x["pos"]) for x in p["reference_rewrites"]]
    assert order == sorted(order)

    # ja.brokenRefDemo 原有断裂引用作为风险（does.not.exist 不在迁移集合）
    kinds = {(x["locale"], x["key"], x["kind"]) for x in p["risks"]["items"]}
    assert ("ja", "brokenRefDemo", "BROKEN_REFERENCE") in kinds
    assert p["risks"]["summary"]["NEW_BROKEN_REFERENCE"] == 0
    assert p["summary"]["broken_after"] == p["summary"]["broken_before"]

    # 试运行不写库：当前版本未变、审计无 rename
    assert client.get("/api/state").json()["current_rev"] == rev
    assert not [a for a in client.get("/api/audit").json()["records"]
                if a["action"] == "rename"]


def test_dry_run_deterministic_plan_hash_regardless_of_order(client):
    rev = _sample_rev(client)
    body_a = {"mappings": [{"old_key": "refA", "new_key": "ref.a"},
                           {"old_key": "appTitle", "new_key": "app.title"}]}
    body_b = {"mappings": list(reversed(body_a["mappings"]))}
    ha = client.post(f"/api/revisions/{rev}/rename/plan", json=body_a).json()["plan_hash"]
    hb = client.post(f"/api/revisions/{rev}/rename/plan", json=body_b).json()["plan_hash"]
    assert ha == hb


def test_exact_duplicate_pair_is_deduped(client):
    rev = _sample_rev(client)
    r = client.post(f"/api/revisions/{rev}/rename/plan", json={"mappings": [
        {"old_key": "refA", "new_key": "ref.a"},
        {"old_key": "refA", "new_key": "ref.a"},
    ]})
    assert r.status_code == 200
    assert r.json()["summary"]["mapping_count"] == 1


# ---------------------------------------------------------------- 确认

def test_commit_writes_new_revision_and_rewrites(client):
    rev = _sample_rev(client)
    mappings = [{"old_key": "appTitle", "new_key": "app.title"},
                {"old_key": "refA", "new_key": "ref.a"}]
    plan = client.post(f"/api/revisions/{rev}/rename/plan",
                       json={"mappings": mappings}).json()

    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": mappings})
    assert r.status_code == 200, r.text
    out = r.json()
    new_rev = out["new_rev"]
    assert new_rev != rev and out["deduplicated"] is False
    assert client.get("/api/state").json()["current_rev"] == new_rev

    en = client.get(f"/api/revisions/{new_rev}/entries/en").json()["entries"]
    assert "appTitle" not in en and en["app.title"] == "Contract Reviewer"
    assert en["ref.a"] == "Tip: {@app.title} keeps every contract safe"
    # refB 不在本次迁移集合中，保持原名；其引用 refA 被改写为 {@ref.a}
    assert "refB" in en and en["refB"] == "Reminder — {@ref.a}"

    fr = client.get(f"/api/revisions/{new_rev}/entries/fr").json()["entries"]
    assert fr["ref.a"].startswith("Astuce : {@app.title}")

    # 旧版本不可变
    old_en = client.get(f"/api/revisions/{rev}/entries/en").json()["entries"]
    assert "appTitle" in old_en and "refA" in old_en

    # 审计
    aud = client.get("/api/audit").json()["records"]
    rec = next(a for a in aud if a["action"] == "rename")
    assert rec["from_rev"] == rev and rec["to_rev"] == new_rev
    assert rec["base_rev"] == rev
    assert rec["detail"]["plan_hash"] == plan["plan_hash"]

    # 变更清单
    keys = {(c["old_key"], c["new_key"]) for c in out["changeset"]["keys"]}
    assert keys == {("appTitle", "app.title"), ("refA", "ref.a")}
    changed_msgs = {(c["locale"], c["key"]) for c in out["changeset"]["values"]}
    assert ("en", "refA") in changed_msgs
    assert ("de", "refC") in changed_msgs

    # 新版本没有引入新问题（断裂引用数与旧版一致，改名后引用仍可解析）
    iss = client.get(f"/api/revisions/{new_rev}/issues").json()
    assert not any(i["code"] == "BROKEN_REFERENCE"
                   and i["key"] in ("ref.a", "refB", "refC", "jaGuide")
                   for i in iss["issues"])


def test_commit_conflict_on_stale_expected_rev(client):
    rev = _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])
    mappings = [{"old_key": "b", "new_key": "bee"}]
    # 先用 merge 产生分叉，使 current != rev
    m = client.post("/api/merge", json={"base_rev": rev, "fixes": [
        {"op": "upsert", "locale": "fr", "key": "a", "value": "fa2"}]})
    assert m.status_code == 200
    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": mappings})
    assert r.status_code == 409
    # 指针没有被陈旧提交改动（仍是 merge 产物）
    assert client.get("/api/state").json()["current_rev"] == m.json()["rev"]


def test_commit_idempotent_same_plan_twice(client):
    """首次确认后用原 rev、原 expected_rev、同一映射重试：

    current_rev 已推进到产物版本（令牌过期），接口必须在返回 409 前按
    （源版本, 规范化计划摘要）识别出已完成迁移，在事务内核对产物与审计
    关系后回放首次结果——200、replayed=true，不新建版本、不追加审计、
    不切换当前指针。
    """
    rev = _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])
    mappings = [{"old_key": "b", "new_key": "bee"}]

    c1 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": mappings})
    assert c1.status_code == 200 and c1.json()["replayed"] is False
    assert c1.json()["deduplicated"] is False
    result_rev = c1.json()["new_rev"]
    assert client.get("/api/state").json()["current_rev"] == result_rev

    def rename_audits():
        return [a for a in client.get("/api/audit").json()["records"]
                if a["action"] == "rename"]

    n_after_first = len(rename_audits())
    revs_after_first = {r["rev"] for r in
                        client.get("/api/revisions").json()["revisions"]}

    # 缺陷回归：旧令牌 + 同一映射重试，曾在查找既有产物前直接 409
    c2 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": mappings})
    assert c2.status_code == 200, c2.text
    body = c2.json()
    assert body["replayed"] is True and body["deduplicated"] is True
    # 回放相同的新旧版本
    assert body["from_rev"] == rev and body["new_rev"] == result_rev
    # 不新建版本、不追加审计、不切换当前指针
    assert client.get("/api/state").json()["current_rev"] == result_rev
    assert len(rename_audits()) == n_after_first
    assert {r["rev"] for r in
            client.get("/api/revisions").json()["revisions"]} == \
        revs_after_first
    # 回放相同的变更清单（键改名 + 值改写 from/to）
    assert {(c["old_key"], c["new_key"]) for c in
            body["changeset"]["keys"]} == {("b", "bee")}
    value = next(c for c in body["changeset"]["values"]
                 if c["locale"] == "en" and c["key"] == "a")
    assert value["from"] == "x {@b}" and value["to"] == "x {@bee}"
    assert body["changeset"] == c1.json()["changeset"]

    # 再重试一次，仍然只是回放
    c3 = client.post(f"/api/revisions/{rev}/rename/commit",
                     json={"expected_rev": rev, "mappings": mappings})
    assert c3.status_code == 200 and c3.json()["replayed"] is True
    assert len(rename_audits()) == n_after_first
    assert client.get("/api/state").json()["current_rev"] == result_rev

    # 映射提交顺序不同但集合相同（规范化计划摘要一致）：先提交一次双键迁移，
    # 再以反序的同一映射集合 + 旧令牌重试，仍命中同一完成记录回放。
    pair_ab = [{"old_key": "a", "new_key": "ah"},
               {"old_key": "bee", "new_key": "bee2"}]
    pair_ba = list(reversed(pair_ab))
    pa = client.post(f"/api/revisions/{result_rev}/rename/plan",
                     json={"mappings": pair_ab}).json()
    pb = client.post(f"/api/revisions/{result_rev}/rename/plan",
                     json={"mappings": pair_ba}).json()
    assert pa["plan_hash"] == pb["plan_hash"]
    c4 = client.post(f"/api/revisions/{result_rev}/rename/commit",
                     json={"expected_rev": result_rev, "mappings": pair_ab})
    assert c4.status_code == 200 and c4.json()["replayed"] is False
    second_rev = c4.json()["new_rev"]
    n_after_second = len(rename_audits())
    c5 = client.post(f"/api/revisions/{result_rev}/rename/commit",
                     json={"expected_rev": result_rev, "mappings": pair_ba})
    assert c5.status_code == 200
    assert c5.json()["replayed"] is True
    assert c5.json()["new_rev"] == second_rev
    assert len(rename_audits()) == n_after_second


def test_replay_returns_200_even_when_pointer_moved_elsewhere(client):
    """回放不得切换当前指针：指针随后被其他操作移走时，重试仍 200 且指针不变。"""
    rev = _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])
    mappings = [{"old_key": "b", "new_key": "bee"}]
    result_rev = client.post(
        f"/api/revisions/{rev}/rename/commit",
        json={"expected_rev": rev, "mappings": mappings}).json()["new_rev"]

    # 以产物版本为基线再做一次 merge，把指针推进到与回放无关的新版本
    after = client.post("/api/merge", json={"base_rev": result_rev, "fixes": [
        {"op": "upsert", "locale": "fr", "key": "a", "value": "fa2"}]}).json()
    other_rev = after["rev"]
    assert client.get("/api/state").json()["current_rev"] == other_rev

    n_audits = len([a for a in client.get("/api/audit").json()["records"]])
    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": mappings})
    assert r.status_code == 200
    body = r.json()
    assert body["replayed"] is True and body["new_rev"] == result_rev
    # 指针没有被回放拉回到产物版本
    assert client.get("/api/state").json()["current_rev"] == other_rev
    assert len(client.get("/api/audit").json()["records"]) == n_audits


def test_stale_request_without_completed_record_still_conflicts(client):
    """尚无成功记录的过期请求仍返回 409，且不产生任何落库。"""
    rev = _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])
    other = _import(client, [
        ("en.json", json.dumps({"a": "other {@b}", "b": "B", "z": "z"})),
        ("fr.json", json.dumps({"a": "fa"})),
    ])
    # 另一个导入把指针切走（同内容导入会幂等，故用不同内容的新基线）
    assert client.get("/api/state").json()["current_rev"] == other

    mappings = [{"old_key": "b", "new_key": "bee"}]
    revs_before = {r["rev"] for r in
                   client.get("/api/revisions").json()["revisions"]}
    r = client.post(f"/api/revisions/{rev}/rename/commit",
                    json={"expected_rev": rev, "mappings": mappings})
    assert r.status_code == 409
    assert client.get("/api/state").json()["current_rev"] == other
    assert {x["rev"] for x in
            client.get("/api/revisions").json()["revisions"]} == revs_before
    assert not [a for a in client.get("/api/audit").json()["records"]
                if a["action"] == "rename"]


@pytest.mark.parametrize("round_no", range(3))
def test_concurrent_duplicate_rename_only_one_takes_effect(client, round_no):
    """并发重复提交同一（源版本, 计划摘要）：只有一次真正生效。

    先到者提交新版本 + 审计 + 完成记录；后到者在写锁内串行后命中完成记录
    并回放（或在新鲜性复核处 409——两种顺序都合法），但无论如何：
    产物版本只有一个、rename 审计只有一条、当前指针只切换一次。
    """
    rev = _import(client, [
        ("en.json", json.dumps({"a": "base {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "old"})),
    ])
    mappings = [{"old_key": "b", "new_key": "bee"}]
    conn_a, conn_b = _extra_conn(client), _extra_conn(client)
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
        except service.ConflictError:
            outcomes[tag] = ("conflict", None, False)
        except Exception as e:
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
    assert {v[0] for v in outcomes.values()} <= {"ok", "conflict"}, outcomes
    ok = [v for v in outcomes.values() if v[0] == "ok"]
    assert len(ok) >= 1
    result_rev = ok[0][1]
    assert all(v[1] in (None, result_rev) for v in outcomes.values())
    # 若后到者也拿到 200，必须是回放而不是第二次提交
    assert all(v[0] != "ok" or v[1] == result_rev for v in outcomes.values())

    watcher = _extra_conn(client)
    try:
        assert dbmod.get_current_rev(watcher) == result_rev
        rename_records = watcher.execute(
            "SELECT * FROM rename_record WHERE base_rev = ?",
            (rev,)).fetchall()
        assert len(rename_records) == 1
        assert rename_records[0]["to_rev"] == result_rev
        rename_audits = [a for a in dbmod.list_audit(watcher, 500)
                        if a["action"] == "rename" and a["base_rev"] == rev]
        assert len(rename_audits) == 1
        assert rename_audits[0]["to_rev"] == result_rev
        assert rename_audits[0]["detail"]["deduplicated"] is False
        # 产物版本全局唯一（内容哈希唯一）
        metas = [r for r in dbmod.list_revisions(watcher)
                 if r["rev"] == result_rev]
        assert len(metas) == 1
    finally:
        watcher.close()
    assert client.get("/api/state").json()["current_rev"] == result_rev


def test_replay_after_content_hash_dedup_first_completion(client):
    """首次完成命中内容哈希去重（审计 deduplicated=True）后，过期重试仍可回放。

    两个不同基线以不同源键迁移到**同一产物快照**：b→bee 先建版本；b2→bee
    的产物逐字节相同，首次完成走内容哈希去重（补审计 + 完成记录，
    deduplicated=True）。之后再以原 rev_q 令牌重试必须回放成功，而不是因
    审计带 deduplicated=True 而被核对拒绝。
    """
    rev_p = _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"}))],
        parents={})
    rev_q = _import(client, [
        ("en.json", json.dumps({"a": "x {@b2}", "b2": "B"}))],
        parents={})
    assert rev_p != rev_q
    # 当前停在 rev_q：先把它迁移成产物 rev_a（新版本）
    first_q = client.post(f"/api/revisions/{rev_q}/rename/commit",
                          json={"expected_rev": rev_q,
                                "mappings": [{"old_key": "b2",
                                              "new_key": "bee"}]})
    assert first_q.status_code == 200
    rev_a = first_q.json()["new_rev"]
    assert first_q.json()["deduplicated"] is False
    assert client.get("/api/state").json()["current_rev"] == rev_a

    # 回到旧基线 rev_p（独立连接切指针，模拟用户切回旧版本重新执行同一
    # 内容计划）：令牌新鲜、产物哈希已存在 -> 内容去重首次完成（dedup 审计）
    reset = _extra_conn(client)
    try:
        with dbmod.transaction(reset):
            dbmod.set_current_rev(reset, rev_p)
    finally:
        reset.close()

    first_p = client.post(f"/api/revisions/{rev_p}/rename/commit",
                          json={"expected_rev": rev_p,
                                "mappings": [{"old_key": "b",
                                              "new_key": "bee"}]})
    assert first_p.status_code == 200
    f = first_p.json()
    assert f["new_rev"] == rev_a and f["deduplicated"] is True
    assert f["replayed"] is False
    assert client.get("/api/state").json()["current_rev"] == rev_a
    n_audits = len(client.get("/api/audit").json()["records"])
    n_revs = len(client.get("/api/revisions").json()["revisions"])

    # 令牌已过期（current=rev_a）但完成记录存在：回放成功
    again = client.post(f"/api/revisions/{rev_p}/rename/commit",
                        json={"expected_rev": rev_p,
                              "mappings": [{"old_key": "b",
                                            "new_key": "bee"}]})
    assert again.status_code == 200, again.text
    a = again.json()
    assert a["replayed"] is True and a["new_rev"] == rev_a
    assert a["from_rev"] == rev_p
    assert a["changeset"]["value_change_count"] >= 1
    # 无任何写入
    assert client.get("/api/state").json()["current_rev"] == rev_a
    assert len(client.get("/api/audit").json()["records"]) == n_audits
    assert len(client.get("/api/revisions").json()["revisions"]) == n_revs


def test_rolled_back_transaction_not_replayed(client):
    """失败事务（故障注入）整体回滚后，完成记录不得存在：

    此时再以过期令牌重试（指针已被另一操作推进），没有成功记录可命中，
    必须返回 409，而不是把失败事务误认作已完成。
    """
    rev = _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "fa {@b}"})),
    ])
    mappings = [{"old_key": "b", "new_key": "bee"}]
    conn = _extra_conn(client)
    try:
        faults.fail_once("rename.after_insert_record")
        with pytest.raises(RuntimeError):
            service.rename_commit(
                conn, rev=rev, expected_rev=rev, mappings=mappings)

        # 全部回滚：指针、完成记录、rename 审计、产物版本都不存在
        assert dbmod.get_current_rev(conn) == rev
        assert dbmod.find_rename_record(conn, rev, renamemod.plan_hash(
            mappings)) is None
        assert not [a for a in dbmod.list_audit(conn, 500)
                    if a["action"] == "rename"]
    finally:
        conn.close()

    # 通过 merge 产生分叉使令牌过期
    m = client.post("/api/merge", json={"base_rev": rev, "fixes": [
        {"op": "upsert", "locale": "fr", "key": "a", "value": "fa2"}]})
    assert m.status_code == 200
    stale = client.post(f"/api/revisions/{rev}/rename/commit",
                        json={"expected_rev": rev, "mappings": mappings})
    assert stale.status_code == 409

    # 故障清除后以新当前版本为基线重新提交才成功（一次生效）
    out = service.rename_commit(
        _extra_conn(client), rev=m.json()["rev"], expected_rev=m.json()["rev"],
        mappings=mappings)
    assert out["replayed"] is False
    assert client.get("/api/state").json()["current_rev"] == out["new_rev"]


def test_replay_rejects_inconsistent_product_or_audit():
    """事务内核对：产物或审计关系被破坏时不得当作已完成回放。

    使用独立数据库文件（不污染模块共享库）；用关闭外键的裸连接破坏审计关系。
    """
    path = os.path.join(_tmp, f"integrity_{os.getpid()}_{time.time_ns()}.db")
    iconn = dbmod.connect(path)
    dbmod.init_db(iconn)
    try:
        rev = service.import_snapshot(
            iconn, base_locale="en",
            files=[{"filename": "en.json",
                    "content": json.dumps({"a": "x {@b}", "b": "B"})}],
            parents_form={})["rev"]
        mappings = [{"old_key": "b", "new_key": "bee"}]
        out = service.rename_commit(
            iconn, rev=rev, expected_rev=rev, mappings=mappings)
        result_rev = out["new_rev"]

        raw = sqlite3.connect(path)
        try:
            raw.execute("PRAGMA foreign_keys = OFF")
            raw.execute("DELETE FROM audit WHERE action = 'rename'")
            raw.commit()
        finally:
            raw.close()

        # 缺失审计由显式核对拦截：拒绝把残缺关系当作已完成回放
        with pytest.raises(service.RenameReplayError, match="审计记录缺失"):
            service.rename_commit(
                iconn, rev=rev, expected_rev=rev, mappings=mappings)

        # 指针与产物未受影响；破坏事务回滚后仍可按（未破坏的）其他键正常工作
        assert dbmod.get_current_rev(iconn) == result_rev
        assert dbmod.load_revision(iconn, result_rev) is not None

        # 恢复审计后（模拟关系修复）：回放重新可用且不写任何东西
        rec = dbmod.find_rename_record(iconn, rev,
                                        renamemod.plan_hash(mappings))
        with dbmod.transaction(iconn):
            audit_id = dbmod.insert_audit(
                iconn, actor="repair", action="rename", from_rev=rev,
                to_rev=result_rev, base_rev=rev,
                detail={"plan_hash": rec["plan_hash"],
                        "deduplicated": False})
            iconn.execute("UPDATE rename_record SET audit_id = ?"
                          " WHERE base_rev = ? AND plan_hash = ?",
                          (audit_id, rev, rec["plan_hash"]))
        again = service.rename_commit(
            iconn, rev=rev, expected_rev=rev, mappings=mappings)
        assert again["replayed"] is True and again["new_rev"] == result_rev
    finally:
        iconn.close()


def test_invalid_mappings_commit_atomic_no_state_change(client):
    rev = _sample_rev(client)
    r = client.post(f"/api/revisions/{rev}/rename/commit", json={
        "expected_rev": rev,
        "mappings": [{"old_key": "missing", "new_key": "x"}]})
    assert r.status_code == 400
    assert client.get("/api/state").json()["current_rev"] == rev


# ---------------------------------------------------------------- 风险

def test_broken_ref_to_renamed_key_is_risk_not_rewritten(client):
    rev = _import(client, [
        ("en.json", json.dumps({"a": "see {@ghost}", "ghost": "G"})),
    ], parents={})
    # {@ghost} 在 en 本语言可解析 -> 无断裂
    ok = client.post(f"/api/revisions/{rev}/rename/plan",
                     json={"mappings": [{"old_key": "ghost", "new_key": "ghost2"}]})
    assert ok.status_code == 200
    assert not [x for x in ok.json()["risks"]["items"]
                if x["kind"] == "BROKEN_REFERENCE"]

    rev2 = _import(client, [
        ("en.json", json.dumps({"a": "see {@nowhere}"})),
    ], parents={})
    bad = client.post(f"/api/revisions/{rev2}/rename/plan",
                      json={"mappings": [{"old_key": "a", "new_key": "a2"}]})
    risks = bad.json()["risks"]["items"]
    assert len(risks) == 1 and risks[0]["kind"] == "BROKEN_REFERENCE"
    # 确认后断裂原文保留（没有任何 {@} 改写）
    c = client.post(f"/api/revisions/{rev2}/rename/commit",
                    json={"expected_rev": rev2,
                          "mappings": [{"old_key": "a", "new_key": "a2"}]})
    new_rev = c.json()["new_rev"]
    val = client.get(f"/api/revisions/{new_rev}/entries/en").json()["entries"]["a2"]
    assert val == "see {@nowhere}"


def test_syntax_error_reference_is_unparseable_risk(client):
    rev = _import(client, [
        ("en.json", json.dumps({"a": "tip: {@oldName} trailing {",
                                "oldName": "OLD"})),
        ("fr.json", json.dumps({"a": "ok"})),
    ])
    r = client.post(f"/api/revisions/{rev}/rename/plan", json={"mappings": [
        {"old_key": "oldName", "new_key": "new.name"}]})
    assert r.status_code == 200
    risks = [x for x in r.json()["risks"]["items"]
             if x["kind"] == "UNPARSEABLE_REFERENCE"]
    assert len(risks) == 1
    assert risks[0]["locale"] == "en" and risks[0]["key"] == "a"
    # 确认后该语法错误消息保持原样
    c = client.post(f"/api/revisions/{rev}/rename/commit", json={
        "expected_rev": rev,
        "mappings": [{"old_key": "oldName", "new_key": "new.name"}]})
    new_rev = c.json()["new_rev"]
    en = client.get(f"/api/revisions/{new_rev}/entries/en").json()["entries"]
    assert "oldName" not in en and en["new.name"] == "OLD"
    assert en["a"] == "tip: {@oldName} trailing {"


# ---------------------------------------------------------------- 原子性

def test_faults_roll_back_rename_completely(client):
    rev = _import(client, [
        ("en.json", json.dumps({"a": "x {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "fa {@b}"})),
    ])
    mappings = [{"old_key": "b", "new_key": "bee"}]
    conn = _extra_conn(client)
    try:
        for point in ("rename.after_insert_revision",
                      "rename.after_insert_audit",
                      "rename.after_set_current"):
            faults.fail_once(point)
            with pytest.raises(RuntimeError):
                service.rename_commit(
                    conn, rev=rev, expected_rev=rev, mappings=mappings)
            assert dbmod.get_current_rev(conn) == rev
            assert not [a for a in dbmod.list_audit(conn, 500)
                        if a["action"] == "rename"]
    finally:
        conn.close()
    # 故障清除后成功
    out = service.rename_commit(
        _extra_conn(client), rev=rev, expected_rev=rev, mappings=mappings)
    assert client.get("/api/state").json()["current_rev"] == out["new_rev"]


def test_concurrent_rename_same_base_only_one_wins(client):
    rev = _import(client, [
        ("en.json", json.dumps({"a": "base {@b}", "b": "B"})),
        ("fr.json", json.dumps({"a": "old"})),
    ])
    mappings = [{"old_key": "b", "new_key": "bee"}]
    conn_a, conn_b = _extra_conn(client), _extra_conn(client)
    entered = threading.Event()
    gate = threading.Event()

    def hook(conn, **kw):
        if conn is conn_a:
            entered.set()
            gate.wait(10)

    faults.set("rename.before_recheck", hook)
    outcomes = {}

    def do(tag, conn, new_name):
        try:
            out = service.rename_commit(
                conn, rev=rev, expected_rev=rev,
                mappings=[{"old_key": "b", "new_key": new_name}])
            outcomes[tag] = ("ok", out["new_rev"])
        except service.ConflictError:
            outcomes[tag] = ("conflict", None)
        except Exception as e:
            outcomes[tag] = ("error", repr(e))

    t_a = threading.Thread(target=do, args=("a", conn_a, "bee"))
    t_b = threading.Thread(target=do, args=("b", conn_b, "bee2"))
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

    assert set(outcomes) == {"a", "b"}
    assert {v[0] for v in outcomes.values()} == {"ok", "conflict"}, outcomes
    winner = outcomes["a"][1] or outcomes["b"][1]
    assert client.get("/api/state").json()["current_rev"] == winner


def test_plan_hash_is_stable_and_changes_with_plan(client):
    rev = _sample_rev(client)
    p1 = client.post(f"/api/revisions/{rev}/rename/plan", json={"mappings": [
        {"old_key": "refA", "new_key": "ref.a"}]}).json()
    p2 = client.post(f"/api/revisions/{rev}/rename/plan", json={"mappings": [
        {"old_key": "refA", "new_key": "ref.a"}]}).json()
    p3 = client.post(f"/api/revisions/{rev}/rename/plan", json={"mappings": [
        {"old_key": "refA", "new_key": "refA2"}]}).json()
    assert p1["plan_hash"] == p2["plan_hash"]
    assert p1["plan_hash"] != p3["plan_hash"]
    assert len(p1["plan_hash"]) == 16
