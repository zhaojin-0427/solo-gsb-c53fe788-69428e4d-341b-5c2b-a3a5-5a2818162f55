"""引用影响分析：图构建、传递影响、SCC、证据路径与不落库预演。"""
import json
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["LRC_DB_PATH"] = os.path.join(_tmp, f"test_impact_{os.getpid()}.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _import(client, bundles, parents, base="en"):
    files = [{"filename": f"{loc}.json", "content": json.dumps(entries, ensure_ascii=False)}
             for loc, entries in sorted(bundles.items())]
    r = client.post("/api/import-json", json={
        "base_locale": base, "files": files, "parents": parents})
    assert r.status_code == 200, r.text
    return r.json()["rev"]


@pytest.fixture(scope="module")
def chain_rev(client):
    # en:  a 引用 t；b 引用 a；x<->y 循环
    # fr:  parent=en，自身无 a，fr.c 引用 a（沿父链落到 en.a）
    # de:  parent=en，de 自带 a，de.d 引用 a（应解析到 de.a 而非 en.a）
    # ja:  parent=en，ja.broken 引用 missing.key（断裂）
    bundles = {
        "en": {
            "t": "target", "a": "see {@t}", "b": "go {@a}",
            "x": "{@y}", "y": "{@x}",
        },
        "fr": {"c": "fr see {@a}"},
        "de": {"a": "DE own a {@t}", "d": "see {@a}"},
        "ja": {"broken": "x {@missing.key}"},
    }
    parents = {"fr": "en", "de": "en", "ja": "en"}
    return _import(client, bundles, parents)


def test_impact_direct_indirect_and_paths(client, chain_rev):
    rev = chain_rev
    r = client.get(f"/api/revisions/{rev}/impact",
                   params={"locale": "en", "key": "t"})
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["rev"] == rev
    # 版本摘要绑定分析结果（current_rev 可能被后续用例的其他模块导入改变，
    # 因此只校验字段存在；is_current 的过期语义另用专门用例覆盖）
    assert d["revision"]["rev"] == rev
    assert "current_rev" in d["revision"]
    assert isinstance(d["revision"]["is_current"], bool)

    by_node = {(i["locale"], i["key"]): i for i in d["impacted"]}
    # 直接：en.a
    assert by_node[("en", "a")]["direct"] is True
    assert by_node[("en", "a")]["distance"] == 1
    # 间接：en.b(2)、fr.c 沿父链 fr->en 解析到 en.a(2)、
    # de.a 引用 t(1 直接)，de.d -> de.a -> en.t(2)
    assert by_node[("en", "b")]["distance"] == 2
    assert by_node[("fr", "c")]["distance"] == 2
    assert by_node[("de", "d")]["distance"] == 2
    assert by_node[("de", "a")]["distance"] == 1
    # 受影响语言排序确定
    assert d["affected_locales"] == ["de", "en", "fr"]
    # 稳定排序：(distance, locale, key)
    order = [(i["distance"], i["locale"], i["key"]) for i in d["impacted"]]
    assert order == sorted(order)
    # 最短证据路径内容
    frc = by_node[("fr", "c")]
    assert [(p["locale"], p["key"]) for p in frc["path"]] == [
        ("fr", "c"), ("en", "a"), ("en", "t")]
    assert frc["path_refs"] == ["a", "t"]
    ded = by_node[("de", "d")]
    assert [(p["locale"], p["key"]) for p in ded["path"]] == [
        ("de", "d"), ("de", "a"), ("en", "t")]
    # 摘要：直接 en.a / de.a，间接 en.b / fr.c / de.d
    assert d["summary"]["direct"] == 2
    assert d["summary"]["indirect"] == 3


def test_graph_payload_structure(client, chain_rev):
    d = client.get(f"/api/revisions/{chain_rev}/impact",
                   params={"locale": "en", "key": "t"}).json()
    g = d["graph"]
    assert len(g["nodes"]) == d["summary"]["total_nodes"]
    assert len(g["edges"]) == d["summary"]["total_edges"]
    # 节点 id 与数组下标一致
    assert [n["id"] for n in g["nodes"]] == list(range(len(g["nodes"])))
    # 目标节点
    target = [n for n in g["nodes"] if n["kind"] == "target"]
    assert [(n["locale"], n["key"]) for n in target] == [("en", "t")]
    # 断裂边（ja.broken -> missing.key）与回退链
    broken = [e for e in g["edges"] if e["broken"]]
    assert len(broken) == 1
    e = broken[0]
    assert e["to"] is None
    src = g["nodes"][e["from"]]
    assert (src["locale"], src["key"]) == ("ja", "broken")
    assert e["ref_key"] == "missing.key"
    assert e["chain"][0] == "ja" and "en" in e["chain"]
    # 已解析边指向有效节点
    for e in g["edges"]:
        if not e["broken"]:
            assert 0 <= e["to"] < len(g["nodes"])


def test_scc_detection(client, chain_rev):
    d = client.get(f"/api/revisions/{chain_rev}/impact",
                   params={"locale": "en", "key": "t"}).json()
    sccs = [[(n["locale"], n["key"]) for n in comp] for comp in d["sccs"]]
    assert sccs == [[("en", "x"), ("en", "y")]]
    assert d["summary"]["in_cycle"] is False
    assert d["target_scc_index"] == -1

    d2 = client.get(f"/api/revisions/{chain_rev}/impact",
                    params={"locale": "en", "key": "x"}).json()
    assert d2["summary"]["in_cycle"] is True
    scc = d2["sccs"][d2["target_scc_index"]]
    assert {("en", "x"), ("en", "y")} == {(n["locale"], n["key"]) for n in scc}


def test_self_loop_is_cycle(client):
    rev = _import(client, {
        "en": {"s": "{@s}", "z": "z"},
        "fr": {"s": "s"},
    }, {"fr": "en"})
    d = client.get(f"/api/revisions/{rev}/impact",
                   params={"locale": "en", "key": "s"}).json()
    assert [[(n["locale"], n["key"]) for n in c] for c in d["sccs"]] == [
        [("en", "s")]]
    assert d["summary"]["in_cycle"] is True


def test_missing_targets_404(client, chain_rev):
    assert client.get(f"/api/revisions/{chain_rev}/impact",
                      params={"locale": "en", "key": "nope"}).status_code == 404
    assert client.get("/api/revisions/rdeadbeef/impact",
                      params={"locale": "en", "key": "t"}).status_code == 404


def test_preview_delete_new_broken_vs_preexisting(client, chain_rev):
    rev = chain_rev
    r = client.post(f"/api/revisions/{rev}/impact/preview", json={
        "locale": "en", "key": "t",
        "fixes": [{"op": "delete", "locale": "en", "key": "t"}]})
    assert r.status_code == 200, r.text
    p = r.json()
    # 不落库：目标版本快照内容不变（预演不产生新版本/审计）
    revs_before = {x["rev"] for x in client.get("/api/revisions").json()["revisions"]}
    assert rev in revs_before
    entries = client.get(f"/api/revisions/{rev}/entries/en").json()["entries"]
    assert entries["t"] == "target"
    assert p["target"]["deleted"] is True
    assert p["target"]["exists_after"] is False
    assert p["impact_after"] is None
    refs = p["broken_refs"]
    # en.a 与 de.a 都直接引用 en.t，删除后两条边均断裂
    nb = {(b["locale"], b["key"], b["ref_key"]): b for b in refs["new_broken"]}
    assert ("en", "a", "t") in nb
    assert ("de", "a", "t") in nb
    assert nb[("en", "a", "t")]["cause"] == "target_deleted"
    assert (nb[("en", "a", "t")]["previous_target"]["locale"] == "en"
            and nb[("en", "a", "t")]["previous_target"]["key"] == "t")
    # ja 的断裂是原有问题
    old = {(b["locale"], b["key"], b["ref_key"]) for b in refs["preexisting"]}
    assert ("ja", "broken", "missing.key") in old
    assert refs["summary"]["broken_after"] == refs["summary"]["broken_before"] + 2
    # en.a 被删除时它携带的 {@t} 属于「源消息消失」，不重复计入新断裂
    # （本例未删除 en.a，故 source_removed 为 0）
    assert refs["summary"]["source_removed"] == 0


def test_preview_rewrite_keeps_graph(client, chain_rev):
    rev = chain_rev
    p = client.post(f"/api/revisions/{rev}/impact/preview", json={
        "locale": "en", "key": "t",
        "fixes": [{"op": "upsert", "locale": "en", "key": "t",
                   "value": "new target text"}]}).json()
    assert p["target"]["deleted"] is False
    assert p["target"]["exists_after"] is True
    assert p["broken_refs"]["summary"]["new_broken"] == 0
    # 原有断裂仍被如实报告
    assert p["broken_refs"]["summary"]["preexisting"] == 1
    # 改写不涉及引用结构，影响集不变
    assert p["impact_after"]["summary"]["indirect"] == 3


def test_preview_rewrite_ref_adds_dependency(client, chain_rev):
    """把 en.t 改成引用 b：t 新增对 b 的引用，b/a 形成 t->b->a->t 环；
    且预演后能发现落点变化与新增 SCC。"""
    rev = chain_rev
    p = client.post(f"/api/revisions/{rev}/impact/preview", json={
        "locale": "en", "key": "t",
        "fixes": [{"op": "upsert", "locale": "en", "key": "t",
                   "value": "{@b}"}]}).json()
    assert p["broken_refs"]["summary"]["new_broken"] == 0
    sccs = {(n["locale"], n["key"]) for comp in p["impact_after"]["sccs"]
            for n in comp}
    assert {("en", "t"), ("en", "a"), ("en", "b")} <= sccs
    assert p["impact_after"]["summary"]["in_cycle"] is True


def test_preview_retarget_when_local_shadow_deleted(client, chain_rev):
    """删除 de.a 后，de.d 中的 {@a} 改由父语言 en.a 承接：落点变化而非断裂。"""
    rev = chain_rev
    p = client.post(f"/api/revisions/{rev}/impact/preview", json={
        "locale": "de", "key": "a",
        "fixes": [{"op": "delete", "locale": "de", "key": "a"}]}).json()
    rt = p["broken_refs"]["retargeted"]
    hit = [x for x in rt
           if x["locale"] == "de" and x["key"] == "d" and x["ref_key"] == "a"]
    assert len(hit) == 1
    assert (hit[0]["from"]["locale"], hit[0]["from"]["key"]) == ("de", "a")
    assert (hit[0]["to"]["locale"], hit[0]["to"]["key"]) == ("en", "a")
    assert p["broken_refs"]["summary"]["new_broken"] == 0
    # de.a 携带的 {@t} 随源消息删除而消失（非断裂）
    removed = {(b["locale"], b["key"], b["ref_key"])
               for b in p["broken_refs"]["source_removed"]}
    assert ("de", "a", "t") in removed


def test_preview_resolves_broken_when_key_added(client, chain_rev):
    rev = chain_rev
    p = client.post(f"/api/revisions/{rev}/impact/preview", json={
        "locale": "ja", "key": "broken",
        "fixes": [{"op": "upsert", "locale": "en", "key": "missing.key",
                   "value": "now exists"}]}).json()
    # 修复集在父语言补出缺失键后，ja 的断裂引用被顺带修复
    assert p["target"]["exists_after"] is True
    fixed = {(b["locale"], b["key"], b["ref_key"])
             for b in p["broken_refs"]["resolved"]}
    assert ("ja", "broken", "missing.key") in fixed
    # 该断裂修复后不再有原有断裂
    assert p["broken_refs"]["summary"]["preexisting"] == 0
    assert p["broken_refs"]["summary"]["broken_after"] == 0


def test_preview_upsert_new_unresolvable_ref_is_new_broken(client, chain_rev):
    """upsert 改写仍存在的源消息、新增无法解析的 {@key}：
    属于修复集引入的新增断裂，不得误列为原有问题。"""
    r = client.post(f"/api/revisions/{chain_rev}/impact/preview", json={
        "locale": "fr", "key": "c",
        "fixes": [{"op": "upsert", "locale": "fr", "key": "c",
                   "value": "fr see {@a} and {@nope}"}]})
    assert r.status_code == 200, r.text
    refs = r.json()["broken_refs"]
    nb = {(b["locale"], b["key"], b["ref_key"]): b for b in refs["new_broken"]}
    assert ("fr", "c", "nope") in nb
    # 新引入的引用没有「原先落点」，原因区别于目标被删/链变化
    assert nb[("fr", "c", "nope")]["cause"] == "ref_added"
    assert nb[("fr", "c", "nope")]["previous_target"] is None
    assert nb[("fr", "c", "nope")]["chain"][0] == "fr"
    # 原有断裂只剩 ja；新断裂不得混入
    old = {(b["locale"], b["key"], b["ref_key"]) for b in refs["preexisting"]}
    assert old == {("ja", "broken", "missing.key")}
    # 未受影响的既有解析边（fr.c 的 {@a} -> en.a）不出现在任何分类中
    assert refs["summary"]["new_broken"] == 1
    assert refs["summary"]["broken_after"] == \
        refs["summary"]["broken_before"] + 1


def test_preview_text_patch_new_unresolvable_ref_is_new_broken(client, chain_rev):
    """text_patch 在既有消息末尾追加无法解析的 {@key}：同样归为新增断裂。"""
    old_val = "see {@a}"  # de.d 的原文
    r = client.post(f"/api/revisions/{chain_rev}/impact/preview", json={
        "locale": "de", "key": "d",
        "fixes": [{"op": "text_patch", "locale": "de", "key": "d",
                   "patches": [{"start": len(old_val), "end": len(old_val),
                                "replacement": " {@nope.too}"}]}]})
    assert r.status_code == 200, r.text
    refs = r.json()["broken_refs"]
    nb = {(b["locale"], b["key"], b["ref_key"]): b for b in refs["new_broken"]}
    assert ("de", "d", "nope.too") in nb
    assert nb[("de", "d", "nope.too")]["cause"] == "ref_added"
    # de.d 原有的 {@a} 仍解析到 de.a（本语言遮蔽），不受影响
    assert refs["summary"]["preexisting"] == 1
    assert refs["summary"]["retargeted"] == 0


def test_preview_upsert_removing_old_broken_ref_is_removed(client, chain_rev):
    """upsert 改写后不再携带原有断裂引用：归为「改写移除」而非异常/原有。"""
    r = client.post(f"/api/revisions/{chain_rev}/impact/preview", json={
        "locale": "ja", "key": "broken",
        "fixes": [{"op": "upsert", "locale": "ja", "key": "broken",
                   "value": "no refs anymore"}]})
    assert r.status_code == 200, r.text  # 不再抛异常，始终返回结构化结果
    p = r.json()
    refs = p["broken_refs"]
    gone = {(b["locale"], b["key"], b["ref_key"]): b for b in refs["removed"]}
    assert ("ja", "broken", "missing.key") in gone
    assert gone[("ja", "broken", "missing.key")]["chain"][0] == "ja"
    # 断裂随之消失：不算原有、不算顺带修复（引用本身已不存在）
    assert refs["summary"]["preexisting"] == 0
    assert refs["summary"]["resolved"] == 0
    assert refs["summary"]["removed"] == 1
    assert refs["summary"]["broken_after"] == 0
    # 源消息仍在：不属于「随源删除消失」，目标视为被改写
    assert refs["summary"]["source_removed"] == 0
    assert p["target"]["exists_after"] is True
    assert p["target"]["rewritten"] is True


def test_preview_text_patch_removing_old_broken_ref_is_removed(client, chain_rev):
    """text_patch 精确删除原有断裂引用的文本片段：同样归为「改写移除」。"""
    val = "x {@missing.key}"  # ja.broken 的原文
    start = val.index("{@missing.key}")
    r = client.post(f"/api/revisions/{chain_rev}/impact/preview", json={
        "locale": "ja", "key": "broken",
        "fixes": [{"op": "text_patch", "locale": "ja", "key": "broken",
                   "patches": [{"start": start,
                                "end": start + len("{@missing.key}"),
                                "replacement": "ok"}]}]})
    assert r.status_code == 200, r.text
    refs = r.json()["broken_refs"]
    assert {(b["locale"], b["key"], b["ref_key"]) for b in refs["removed"]} == \
        {("ja", "broken", "missing.key")}
    assert refs["summary"]["broken_after"] == 0
    assert refs["summary"]["new_broken"] == 0


def test_preview_delete_source_message_keeps_source_removed(client, chain_rev):
    """删除源消息：其携带的引用仍归为「随源删除消失」，依赖它的引用才是新增断裂。"""
    r = client.post(f"/api/revisions/{chain_rev}/impact/preview", json={
        "locale": "en", "key": "a",
        "fixes": [{"op": "delete", "locale": "en", "key": "a"}]})
    assert r.status_code == 200, r.text
    p = r.json()
    refs = p["broken_refs"]
    assert p["target"]["deleted"] is True
    # en.a 自身携带的 {@t} 随源消息消失（删除前可解析）
    removed = {(b["locale"], b["key"], b["ref_key"]): b
               for b in refs["source_removed"]}
    assert ("en", "a", "t") in removed
    assert removed[("en", "a", "t")]["was_broken"] is False
    # en.b 与 fr.c 的 {@a} 失去落点：新增断裂（目标被删）
    nb = {(b["locale"], b["key"], b["ref_key"]): b for b in refs["new_broken"]}
    assert ("en", "b", "a") in nb and ("fr", "c", "a") in nb
    assert nb[("en", "b", "a")]["cause"] == "target_deleted"
    assert (nb[("fr", "c", "a")]["previous_target"]
            == {"locale": "en", "key": "a"})
    # de.d 的 {@a} 由 de 自己的 a 承接，不受影响；ja 的断裂仍是原有问题
    assert refs["summary"]["retargeted"] == 0
    assert {(b["locale"], b["key"], b["ref_key"]) for b in refs["preexisting"]} == \
        {("ja", "broken", "missing.key")}
    assert refs["summary"]["broken_after"] == \
        refs["summary"]["broken_before"] + 2


def test_preview_invalid_fix_returns_400_and_persists_nothing(client, chain_rev):
    rev_list_before = len(client.get("/api/revisions").json()["revisions"])
    r = client.post(f"/api/revisions/{chain_rev}/impact/preview", json={
        "locale": "en", "key": "t",
        "fixes": [{"op": "text_patch", "locale": "en", "key": "t",
                   "patches": [{"start": 0, "end": 999, "replacement": "x"}]}]})
    assert r.status_code == 400
    assert len(client.get("/api/revisions").json()["revisions"]) == rev_list_before


def test_preview_unknown_locale_404(client, chain_rev):
    r = client.post(f"/api/revisions/{chain_rev}/impact/preview", json={
        "locale": "zz", "key": "t", "fixes": []})
    assert r.status_code == 404


def test_impact_determinism(client, chain_rev):
    """重复请求结果字节级一致（排序/编号确定性）。"""
    url = f"/api/revisions/{chain_rev}/impact"
    params = {"locale": "en", "key": "t"}
    a = client.get(url, params=params).content
    b = client.get(url, params=params).content
    assert a == b


def test_impact_revision_summary_marks_stale(client):
    """分析绑定旧版本、当前版本已前移时，摘要明确标记过期。"""
    rev = _import(client, {"en": {"t": "x {@z}", "z": "z"},
                           "fr": {"z": "z"}}, {"fr": "en"})
    d1 = client.get(f"/api/revisions/{rev}/impact",
                    params={"locale": "en", "key": "z"}).json()
    assert d1["revision"]["is_current"] is True
    # 合并产生新版本，旧版本结果变为过期
    m = client.post("/api/merge", json={
        "base_rev": rev,
        "fixes": [{"op": "upsert", "locale": "en", "key": "z", "value": "zz"}]}
    ).json()["rev"]
    d2 = client.get(f"/api/revisions/{rev}/impact",
                    params={"locale": "en", "key": "z"}).json()
    assert d2["revision"]["is_current"] is False
    assert d2["revision"]["current_rev"] == m
    # 新版本自身的分析仍标记为当前
    d3 = client.get(f"/api/revisions/{m}/impact",
                    params={"locale": "en", "key": "z"}).json()
    assert d3["revision"]["is_current"] is True
