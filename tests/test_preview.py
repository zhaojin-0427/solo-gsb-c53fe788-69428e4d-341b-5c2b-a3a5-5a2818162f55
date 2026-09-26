"""消息实例预览：回退取值、引用轨迹、CLDR 复数/序数、number/date/time、
精确分支、# 符号、错误隔离与版本绑定。"""
import json
import os
import tempfile

import pytest

_tmp = tempfile.mkdtemp()
os.environ["LRC_DB_PATH"] = os.path.join(_tmp, f"testprev_{os.getpid()}.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.preview import render_message  # noqa: E402
from app.service import analyze_snapshot, preview_messages  # noqa: E402
from app import db as dbmod  # noqa: E402


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


def _preview(client, rev, key, locales, variables):
    return client.post(f"/api/revisions/{rev}/preview",
                       json={"key": key, "locales": locales,
                             "variables": variables}).json()


def _by(j, loc):
    return next(x for x in j["results"] if x["locale"] == loc)


# ---------------------------------------------------------------- 基础解析

def test_simple_variable_and_fallback(client):
    rev = _import(client, {
        "en": {"k": "Hello {name}"},
        "de": {},                      # de 缺键，回退 en
        "fr": {"k": "Bonjour {name}"},
    }, parents={"de": "en", "fr": "en"})
    j = _preview(client, rev, "k", ["en", "de", "fr"], {"name": "Ada"})
    assert j["summary"] == {"locales": 3, "ok": 3, "errors": 0}
    assert _by(j, "en")["text"] == "Hello Ada"
    assert _by(j, "fr")["text"] == "Bonjour Ada"
    de = _by(j, "de")
    assert de["text"] == "Hello Ada"
    assert de["fell_back"] is True
    assert de["resolved_locale"] == "en"
    assert de["fallback_chain"] == ["de", "en"]
    assert de["hops"] == []
    en = _by(j, "en")
    assert en["fell_back"] is False and en["resolved_locale"] == "en"


def test_missing_param_is_isolated_per_locale(client):
    rev = _import(client, {
        "en": {"k": "Hi {name}"},
        "fr": {"k": "Bonjour {name}"},
    }, parents={"fr": "en"})
    # ja 消息占位符契约与 en 不同（{user}）：en/fr 用 name 成功，ja 缺 user 出错
    rev2 = _import(client, {
        "en": {"k": "Hi {name}"},
        "ja": {"k": "こんにちは {user}"},
    }, parents={"ja": "en"})
    j = _preview(client, rev2, "k", ["en", "ja"], {"name": "A"})
    assert _by(j, "en")["ok"] is True
    err = _by(j, "ja")["error"]
    assert err["code"] == "MISSING_PARAM"
    assert err["locale"] == "ja" and err["key"] == "k"
    assert err["path"] == ["user"]
    assert "text" not in _by(j, "ja")     # 不输出部分文本
    assert j["summary"] == {"locales": 2, "ok": 1, "errors": 1}


# ---------------------------------------------------------------- number/date/time

def test_number_formats_follow_target_locale(client):
    rev = _import(client, {
        "en": {"n": "{x, number}", "p": "{x, number, percent}",
               "c": "{x, number, ::currency/USD}"},
        "de": {"n": "{x, number}", "p": "{x, number, percent}",
               "c": "{x, number, ::currency/EUR}"},
    }, parents={"de": "en"})
    j = _preview(client, rev, "n", ["en", "de"], {"x": 1234.5})
    assert _by(j, "en")["text"] == "1,234.5"
    assert _by(j, "de")["text"] == "1.234,5"
    j = _preview(client, rev, "p", ["en"], {"x": 0.25})
    assert _by(j, "en")["text"] == "25%"
    j = _preview(client, rev, "c", ["en", "de"], {"x": 1234.5})
    assert "$1,234.50" in _by(j, "en")["text"]
    assert "€" in _by(j, "de")["text"]


def test_number_type_mismatch_and_null(client):
    rev = _import(client, {"en": {"k": "{n, number}"}}, parents={})
    for bad in ("x", True, None, [1], {"a": 1}):
        j = _preview(client, rev, "k", ["en"], {"n": bad})
        assert _by(j, "en")["error"]["code"] == "TYPE_MISMATCH"


def test_simple_var_rejects_compound_types(client):
    rev = _import(client, {"en": {"k": "{v}"}}, parents={})
    assert _by(_preview(client, rev, "k", ["en"], {"v": 1}), "en")["text"] == "1"
    assert _by(_preview(client, rev, "k", ["en"], {"v": [1]}), "en")["error"]["code"] \
        == "TYPE_MISMATCH"


def test_date_time_iso8601(client):
    rev = _import(client, {
        "en": {"d": "{d, date, medium}", "t": "{t, time, short}",
               "dt": "{dt, date, full}"},
        "de": {"d": "{d, date, medium}", "t": "{t, time, short}"},
    }, parents={"de": "en"})
    j = _preview(client, rev, "d", ["en", "de"], {"d": "2026-03-09"})
    assert _by(j, "en")["text"] == "Mar 9, 2026"
    assert _by(j, "de")["text"] == "09.03.2026"
    j = _preview(client, rev, "t", ["en", "de"], {"t": "13:05"})
    assert _by(j, "en")["text"].replace(" ", " ") == "1:05 PM"
    assert _by(j, "de")["text"] == "13:05"
    j = _preview(client, rev, "dt", ["en"], {"dt": "2026-03-09T13:05:07"})
    assert "Monday" in _by(j, "en")["text"] and "2026" in _by(j, "en")["text"]
    # 非 ISO 字符串 / 非字符串类型
    bad = _preview(client, rev, "d", ["en"], {"d": "not-a-date"})
    assert _by(bad, "en")["error"]["code"] == "TYPE_MISMATCH"
    bad = _preview(client, rev, "t", ["en"], {"t": 9})
    assert _by(bad, "en")["error"]["code"] == "TYPE_MISMATCH"


# ---------------------------------------------------------------- 复数 / 序数

def test_plural_exact_branch_precedence_and_cldr(client):
    rev = _import(client, {
        "en": {"k": "{n, plural, =0 {none} =1 {uno} one {ONE} other {#x}}"},
        "fr": {"k": "{n, plural, =0 {zero-exact} one {un} other {#x}}"},
    }, parents={"fr": "en"})
    # 精确分支优先于 CLDR 类别（fr 的 0 在 CLDR 中属 one，但 =0 先命中）
    assert _by(_preview(client, rev, "k", ["fr"], {"n": 0}), "fr")["text"] == "zero-exact"
    assert _by(_preview(client, rev, "k", ["en"], {"n": 0}), "en")["text"] == "none"
    assert _by(_preview(client, rev, "k", ["en"], {"n": 1}), "en")["text"] == "uno"
    assert _by(_preview(client, rev, "k", ["en"], {"n": 2}), "en")["text"] == "2x"
    # fr CLDR：0 若无精确分支本应 one
    assert _by(_preview(client, rev, "k", ["fr"], {"n": 1}), "fr")["text"] == "un"


def test_plural_cldr_categories_ru_ar(client):
    rev = _import(client, {
        "en": {"k": "{n, plural, other {#}}"},
        "ru": {"k": "{n, plural, one {О} few {Ф} many {М} other {Д}}"},
        "ar": {"k": "{n, plural, zero {Z} two {T} few {F} many {K} other {O}}"},
    }, parents={"ru": "en", "ar": "en"})
    cases = {(1, "О"), (2, "Ф"), (3, "Ф"), (5, "М"), (11, "М"), (21, "О"),
             (0, "М")}
    for n, want in cases:
        assert _by(_preview(client, rev, "k", ["ru"], {"n": n}), "ru")["text"] == want
    assert _by(_preview(client, rev, "k", ["ar"], {"n": 2}), "ar")["text"] == "T"
    assert _by(_preview(client, rev, "k", ["ar"], {"n": 0}), "ar")["text"] == "Z"


def test_selectordinal_uses_ordinal_rules(client):
    rev = _import(client, {
        "en": {"k": "{n, selectordinal, one {#st} two {#nd} few {#rd} other {#th}}"},
    }, parents={})
    for n, suf in [(1, "1st"), (2, "2nd"), (3, "3rd"), (11, "11th"),
                   (12, "12th"), (13, "13th"), (21, "21st"), (22, "22nd"),
                   (101, "101st")]:
        assert _by(_preview(client, rev, "k", ["en"], {"n": n}), "en")["text"] == suf


def test_hash_formats_in_target_locale(client):
    rev = _import(client, {
        "en": {"k": "{n, plural, other {# pcs}}"}},
        parents={})
    assert _by(_preview(client, rev, "k", ["en"], {"n": 1234.5}), "en")["text"] \
        == "1,234.5 pcs"


def test_select_matching_and_other(client):
    rev = _import(client, {
        "en": {"k": "{g, select, male {his} female {her} other {their}}"},
        "de": {"k": "{g, select, male {sein} other {ihr}}"},
    }, parents={"de": "en"})
    assert _by(_preview(client, rev, "k", ["en"], {"g": "female"}), "en")["text"] == "her"
    # de 无 female -> other
    assert _by(_preview(client, rev, "k", ["de"], {"g": "female"}), "de")["text"] == "ihr"
    err = _by(_preview(client, rev, "k", ["de"], {"g": 1}), "de")["error"]
    assert err["code"] == "TYPE_MISMATCH"


def test_no_branch_error(client):
    rev = _import(client, {
        "en": {"k": "{g, select, male {m} female {f}}"},
    }, parents={})
    err = _by(_preview(client, rev, "k", ["en"], {"g": "other"}), "en")["error"]
    assert err["code"] == "NO_BRANCH"
    assert err["available"] == ["male", "female"]
    assert err["path"] == ["g"]
    # 无 other 的 plural，CLDR 类别落不到任何分支
    rev2 = _import(client, {
        "en": {"k": "{n, plural, one {one}}"},
    }, parents={})
    err = _by(_preview(client, rev2, "k", ["en"], {"n": 5}), "en")["error"]
    assert err["code"] == "NO_BRANCH"
    assert err["value"] == "5"


# ---------------------------------------------------------------- 引用

def test_ref_resolves_from_containing_message_locale(client):
    rev = _import(client, {
        "en": {"app": "App", "note": "via {@app}"},
        "fr": {"app": "Appli", "note": "via {@app}"},
        "ja": {"note": "{@app}"},     # 无 app，引用回退 en
        "de": {},                     # de 存在但无 note：整条消息回退 en
    }, parents={"fr": "en", "ja": "en", "de": "en"})
    jfr = _by(_preview(client, rev, "note", ["fr"], {}), "fr")
    assert jfr["text"] == "via Appli"
    assert jfr["hops"] == [
        {"ref_key": "app",
         "from": {"locale": "fr", "key": "note"},
         "to": {"locale": "fr", "key": "app"},
         "chain": ["fr", "en"]}]
    jja = _by(_preview(client, rev, "note", ["ja"], {}), "ja")
    assert jja["text"] == "App"
    # ja 自带 note（值为 {@app}）且无 app：消息本体在 ja，引用继续回退到 en.app
    assert jja["resolved_locale"] == "ja"
    assert jja["fell_back"] is False
    assert jja["hops"][0]["from"] == {"locale": "ja", "key": "note"}
    assert jja["hops"][0]["to"] == {"locale": "en", "key": "app"}

    # de 没有 note：整条消息回退到 en.note，引用也从 en 解析
    jde = _by(_preview(client, rev, "note", ["de"], {}), "de")
    assert jde["text"] == "via App"
    assert jde["resolved_locale"] == "en" and jde["fell_back"] is True
    assert jde["hops"][0]["from"] == {"locale": "en", "key": "note"}


def test_ref_chain_trace(client):
    rev = _import(client, {
        "en": {"a": "A {@b}", "b": "B {@c}", "c": "C"},
    }, parents={})
    x = _by(_preview(client, rev, "a", ["en"], {}), "en")
    assert x["text"] == "A B C"
    assert [h["ref_key"] for h in x["hops"]] == ["b", "c"]


def test_refs_share_outer_variables(client):
    rev = _import(client, {
        "en": {"a": "{n}+{@b}", "b": "{n}"},
    }, parents={})
    assert _by(_preview(client, rev, "a", ["en"], {"n": 7}), "en")["text"] == "7+7"
    err = _by(_preview(client, rev, "a", ["en"], {}), "en")["error"]
    assert err["code"] == "MISSING_PARAM"


def test_broken_reference_error(client):
    rev = _import(client, {
        "en": {"k": "see {@missing.key}"},
    }, parents={})
    err = _by(_preview(client, rev, "k", ["en"], {}), "en")["error"]
    assert err["code"] == "BROKEN_REFERENCE"
    assert err["path"] == ["@missing.key"]
    assert err["chain"] == ["en"]
    assert err["key"] == "k"


def test_reference_cycle_error(client):
    rev = _import(client, {
        "en": {"x": "{@y}", "y": "{@z}", "z": "{@x}"},
    }, parents={})
    err = _by(_preview(client, rev, "x", ["en"], {}), "en")["error"]
    assert err["code"] == "REFERENCE_CYCLE"
    assert err["cycle"][0] == err["cycle"][-1]
    assert err["path"] == ["@y", "@z", "@x"]


def test_invalid_icu_in_referenced_message(client):
    rev = _import(client, {
        "en": {"a": "{@b}", "b": "oops {"},
    }, parents={})
    err = _by(_preview(client, rev, "a", ["en"], {}), "en")["error"]
    assert err["code"] == "INVALID_MESSAGE"
    assert err["key"] == "b"


# ---------------------------------------------------------------- 其它

def test_message_not_found(client):
    rev = _import(client, {"en": {"a": "A"}}, parents={})
    assert _by(_preview(client, rev, "zzz", ["en"], {}), "en")["error"]["code"] \
        == "MESSAGE_NOT_FOUND"
    # 未知语言：整语言错误，其余照常
    j = _preview(client, rev, "a", ["en", "zz"], {})
    assert _by(j, "en")["ok"] is True
    assert _by(j, "zz")["error"]["code"] == "MESSAGE_NOT_FOUND"


def test_result_bound_to_revision_and_stale_flag(client):
    rev1 = _import(client, {"en": {"a": "v1"}, "fr": {"a": "v1fr"}},
                   parents={"fr": "en"})
    j = _preview(client, rev1, "a", ["en"], {})
    assert j["rev"] == rev1
    assert j["revision"]["rev"] == rev1
    assert j["revision"]["is_current"] is True
    # 合并产生新版本后，旧版本仍可只读预览且标记为非当前
    m = client.post("/api/merge", json={"base_rev": rev1,
                    "fixes": [{"op": "upsert", "locale": "fr", "key": "a",
                               "value": "v2fr"}]}).json()
    rev2 = m["rev"]
    j1 = _preview(client, rev1, "a", ["en"], {})
    assert j1["revision"]["is_current"] is False
    assert j1["revision"]["current_rev"] == rev2
    assert j1["results"][0]["text"] == "v1"      # 旧快照内容不变
    j2 = _preview(client, rev2, "a", ["en"], {})
    assert j2["revision"]["is_current"] is True


def test_api_validation_and_404(client):
    rev = _import(client, {"en": {"a": "A"}}, parents={})
    r = client.post(f"/api/revisions/{rev}/preview",
                    json={"key": "a", "locales": [], "variables": {}})
    assert r.status_code == 400
    r = client.post(f"/api/revisions/{rev}/preview",
                    json={"key": "", "locales": ["en"], "variables": {}})
    assert r.status_code == 400
    r = client.post("/api/revisions/nope/preview",
                    json={"key": "a", "locales": ["en"], "variables": {}})
    assert r.status_code == 404
    # locales 去重保序
    j = client.post(f"/api/revisions/{rev}/preview",
                    json={"key": "a", "locales": ["en", "en"],
                          "variables": {}}).json()
    assert [x["locale"] for x in j["results"]] == ["en"]


def test_preview_is_readonly(client):
    rev = _import(client, {"en": {"a": "{n}"}}, parents={})
    _preview(client, rev, "a", ["en"], {"n": 1})
    _preview(client, rev, "a", ["en"], {})   # 出错请求
    # 仅本次导入之后不应新增任何审计记录（预览不写库）
    aud_before = client.get("/api/audit").json()["records"]
    _preview(client, rev, "a", ["en"], {"n": 2})
    aud_after = client.get("/api/audit").json()["records"]
    assert len(aud_before) == len(aud_after)
    assert client.get("/api/state").json()["current_rev"] == rev


def test_quote_escaping(client):
    rev = _import(client, {
        "en": {"k": "a''b '{x}' {x}"},
    }, parents={})
    assert _by(_preview(client, rev, "k", ["en"], {"x": "Y"}), "en")["text"] \
        == "a'b {x} Y"


def test_unit_render_directly():
    """不经 HTTP 直接调用渲染器：RenderError 携带结构化字段。"""
    from app.main import conn as get_conn
    from app.preview import RenderError

    local = TestClient(app)
    with local:
        r = local.post("/api/import-json", json={
            "base_locale": "en",
            "files": [
                {"filename": "en.json", "content": json.dumps(
                    {"k": "{n, plural, other {#}}"})},
                {"filename": "fr.json", "content": json.dumps(
                    {"k": "{n, plural, one {un} other {#}}"})},
            ],
            "parents": {"fr": "en"}})
        rev = r.json()["rev"]
    snap = dbmod.load_revision(get_conn(), rev)
    analysis = analyze_snapshot(snap)
    assert render_message(analysis, "fr", "k", {"n": 1}).text == "un"
    with pytest.raises(RenderError) as ei:
        render_message(analysis, "fr", "k", {})
    assert ei.value.code == "MISSING_PARAM"
    assert ei.value.path == ["n"]
    d = ei.value.to_dict()
    assert d["locale"] == "fr" and d["key"] == "k"
