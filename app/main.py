"""FastAPI 入口：本地化资源契约审查台。"""
from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import db as dbmod
from . import service

DB_PATH = os.environ.get("LRC_DB_PATH", "/data/lrc.db")

_conn = None


def conn():
    global _conn
    if _conn is None:
        _conn = dbmod.connect(DB_PATH)
        dbmod.init_db(_conn)
    return _conn


STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    conn()
    yield


app = FastAPI(title="本地化资源契约审查台", version="1.0.0", lifespan=lifespan)


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC_DIR, "index.html"))


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ---------------------------------------------------------------- 导入

@app.post("/api/import")
async def import_resources(
    base_locale: str = Form(...),
    label: str = Form(""),
    parents_json: str = Form(""),
    locales_json: str = Form(""),
    files: list[UploadFile] = File(...),
):
    """上传 JSON/ARB 资源。

    parents_json: {"fr": "en", "de": "en"} 显式父语言链（优先级高于文件元数据）。
    locales_json: {"app_fr.json": "fr"} 显式文件名->语言标识。
    """
    try:
        parents_form = json.loads(parents_json) if parents_json else {}
        locales_map = json.loads(locales_json) if locales_json else {}
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"JSON 参数格式错误: {e}")
    if not isinstance(parents_form, dict) or not isinstance(locales_map, dict):
        raise HTTPException(400, "parents_json/locales_json 必须是对象")
    parents_form["__locales__"] = locales_map

    payload_files = []
    for f in sorted(files, key=lambda x: x.filename or ""):
        raw = await f.read()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(400, f"{f.filename}: 不是 UTF-8 文本")
        payload_files.append({"filename": f.filename, "content": content})

    try:
        result = service.import_snapshot(
            conn(), base_locale=base_locale, files=payload_files,
            parents_form=parents_form, label=label)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


class ImportJsonFile(BaseModel):
    filename: str
    content: str


class ImportJsonBody(BaseModel):
    base_locale: str
    files: list[ImportJsonFile]
    parents: dict[str, Optional[str]] = Field(default_factory=dict)
    locales: dict[str, str] = Field(default_factory=dict)
    label: str = ""


@app.post("/api/import-json")
def import_json(body: ImportJsonBody):
    """等价的 JSON 接口（便于脚本调用与测试）。"""
    parents_form = dict(body.parents)
    parents_form["__locales__"] = dict(body.locales)
    try:
        return service.import_snapshot(
            conn(), base_locale=body.base_locale,
            files=[{"filename": f.filename, "content": f.content}
                   for f in sorted(body.files, key=lambda f: f.filename)],
            parents_form=parents_form, label=body.label)
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- 版本与问题

@app.get("/api/state")
def get_state():
    rev = dbmod.get_current_rev(conn())
    return {"current_rev": rev}


@app.get("/api/revisions")
def revisions():
    return {"revisions": dbmod.list_revisions(conn())}


@app.get("/api/revisions/{rev}")
def revision_detail(rev: str):
    snap = dbmod.load_revision(conn(), rev)
    if snap is None:
        raise HTTPException(404, "版本不存在")
    meta = snap["meta"]
    return {
        "rev": meta["rev"], "base_locale": meta["base_locale"],
        "created_at": meta["created_at"], "label": meta["label"],
        "input_hash": meta["input_hash"],
        "parents": snap["parents"],
        "locales": [
            {"locale": loc, "filename": b["filename"],
             "key_count": len(b["entries"]),
             "invalid_count": len(b["invalid"]),
             "parse_error": b.get("parse_error")}
            for loc, b in sorted(snap["bundles"].items())],
    }


@app.get("/api/revisions/{rev}/issues")
def revision_issues(rev: str, severity: Optional[str] = None,
                    code: Optional[str] = None, locale: Optional[str] = None):
    snap = dbmod.load_revision(conn(), rev)
    if snap is None:
        raise HTTPException(404, "版本不存在")
    result = service.analyze_snapshot(snap)
    issues = [i.to_dict() for i in result.issues]
    if severity:
        issues = [i for i in issues if i["severity"] == severity]
    if code:
        issues = [i for i in issues if i["code"] == code]
    if locale:
        issues = [i for i in issues if i["locale"] == locale]
    counts = {"error": 0, "warning": 0}
    by_code: dict[str, int] = {}
    for i in result.issues:
        counts[i.severity] = counts.get(i.severity, 0) + 1
        by_code[i.code] = by_code.get(i.code, 0) + 1
    return {"rev": rev, "base_locale": result.base_locale,
            "total": len(issues), "counts": counts, "by_code": by_code,
            "cycles": result.cycles, "issues": issues}


@app.get("/api/revisions/{rev}/graph")
def revision_graph(rev: str):
    snap = dbmod.load_revision(conn(), rev)
    if snap is None:
        raise HTTPException(404, "版本不存在")
    result = service.analyze_snapshot(snap)
    nodes = []
    for loc in sorted(snap["bundles"]):
        chain, reason = result.graph.chain(loc)
        nodes.append({
            "locale": loc,
            "parent": result.graph.parents.get(loc),
            "is_base": loc == result.base_locale,
            "chain": chain,
            "status": ("cycle" if reason and reason.startswith("cycle")
                       else "broken" if reason
                       else ("covered" if chain[-1] == result.base_locale
                             else "unrooted")),
        })
    return {"rev": rev, "base_locale": result.base_locale,
            "cycles": result.cycles, "nodes": nodes}


@app.get("/api/revisions/{rev}/entries/{locale}")
def revision_entries(rev: str, locale: str, key: Optional[str] = None):
    snap = dbmod.load_revision(conn(), rev)
    if snap is None:
        raise HTTPException(404, "版本不存在")
    b = snap["bundles"].get(locale)
    if b is None:
        raise HTTPException(404, "语言不存在")
    entries = b["entries"]
    if key is not None:
        if key not in entries:
            raise HTTPException(404, "键不存在")
        return {"locale": locale, "key": key, "value": entries[key]}
    return {"locale": locale, "entries": entries}


# ---------------------------------------------------------------- 引用影响分析

@app.get("/api/revisions/{rev}/impact")
def revision_impact(rev: str, locale: str, key: str):
    """跨语言引用依赖图与传递影响（直接/间接、最短证据路径、循环 SCC）。"""
    try:
        return service.impact_for_target(conn(), rev, locale, key)
    except KeyError:
        raise HTTPException(404, "版本不存在")
    except LookupError as e:
        raise HTTPException(404, str(e))


class ImpactPreviewBody(BaseModel):
    locale: str
    key: str
    fixes: list[FixIn]


@app.post("/api/revisions/{rev}/impact/preview")
def revision_impact_preview(rev: str, body: ImpactPreviewBody):
    """对指定修复集做不落库的影响预演（新增断裂引用 vs 原有问题）。"""
    try:
        return service.preview_impact(
            conn(), rev=rev, locale=body.locale, key=body.key,
            fixes=[f.model_dump() for f in body.fixes])
    except KeyError:
        raise HTTPException(404, "版本不存在")
    except LookupError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))


# ---------------------------------------------------------------- 消息实例预览

class PreviewBody(BaseModel):
    key: str
    locales: list[str]
    variables: dict = Field(default_factory=dict)


@app.post("/api/revisions/{rev}/preview")
def revision_preview(rev: str, body: PreviewBody):
    """只读消息实例预览：同一组变量并排渲染多个目标语言。"""
    if not body.locales:
        raise HTTPException(400, "locales 不能为空")
    if not body.key:
        raise HTTPException(400, "key 不能为空")
    try:
        return service.preview_messages(
            conn(), rev=rev, key=body.key,
            locales=list(dict.fromkeys(body.locales)),  # 去重保序
            variables=body.variables)
    except KeyError:
        raise HTTPException(404, "版本不存在")


# ---------------------------------------------------------------- 自包含语言包编译

class PackBody(BaseModel):
    locales: list[str]
    keys: list[str]


def _compile_pack(rev: str, locales: list[str], keys: list[str]) -> dict:
    from . import pack as packmod
    try:
        return service.compile_language_pack(
            conn(), rev=rev, locales=locales, keys=keys)
    except KeyError:
        raise HTTPException(404, "版本不存在")
    except ValueError as e:
        raise HTTPException(400, str(e))
    except packmod.PackBlocked as blocked:
        raise HTTPException(422, blocked.payload(rev,
                            service.revision_summary(conn(), rev)))


@app.post("/api/revisions/{rev}/pack")
def revision_pack_compile(rev: str, body: PackBody):
    """编译自包含语言包：返回清单、逐键来源与模板（不回传 ZIP 二进制）。

    断裂引用 / 引用环 / 语法错误 / 入口不可解析时返回 422 与全部错误
    （携带语言、入口键与引用路径），整包不生成。
    """
    result = _compile_pack(rev, body.locales, body.keys)
    result.pop("_package_bytes", None)
    return result


@app.get("/api/revisions/{rev}/pack/download")
def revision_pack_download(
        rev: str,
        locales: list[str] = Query(default=..., description="目标语言（可重复）"),
        keys: list[str] = Query(default=..., description="入口键（可重复）")):
    """与 POST 等价的确定性下载入口：相同输入逐字节相同（固定 ZIP 元数据）。"""
    result = _compile_pack(rev, locales, keys)
    package = result.pop("_package_bytes")
    return Response(
        content=package, media_type="application/zip",
        headers={"Content-Disposition":
                 f'attachment; filename="{result["package"]["filename"]}"'})


# ---------------------------------------------------------------- 资源键重命名迁移

class RenameMapping(BaseModel):
    old_key: str
    new_key: str


class RenameDryRunBody(BaseModel):
    mappings: list[RenameMapping]


class RenameCommitBody(BaseModel):
    expected_rev: str
    mappings: list[RenameMapping]
    actor: str = "user"


def _rename_pairs(body) -> list[dict]:
    return [{"old_key": m.old_key, "new_key": m.new_key} for m in body.mappings]


@app.post("/api/revisions/{rev}/rename/plan")
def rename_plan(rev: str, body: RenameDryRunBody):
    """试运行资源键重命名：完整改写计划、受影响引用与断裂风险（不写库）。"""
    from . import rename as renamemod
    try:
        return service.rename_dry_run(
            conn(), rev=rev, mappings=_rename_pairs(body))
    except renamemod.RenameValidationError as e:
        raise HTTPException(400, {"errors": e.errors,
                                  "message": "重命名映射校验失败"})
    except KeyError:
        raise HTTPException(404, "版本不存在")


@app.post("/api/revisions/{rev}/rename/commit")
def rename_commit(rev: str, body: RenameCommitBody):
    """确认迁移：携带 expected_rev，在单事务内改写资源键与引用，生成新版本。

    expected_rev 过期返回 409；映射非法返回 400（结构化错误）。
    已完成的迁移按（源版本 rev, 规范化计划摘要）识别：即使首次确认已把
    current_rev 推进到新版本，客户端用原 rev、原 expected_rev 与同一映射
    重试也会在事务内核对产物与审计后**回放**首次结果（200，replayed=true），
    不新建版本、不追加审计、不切换当前指针；尚无成功记录的过期请求仍为 409。
    """
    from . import rename as renamemod
    try:
        return service.rename_commit(
            conn(), rev=rev, expected_rev=body.expected_rev,
            mappings=_rename_pairs(body), actor=body.actor)
    except renamemod.RenameValidationError as e:
        raise HTTPException(400, {"errors": e.errors,
                                  "message": "重命名映射校验失败"})
    except service.ConflictError as e:
        raise HTTPException(409, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))


# ---------------------------------------------------------------- 差异

@app.get("/api/diff")
def diff(from_rev: str, to_rev: str):
    try:
        return service.diff_snapshots(conn(), from_rev, to_rev)
    except KeyError:
        raise HTTPException(404, "版本不存在")


# ---------------------------------------------------------------- 合并

class FixIn(BaseModel):
    op: str
    locale: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    parent: Optional[str] = None
    patches: Optional[list[dict]] = None
    reason: Optional[str] = None


class MergeBody(BaseModel):
    base_rev: str
    fixes: list[FixIn]
    actor: str = "user"


@app.post("/api/merge")
def merge(body: MergeBody):
    try:
        result = service.apply_merge(
            conn(), base_rev=body.base_rev,
            fixes=[f.model_dump() for f in body.fixes], actor=body.actor)
    except service.ConflictError as e:
        raise HTTPException(409, str(e))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return result


# ---------------------------------------------------------------- 审计

@app.get("/api/audit")
def audit(limit: int = 100):
    return {"records": dbmod.list_audit(conn(), limit=min(limit, 500))}


@app.get("/api/health")
def health():
    return {"status": "ok"}
