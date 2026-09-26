import json
import os
import tempfile

import pytest

# 在导入 app 前指定独立数据库（每次进程使用新库，避免跨轮次残留）
_tmp = tempfile.mkdtemp()
os.environ["LRC_DB_PATH"] = os.path.join(_tmp, f"test_{os.getpid()}.db")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app import db as dbmod  # noqa: E402


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def _files():
    base = os.path.join(os.path.dirname(__file__), "..", "samples")
    out = []
    for name in ["en.json", "fr.json", "de.json", "ja.json"]:
        with open(os.path.join(base, name), "rb") as f:
            out.append (("files", (name, f.read(), "application/json")))
    return out


def test_health(client):
    assert client.get("/api/health").json() == {"status": "ok"}


def test_full_flow_import_issues_merge_diff_audit(client):
    # 1) 导入（多部分表单，父语言用表单）
    r = client.post(
        "/api/import",
        data={"base_locale": "en",
              "parents_json": json.dumps({"fr": "en", "ja": "en", "de": "en"})},
        files=_files())
    assert r.status_code == 200, r.text
    rev1 = r.json()["rev"]
    assert client.get("/api/state").json()["current_rev"] == rev1

    # 2) 幂等：相同输入返回同一版本
    r2 = client.post(
        "/api/import",
        data={"base_locale": "en",
              "parents_json": json.dumps({"fr": "en", "ja": "en", "de": "en"})},
        files=_files())
    assert r2.json()["rev"] == rev1 and r2.json()["deduplicated"] is True

    # 3) 问题清单
    iss = client.get(f"/api/revisions/{rev1}/issues").json()
    codes = {}
    for i in iss["issues"]:
        codes[i["code"]] = codes.get(i["code"], 0) + 1
    assert codes.get("MISSING_KEY", 0) > 0       # de/ja 缺 untranslatedNote 等
    assert codes.get("PLACEHOLDER_MISMATCH", 0) > 0  # ja name->user, de price
    assert codes.get("BRANCH_MISMATCH", 0) > 0   # de 缺 female, ja 缺 =0/one
    # de 缺 other 等（cart.items de 有 other；invite de 缺 female）
    # 排序确定性校验
    keys = [(i["locale"], i["key"], i["pos"], i["code"], i.get("sort_tail") or i["detail"])
            for i in iss["issues"]]
    assert keys == sorted(keys)

    # 4) 回退图
    g = client.get(f"/api/revisions/{rev1}/graph").json()
    st = {n["locale"]: n["status"] for n in g["nodes"]}
    assert st["en"] == "covered"
    assert all(st[l] == "covered" for l in ("fr", "de", "ja"))

    # 5) 应用修复：删除 fr 的多余键 + 给 de 补缺失键（upsert）
    fix_delete = next(i for i in iss["issues"]
                      if i["code"] == "EXTRA_KEY" and i["locale"] == "fr"
                      and i["key"] == "frenchOnly")
    fix_missing_de = next(i for i in iss["issues"]
                          if i["code"] == "MISSING_KEY" and i["locale"] == "de"
                          and i["key"] == "untranslatedNote")
    m = client.post("/api/merge", json={
        "base_rev": rev1,
        "fixes": [fix_delete["fix"], fix_missing_de["fix"]]})
    assert m.status_code == 200, m.text
    rev2 = m.json()["rev"]
    assert rev2 != rev1
    assert client.get("/api/state").json()["current_rev"] == rev2

    # 6) 差异报告
    d = client.get(f"/api/diff?from_rev={rev1}&to_rev={rev2}").json()
    assert ("fr", "frenchOnly") in {(x["locale"], x["key"]) for x in d["removed"]}
    assert ("de", fix_missing_de["key"]) in \
        {(x["locale"], x["key"]) for x in d["added"]}

    # 7) 新版本问题减少
    iss2 = client.get(f"/api/revisions/{rev2}/issues").json()
    total1 = iss["counts"]["error"] + iss["counts"]["warning"]
    total2 = iss2["counts"]["error"] + iss2["counts"]["warning"]
    assert total2 < total1

    # 8) 审计
    aud = client.get("/api/audit").json()["records"]
    actions = [a["action"] for a in aud]
    assert "merge" in actions and "import" in actions
    merge_rec = next(a for a in aud if a["action"] == "merge")
    assert merge_rec["from_rev"] == rev1 and merge_rec["to_rev"] == rev2
    assert len(merge_rec["detail"]["applied"]) == 2


def test_merge_conflict_when_version_changed(client):
    # 用 JSON 接口导入一组新的内容制造分叉版本
    body = {
        "base_locale": "en",
        "files": [
            {"filename": "en.json", "content": json.dumps({"a": "A"})},
            {"filename": "fr.json", "content": json.dumps({"a": "Afr"})},
        ],
        "parents": {"fr": "en"},
    }
    r = client.post("/api/import-json", json=body)
    assert r.status_code == 200, r.text
    base = r.json()["rev"]

    # 先合并一次产生 revB，使 current != base
    m1 = client.post("/api/merge", json={
        "base_rev": base,
        "fixes": [{"op": "upsert", "locale": "fr", "key": "a", "value": "AFR2"}]})
    assert m1.status_code == 200

    # 再基于过期的 base 合并 -> 409
    m2 = client.post("/api/merge", json={
        "base_rev": base,
        "fixes": [{"op": "upsert", "locale": "fr", "key": "a", "value": "AFR3"}]})
    assert m2.status_code == 409


def test_invalid_patch_is_rejected_atomically(client):
    body = {
        "base_locale": "en",
        "files": [
            {"filename": "en.json", "content": json.dumps({"a": "{x}"})},
            {"filename": "fr.json", "content": json.dumps({"a": "{y}"})},
        ],
        "parents": {"fr": "en"},
    }
    rev = client.post("/api/import-json", json=body).json()["rev"]
    # 非法区间补丁
    r = client.post("/api/merge", json={
        "base_rev": rev,
        "fixes": [{"op": "text_patch", "locale": "fr", "key": "a",
                   "patches": [{"start": 0, "end": 99, "replacement": "z"}]}]})
    assert r.status_code == 400
    # 当前版本未变（原子性）
    assert client.get("/api/state").json()["current_rev"] == rev


def test_nested_branch_patch_follows_path(client):
    """外层 plural 内嵌套 select 缺分支时，补丁按变量/分支路径精确定位。"""
    body = {
        "base_locale": "en",
        "files": [
            {"filename": "en.json", "content": json.dumps({"k":
                "{n, plural, one {{g, select, male {his} female {her} other {their}}} other {many}}"})},
            {"filename": "fr.json", "content": json.dumps({"k":
                "{n, plural, one {{g, select, male {son} other {leur}}} other {bcp}}"})},
        ],
        "parents": {"fr": "en"},
    }
    rev = client.post("/api/import-json", json=body).json()["rev"]
    iss = client.get(f"/api/revisions/{rev}/issues").json()
    br = [i for i in iss["issues"]
          if i["code"] == "BRANCH_MISMATCH" and i["locale"] == "fr"]
    assert len(br) == 1
    assert br[0]["var_path"] == [["v", "n"], ["c", "one"], ["v", "g"]]
    fx = br[0]["fix"]
    assert fx["op"] == "text_patch"
    r = client.post("/api/merge", json={"base_rev": rev, "fixes": [fx]})
    assert r.status_code == 200, r.text
    new_rev = r.json()["rev"]
    val = client.get(f"/api/revisions/{new_rev}/entries/fr").json()["entries"]["k"]
    assert "female {her}" in val
    assert client.get(f"/api/revisions/{new_rev}/issues").json()["total"] == 0


def test_import_bad_base_locale(client):
    r = client.post("/api/import-json", json={
        "base_locale": "zz",
        "files": [{"filename": "en.json", "content": '{"a":"A"}'}],
    })
    assert r.status_code == 400


def test_branch_text_patch_auto_applies(client):
    """select 多余分支的删除补丁可自动应用并保持可解析。"""
    body = {
        "base_locale": "en",
        "files": [
            {"filename": "en.json",
             "content": json.dumps({"k": "{g, select, male {m} other {o}}"})},
            {"filename": "fr.json",
             "content": json.dumps({"k": "{g, select, male {m} female {f} other {o}}"})},
        ],
        "parents": {"fr": "en"},
    }
    rev = client.post("/api/import-json", json=body).json()["rev"]
    iss = client.get(f"/api/revisions/{rev}/issues").json()
    fx = next(i["fix"] for i in iss["issues"]
              if i["code"] == "BRANCH_MISMATCH" and i["fix"]["op"] == "text_patch")
    assert fx["auto"] is True
    m = client.post("/api/merge", json={"base_rev": rev, "fixes": [fx]})
    assert m.status_code == 200, m.text
    new_rev = m.json()["rev"]
    entries = client.get(f"/api/revisions/{new_rev}/entries/fr").json()["entries"]
    assert "female" not in entries["k"]
