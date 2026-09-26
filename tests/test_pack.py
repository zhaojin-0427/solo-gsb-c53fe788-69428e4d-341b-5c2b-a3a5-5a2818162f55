"""自包含语言包编译：父链来源、引用就地展开、ICU 语义保留、
断裂/引用环/语法错误整包阻断、逐键来源、确定性字节、只读与独立可用性。"""
import io
import json
import os
import tempfile
import zipfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["LRC_DB_PATH"] = os.path.join(_tmp, f"testpack_{os.getpid()}.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

SAMPLE_BASE = os.path.join(os.path.dirname(__file__), "..", "samples")


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _import(client, files, parents=None, base="en"):
    body = {"base_locale": base,
            "files": [{"filename": f"{loc}.json", "content": json.dumps(entries, ensure_ascii=False)}
                      for loc, entries in files.items()],
            "parents": parents or {}}
    r = client.post("/api/import-json", json=body)
    assert r.status_code == 200, r.text
    return r.json()["rev"]


def _import_samples(client, parents=None):
    files = []
    for name in ["en.json", "fr.json", "de.json", "ja.json"]:
        with open(os.path.join(SAMPLE_BASE, name), encoding="utf-8") as f:
            files.append({"filename": name, "content": f.read()})
    body = {"base_locale": "en",
            "files": files,
            "parents": parents or {"fr": "en", "de": "en", "ja": "en"}}
    r = client.post("/api/import-json", json=body)
    assert r.status_code == 200, r.text
    return r.json()["rev"]


def _compile(client, rev, locales, keys):
    return client.post(f"/api/revisions/{rev}/pack",
                       json={"locales": locales, "keys": keys})


def _package(client, rev, locales, keys):
    r = _compile(client, rev, locales, keys)
    assert r.status_code == 200, r.text
    return r.json()


# ---------------------------------------------------------------- 来源与展开

def test_fallback_source_recorded(client):
    rev = _import(client, {
        "en": {"k": "Hello"},
        "de": {},
    }, parents={"de": "en"})
    j = _package(client, rev, ["de", "en"], ["k"])
    assert j["locales"] == ["de", "en"] and j["keys"] == ["k"]
    prov_de = j["provenance"]["de"]["k"]["source"]
    assert prov_de["locale"] == "en"
    assert prov_de["fell_back"] is True
    assert prov_de["fallback_chain"] == ["de", "en"]
    assert j["templates"]["de"]["k"] == "Hello"
    prov_en = j["provenance"]["en"]["k"]["source"]
    assert prov_en["locale"] == "en" and prov_en["fell_back"] is False
    # 来源去重，顺序为首次访问
    assert j["provenance"]["de"]["k"]["sources"] == [{"locale": "en", "key": "k"}]


def test_ref_expands_from_containing_message_locale_with_shadowing(client):
    rev = _import_samples(client)
    # fr.refA 引用 {@appTitle}，fr 自己有 appTitle（遮蔽）-> 取 fr 的值
    j = _package(client, rev, ["fr"], ["refA"])
    assert j["templates"]["fr"]["refA"] == \
        "Astuce : Réviseur de contrats veille sur chaque contrat"
    prov = j["provenance"]["fr"]["refA"]
    assert [tuple(s.values()) for s in prov["sources"]] == \
        [("fr", "refA"), ("fr", "appTitle")]
    hop = prov["hops"][0]
    assert hop == {"ref_key": "appTitle",
                   "from": {"locale": "fr", "key": "refA"},
                   "to": {"locale": "fr", "key": "appTitle"},
                   "chain": ["fr", "en"]}
    # de.refC -> en.refA -> en.appTitle（de 两者皆无）
    j = _package(client, rev, ["de"], ["refC"])
    assert j["templates"]["de"]["refC"] == \
        "Hinweis: Tip: Contract Reviewer keeps every contract safe"
    sources = [tuple(s.values()) for s in j["provenance"]["de"]["refC"]["sources"]]
    assert sources == [("de", "refC"), ("en", "refA"), ("en", "appTitle")]
    assert [h["ref_key"] for h in j["provenance"]["de"]["refC"]["hops"]] == \
        ["refA", "appTitle"]
    # ja.jaGuide -> en.refB -> en.refA -> en.appTitle（ja 三个键皆无）
    j = _package(client, rev, ["ja"], ["jaGuide"])
    assert j["templates"]["ja"]["jaGuide"] == \
        "ご案内：Reminder — Tip: Contract Reviewer keeps every contract safe"
    hops = j["provenance"]["ja"]["jaGuide"]["hops"]
    assert [(h["from"]["locale"], h["ref_key"], h["to"]["locale"]) for h in hops] == \
        [("ja", "refB", "en"), ("en", "refA", "en"), ("en", "appTitle", "en")]


def test_variables_and_plural_and_hash_preserved(client):
    rev = _import(client, {
        "en": {"t": "{n, plural, =0 {none} other {# items for {name}}}"},
        "de": {},
    }, parents={"de": "en"})
    j = _package(client, rev, ["de"], ["t"])
    t = j["templates"]["de"]["t"]
    assert t == "{n, plural, =0 {none} other {# items for {name}}}"
    assert j["provenance"]["de"]["t"]["source"]["fell_back"] is True


def test_referenced_top_level_hash_is_escaped_when_inlined_into_plural(client):
    """被引用消息顶层的裸 # 只是普通文本；内联进 plural 分支体后必须加引号，
    否则会被重新解释为数量符号。"""
    rev = _import(client, {
        "en": {
            "note": "tip # literal {x}",
            "t": "{n, plural, one {see {@note}} other {see {@note}}}",
        },
    }, parents={})
    j = _package(client, rev, ["en"], ["t"])
    t = j["templates"]["en"]["t"]
    # 顶层 # 被引号保护；{x} 是结构占位符（note 自带 x），原样保留
    assert "'#'" in t and "{x}" in t
    # 独立可用性：note 内联后 # 仍是字面量，不被当前 plural 参数替换
    rev2 = _import(client, {"en": {"t": t, "note": "tip # literal {x}"}}, parents={})
    r = client.post(f"/api/revisions/{rev2}/preview",
                    json={"key": "t", "locales": ["en"],
                          "variables": {"n": 2, "x": "Z"}})
    assert r.json()["results"][0]["text"] == "see tip # literal Z"


def test_hash_inside_referenced_own_plural_stays_bound(client):
    rev = _import(client, {
        "en": {
            "sub": "{m, plural, other {# sub}}",
            "t": "{n, plural, other {outer: {@sub}}}",
        },
    }, parents={})
    j = _package(client, rev, ["en"], ["t"])
    t = j["templates"]["en"]["t"]
    assert t == "{n, plural, other {outer: {m, plural, other {# sub}}}}"


def test_quote_semantics_preserved(client):
    rev = _import(client, {
        "en": {
            "q": "a''b '{x}' end",
            "t": "go {@q}",
        },
    }, parents={})
    j = _package(client, rev, ["en"], ["t"])
    t = j["templates"]["en"]["t"]
    # 字面 {x} 必须被引号保护，'' 双写保留
    assert "'{x}'" in t and "a''b" in t
    rev2 = _import(client, {"en": {"t": t}}, parents={})
    r = client.post(f"/api/revisions/{rev2}/preview",
                    json={"key": "t", "locales": ["en"], "variables": {}})
    assert r.json()["results"][0]["text"] == "go a'b {x} end"


def test_select_cases_preserved(client):
    rev = _import(client, {
        "en": {"s": "{g, select, male {his #} female {her} other {their {n, number}}}"},
        "de": {},
    }, parents={"de": "en"})
    j = _package(client, rev, ["de"], ["s"])
    t = j["templates"]["de"]["s"]
    # select 分支体不是复数上下文：# 作为字面 # 出现（解析器不产出 hash 事件）；
    # 分支顺序按原文保留
    assert t == "{g, select, male {his #} female {her} other {their {n, number}}}"


# ---------------------------------------------------------------- 整包阻断

def test_broken_reference_blocks_whole_package(client):
    rev = _import_samples(client)
    r = _compile(client, rev, ["ja"], ["brokenRefDemo", "jaGuide"])
    assert r.status_code == 422
    errs = r.json()["detail"]["errors"]
    assert len(errs) == 1
    e = errs[0]
    assert e["code"] == "BROKEN_REFERENCE"
    assert e["locale"] == "ja" and e["key"] == "brokenRefDemo"
    assert e["path"] == ["@does.not.exist"]
    assert e["chain"] == ["ja", "en"]

    # 其他语言同时存在的错误也被一并收集并稳定排序
    r = _compile(client, rev, ["ja", "fr"],
                 ["brokenRefDemo", "cycX", "jaGuide"])
    assert r.status_code == 422
    errs = r.json()["detail"]["errors"]
    # 至少包含断裂 + 环两类
    assert {e["code"] for e in errs} >= {"BROKEN_REFERENCE", "REFERENCE_CYCLE"}
    assert [(e["locale"], e["key"], e["code"]) for e in errs] == \
        sorted((e["locale"], e["key"], e["code"]) for e in errs)
    cyc = next(e for e in errs if e["code"] == "REFERENCE_CYCLE")
    assert cyc["path"][0].startswith("@")
    assert cyc["cycle"][0] == cyc["cycle"][-1]


def test_cycle_in_transitive_reference_blocks(client):
    rev = _import(client, {
        "en": {"x": "{@y}", "y": "{@z}", "z": "{@x}", "a": "{@x}"},
        "de": {},
    }, parents={"de": "en"})
    r = _compile(client, rev, ["de"], ["a"])
    assert r.status_code == 422
    e = r.json()["detail"]["errors"][0]
    assert e["code"] == "REFERENCE_CYCLE"
    assert e["key"] == "a" and e["locale"] == "de"
    assert e["path"] == ["@x", "@y", "@z", "@x"]


def test_syntax_error_blocks(client):
    rev = _import(client, {
        "en": {"a": "{@b}", "b": "oops {"},
    }, parents={})
    r = _compile(client, rev, ["en"], ["a"])
    assert r.status_code == 422
    e = r.json()["detail"]["errors"][0]
    assert e["code"] == "INVALID_MESSAGE"
    assert e["key"] == "a"               # 入口键
    assert e["message_locale"] == "en"
    assert "pos" in e
    # 入口本身语法错误
    rev2 = _import(client, {"en": {"k": "x {y"}}, parents={})
    r = _compile(client, rev2, ["en"], ["k"])
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["code"] == "INVALID_MESSAGE"


def test_unresolvable_entry_and_unknown_locale_block(client):
    rev = _import(client, {"en": {"a": "A"}}, parents={})
    r = _compile(client, rev, ["en"], ["nope"])
    assert r.status_code == 422
    e = r.json()["detail"]["errors"][0]
    assert e["code"] == "ENTRY_NOT_FOUND" and e["chain"] == ["en"]
    r = _compile(client, rev, ["zz"], ["a"])
    assert r.status_code == 422
    assert r.json()["detail"]["errors"][0]["code"] == "LOCALE_NOT_FOUND"
    # 请求参数为空 -> 400
    assert _compile(client, rev, [], ["a"]).status_code == 400
    assert _compile(client, rev, ["en"], []).status_code == 400
    # 版本不存在 -> 404
    assert _compile(client, "rnope", ["en"], ["a"]).status_code == 404


def test_invalid_locale_id_blocks(client):
    """不能作为安全文件名的语言标识（含路径穿越）被拒绝。"""
    body = {"base_locale": "en",
            "files": [{"filename": "en.json", "content": '{"a":"A"}'},
                      {"filename": "x.json", "content": '{"a":"B"}'}],
            "locales": {"x.json": "../bad"}}
    rev = client.post("/api/import-json", json=body).json()["rev"]
    r = _compile(client, rev, ["../bad"], ["a"])
    assert r.status_code == 422
    e = r.json()["detail"]["errors"][0]
    assert e["code"] == "INVALID_LOCALE_ID" and e["locale"] == "../bad"


# ---------------------------------------------------------------- 包内容与确定性

def test_zip_contents_manifest_and_sha(client):
    rev = _import_samples(client)
    j = _package(client, rev, ["de", "fr"], ["appTitle", "refA"])
    names = [f["path"] for f in j["files"]]
    assert names == ["de.json", "fr.json"]
    m = j["manifest"]
    assert m["manifest_version"] >= 1
    assert m["revision"]["rev"] == rev
    assert m["compilation"] == {"locales": ["de", "fr"],
                                "entry_keys": ["appTitle", "refA"]}
    assert m["files"] == j["files"]
    # 清单逐键来源
    assert m["entries"]["fr"]["refA"]["source"]["locale"] == "fr"
    # de 没有 refA：沿 de->en 回退到 en
    assert m["entries"]["de"]["refA"]["source"]["locale"] == "en"
    assert m["entries"]["de"]["refA"]["source"]["fell_back"] is True
    assert m["entries"]["de"]["refA"]["source"]["fallback_chain"] == ["de", "en"]

    # 下载 ZIP 与编译结果同字节
    params = "&".join([f"locales=de", "locales=fr",
                       "keys=appTitle", "keys=refA"])
    d1 = client.get(f"/api/revisions/{rev}/pack/download?{params}").content
    d2 = client.get(f"/api/revisions/{rev}/pack/download?{params}").content
    assert d1 == d2
    import hashlib
    assert hashlib.sha256(d1).hexdigest() == j["package"]["sha256"]
    zf = zipfile.ZipFile(io.BytesIO(d1))
    assert sorted(zf.namelist()) == ["de.json", "fr.json", "manifest.json"]
    assert zf.testzip() is None
    # ZIP 内 JSON 与清单 SHA-256 一致
    for f in j["files"]:
        assert hashlib.sha256(zf.read(f["path"])).hexdigest() == f["sha256"]
    manifest_in_zip = json.loads(zf.read("manifest.json").decode("utf-8"))
    assert manifest_in_zip == j["manifest"]


def test_deterministic_bytes_for_same_input(client):
    rev = _import_samples(client)
    # 参数乱序、重复，编译结果仍一致（内部去重排序）
    a = _package(client, rev, ["fr", "de", "fr"], ["refA", "appTitle", "refA"])
    b = _package(client, rev, ["de", "fr"], ["appTitle", "refA"])
    assert a["package"]["sha256"] == b["package"]["sha256"]
    assert a["files"] == b["files"]
    assert a["templates"] == b["templates"]


def test_exported_package_is_usable_without_parent_chain(client):
    """核心保证：只导出 ja 的 JSON，作为无父链的新版本导入后可独立渲染，
    文本与原始带父链版本的预览一致。"""
    rev = _import_samples(client)
    keys = ["jaGuide", "greeting", "cart.items"]
    j = _package(client, rev, ["ja"], keys)
    ja_json = json.dumps(j["templates"]["ja"], ensure_ascii=False, sort_keys=True,
                         indent=2)
    # 无 {@ 引用残留
    assert "{@" not in ja_json

    # 原始（带 ja->en 父链）预览
    def orig_text(key, variables):
        r = client.post(f"/api/revisions/{rev}/preview",
                        json={"key": key, "locales": ["ja"],
                              "variables": variables})
        res = r.json()["results"][0]
        assert res["ok"] is True, res
        return res["text"]

    # 独立版本：只有 ja 一种语言、无父链
    rev2 = _import(client, {
        "ja": j["templates"]["ja"],
    }, base="ja", parents={})
    def standalone_text(key, variables):
        r = client.post(f"/api/revisions/{rev2}/preview",
                        json={"key": key, "locales": ["ja"],
                              "variables": variables})
        res = r.json()["results"][0]
        assert res["ok"] is True, res
        assert res["resolved_locale"] == "ja" and res["fell_back"] is False
        return res["text"]

    assert standalone_text("jaGuide", {}) == orig_text("jaGuide", {})
    assert standalone_text("jaGuide", {}) == \
        "ご案内：Reminder — Tip: Contract Reviewer keeps every contract safe"
    assert standalone_text("greeting", {"user": "Ana"}) == \
        orig_text("greeting", {"user": "Ana"})
    assert standalone_text("cart.items", {"count": 3}) == \
        orig_text("cart.items", {"count": 3})


def test_compile_is_readonly(client):
    rev = _import_samples(client)
    aud_before = client.get("/api/audit").json()["records"]
    _package(client, rev, ["fr", "de"], ["appTitle", "refA"])
    _compile(client, rev, ["ja"], ["brokenRefDemo"])   # 阻断请求
    aud_after = client.get("/api/audit").json()["records"]
    assert len(aud_before) == len(aud_after)
    assert client.get("/api/state").json()["current_rev"] == rev
    # 模板中不含引用，且仍是合法 ICU（抽样可解析）
    from app.icu import parse_message
    j = _package(client, rev, ["fr", "de", "ja"],
                 ["appTitle", "refA", "refB", "greeting"])
    for loc in j["templates"].values():
        for t in loc.values():
            assert "{@" not in t
            parse_message(t)
