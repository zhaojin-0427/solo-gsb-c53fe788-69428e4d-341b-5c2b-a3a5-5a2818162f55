"""自包含语言包编译：沿显式父语言链物化条目并把 ``{@key}`` 引用就地展开。

目标
----
用户选定不可变资源版本、目标语言集合与入口键集合后，编译器产出一个 ZIP：
每个目标语言一个 JSON（键 → 已展开引用的 ICU 模板）加一份 ``manifest.json``。
导出的消息不再包含任何 ``{@key}``，因此消费方**无需原数据库、无需父语言链**
即可直接使用（键已就地存在于对应语言文件中）。

解析语义（与 :mod:`app.preview` 完全一致）
-----------------------------------------
- 入口消息先沿目标语言的显式父语言链解析（本语言优先、逐级向上）确定实际来源；
- 消息内的 ``{@key}`` 从**引用所在消息的语言**继续沿其父链解析（近遮蔽优先），
  被引用消息以同样方式递归展开；
- 展开保留全部 ICU 结构语义：变量（含类型与样式）、plural/select/selectordinal
  分支、嵌套占位符、``#`` 数量符号、单引号转义。

``#`` 的关键处理：被引用消息在其自身顶层的 ``#`` 只是普通文本；一旦它被内联进
外层 plural 分支体，裸 ``#`` 会被重新解释为数量符号。序列化器因此携带
``plural_ctx``——在复数上下文中展开引用时，对被引用消息顶层的结构性字符
（``#``、``{``、``}``）重新做 ICU 引号转义；而被引用消息**自身** plural 分支
体内的 ``#`` 始终绑定其最内层参数，原样保留。

失败语义
--------
断裂引用、引用环、入口/被引用消息 ICU 语法错误、入口沿链不可解析、目标语言
不存在等**任一条目**出错都阻止整包生成（不输出任何文件字节），错误携带
语言、入口键与结构化引用路径（``@key`` 序列）。一次编译内的全部错误都会被
收集并按稳定顺序返回。

确定性
------
输入去重后排序；JSON 以 ``ensure_ascii=False、sort_keys=True、indent=2``
规范化并以 ``\\n`` 结尾；ZIP 条目按路径排序、固定时间戳/权限/压缩参数。
相同输入（同一不可变版本 + 相同语言与键集合）重复编译得到逐字节相同的结果。

整个编译只读：不写快照、不写审计、不切换 current_rev。
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

from .analyzer import AnalysisResult, ParsedBundle
from .icu import IcuError, parse_message
from .impact import resolution_chain
from .preview import _unquote

# ---------------------------------------------------------------- 错误码

LOCALE_NOT_FOUND = "LOCALE_NOT_FOUND"
ENTRY_NOT_FOUND = "ENTRY_NOT_FOUND"
BROKEN_REFERENCE = "BROKEN_REFERENCE"
REFERENCE_CYCLE = "REFERENCE_CYCLE"
INVALID_MESSAGE = "INVALID_MESSAGE"
INVALID_LOCALE_ID = "INVALID_LOCALE_ID"

_LOCALE_ID_RE = re.compile(r"[A-Za-z0-9_-]+")

MANIFEST_VERSION = 1
PACKAGE_MEDIA_TYPE = "application/zip"


class PackError(Exception):
    """单条目编译错误。locale/key 始终标识所请求的入口；path 为 @key 序列。"""

    def __init__(self, code: str, locale: str, key: str, detail: str,
                 path: Optional[list[str]] = None, extra: Optional[dict] = None):
        super().__init__(detail)
        self.code = code
        self.locale = locale
        self.key = key
        self.detail = detail
        self.path = path or []
        self.extra = extra or {}

    def to_dict(self) -> dict:
        d = {"code": self.code, "locale": self.locale, "key": self.key,
             "detail": self.detail, "path": list(self.path)}
        d.update(self.extra)
        return d


class PackBlocked(Exception):
    """整包被阻止：携带全部条目错误（稳定排序）。"""

    def __init__(self, errors: list[PackError]):
        self.errors = errors
        super().__init__(f"语言包编译被 {len(errors)} 个错误阻止")

    def payload(self, rev: str, revision: Optional[dict]) -> dict:
        return {"ok": False, "rev": rev, "revision": revision,
                "errors": [e.to_dict() for e in self.errors]}


# ---------------------------------------------------------------- 编译上下文

Node = tuple[str, str]


@dataclass
class _Ctx:
    analysis: AnalysisResult
    target_locale: str
    root_key: str
    stack: list[Node] = field(default_factory=list)   # 当前展开的消息链（检环）
    path: list[str] = field(default_factory=list)     # @key 序列（结构化路径）
    hops: list[dict] = field(default_factory=list)
    sources: list[Node] = field(default_factory=list)
    seen: set[Node] = field(default_factory=set)

    @property
    def bundles(self) -> dict[str, ParsedBundle]:
        return self.analysis.bundles


# ---------------------------------------------------------------- 原文再转义

def _escape_literal(text: str, plural_ctx: bool) -> str:
    """把已去义的字面文本重新编码为合法 ICU 字面量。

    - 单引号一律按 ICU 规则双写（``'`` → ``''``）；
    - ``{`` / ``}`` 永远是结构性字符，必须放入引号段；
    - plural_ctx 下 ``#`` 也是结构性字符；
    - 引号段尽量延展到同一「词」（空白/引号之前）内的最后一个特殊字符，
      使 ``{x}`` 这类原文输出为 ``'{x}'`` 而非 ``'{'x'}'``——两者语义相同，
      前者更可读。引号段内绝不包含单引号（单引号走双写分支），故延展安全。
    """
    special = set("{}#") if plural_ctx else set("{}")
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        c = text[i]
        if c == "'":
            out.append("''")
            i += 1
            continue
        if c in special:
            j = i
            last = i          # 本词内最后一个特殊字符的位置
            while j < n and text[j] not in "' \t\r\n":
                if text[j] in special:
                    last = j
                j += 1
            out.append("'" + text[i:last + 1] + "'")
            i = last + 1      # 词内剩余普通字符回到主循环原样输出
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------- 模板序列化

def _resolve(ctx: _Ctx, locale: str, key: str) -> tuple[Optional[str], list[str]]:
    """沿 locale 的显式父链解析 key（本语言优先）。与 preview._resolve 同构。"""
    chain = resolution_chain(ctx.analysis.graph, locale)
    for loc in chain:
        b = ctx.bundles.get(loc)
        if b is not None and key in b.entries:
            return loc, chain
    return None, chain


def _serialize_shape(shape, src: str, *, plural_ctx: bool, ctx: _Ctx) -> str:
    parts: list[str] = []
    for kind, *rest in shape.items:
        if kind == "text":
            start, end = rest
            parts.append(_escape_literal(_unquote(src[start:end]), plural_ctx))
        elif kind == "hash":
            # 解析器只在 plural/selectordinal 分支体内产出 hash 事件
            parts.append("#")
        else:
            v = rest[0]
            if v.is_ref:
                parts.append(_expand_ref(v, plural_ctx=plural_ctx, ctx=ctx))
            else:
                parts.append(_serialize_arg(v, src=src, ctx=ctx))
    return "".join(parts)


def _serialize_arg(v, *, src: str, ctx: _Ctx) -> str:
    if v.type in ("plural", "selectordinal", "select"):
        # plural/selectordinal 分支体内 # 为数量符号；select 分支体则不是
        inner_plural = v.type in ("plural", "selectordinal")
        cases = " ".join(
            f"{c.label} {{{_serialize_shape(c.body, src, plural_ctx=inner_plural, ctx=ctx)}}}"
            for c in v.cases)
        return f"{{{v.name}, {v.type}, {cases}}}"
    if v.style is not None:
        return f"{{{v.name}, {v.type}, {v.style}}}"
    if v.type:
        return f"{{{v.name}, {v.type}}}"
    return f"{{{v.name}}}"


def _expand_ref(v, *, plural_ctx: bool, ctx: _Ctx) -> str:
    """展开单个 {@key}：从引用所在消息（stack 顶）的语言继续沿父链解析。"""
    ref_key = v.name
    from_locale, from_key = ctx.stack[-1]
    ref_path = ctx.path + [f"@{ref_key}"]

    loc, chain = _resolve(ctx, from_locale, ref_key)
    if loc is None:
        raise PackError(
            BROKEN_REFERENCE, ctx.target_locale, ctx.root_key,
            f"引用 {{@{ref_key}}} 沿父链无法解析（链：{' -> '.join(chain)}）",
            ref_path, extra={"ref_key": ref_key, "chain": list(chain),
                             "from": {"locale": from_locale, "key": from_key}})

    node = (loc, ref_key)
    if node in ctx.stack:
        start = ctx.stack.index(node)
        cycle = [f"{l}:{k}" for l, k in ctx.stack[start:]]
        cycle.append(f"{loc}:{ref_key}")
        raise PackError(
            REFERENCE_CYCLE, ctx.target_locale, ctx.root_key,
            f"检测到引用环：{' -> '.join(cycle)}", ref_path,
            extra={"ref_key": ref_key, "cycle": cycle,
                   "from": {"locale": from_locale, "key": from_key}})

    bundle = ctx.bundles[loc]
    shape = bundle.shapes.get(ref_key)
    if shape is None:
        err = bundle.syntax_errors.get(ref_key)
        detail = f"被引用消息 {loc}:{ref_key} 存在 ICU 语法错误"
        extra = {"ref_key": ref_key,
                 "message_locale": loc,
                 "from": {"locale": from_locale, "key": from_key}}
        if err is not None:
            detail += f"：{err.message}（位置 {err.pos}）"
            extra["message"] = err.message
            extra["pos"] = err.pos
        raise PackError(INVALID_MESSAGE, ctx.target_locale, ctx.root_key,
                        detail, ref_path, extra=extra)

    ctx.hops.append({
        "ref_key": ref_key,
        "from": {"locale": from_locale, "key": from_key},
        "to": {"locale": loc, "key": ref_key},
        "chain": list(chain),
    })

    is_new = node not in ctx.seen
    if is_new:
        ctx.seen.add(node)
        ctx.sources.append(node)
    ctx.stack.append(node)
    ctx.path = ref_path
    try:
        return _serialize_shape(
            shape, bundle.entries[ref_key], plural_ctx=plural_ctx, ctx=ctx)
    finally:
        ctx.stack.pop()
        ctx.path = ref_path[:-1]


# ---------------------------------------------------------------- 单条目

def _compile_entry(analysis: AnalysisResult, target_locale: str,
                   key: str) -> dict:
    """编译一个 (目标语言, 入口键)。失败抛 PackError，不产生部分结果。"""
    ctx = _Ctx(analysis=analysis, target_locale=target_locale, root_key=key)

    if target_locale not in analysis.bundles:
        raise PackError(
            LOCALE_NOT_FOUND, target_locale, key,
            f"目标语言 {target_locale!r} 不在该版本中",
            extra={"available": sorted(analysis.bundles)})

    loc, chain = _resolve(ctx, target_locale, key)
    if loc is None:
        raise PackError(
            ENTRY_NOT_FOUND, target_locale, key,
            f"消息键 {key!r} 沿父链无法解析（链：{' -> '.join(chain)}）",
            extra={"chain": list(chain)})

    bundle = analysis.bundles[loc]
    shape = bundle.shapes.get(key)
    if shape is None:
        err = bundle.syntax_errors.get(key)
        detail = f"消息 {loc}:{key} 存在 ICU 语法错误"
        extra = {"resolved_locale": loc, "chain": list(chain),
                 "message_locale": loc}
        if err is not None:
            detail += f"：{err.message}（位置 {err.pos}）"
            extra["message"] = err.message
            extra["pos"] = err.pos
        raise PackError(INVALID_MESSAGE, target_locale, key, detail,
                        extra=extra)

    root: Node = (loc, key)
    ctx.stack.append(root)
    ctx.seen.add(root)
    ctx.sources.append(root)
    template = _serialize_shape(
        shape, bundle.entries[key], plural_ctx=False, ctx=ctx)

    # 防御性校验：产物必须仍是合法 ICU 且不再携带任何引用
    try:
        reparsed = parse_message(template)
    except IcuError as e:
        raise PackError(
            INVALID_MESSAGE, target_locale, key,
            f"展开后的模板存在 ICU 语法错误：{e.message}（位置 {e.pos}）",
            extra={"resolved_locale": loc, "pos": e.pos})
    if any(v.is_ref for v, _ in reparsed.walk_vars()):
        raise PackError(
            INVALID_MESSAGE, target_locale, key,
            "展开后的模板仍包含未展开的 {@key} 引用（内部错误）",
            extra={"resolved_locale": loc})

    return {
        "template": template,
        "provenance": {
            "source": {"locale": loc, "fell_back": loc != target_locale,
                       "fallback_chain": list(chain)},
            "sources": [{"locale": l, "key": k} for l, k in ctx.sources],
            "hops": list(ctx.hops),
        },
    }


# ---------------------------------------------------------------- 规范化输出

def canonical_json_bytes(obj) -> bytes:
    """确定性 JSON：键排序、非 ASCII 原样、两格缩进、行尾换行。"""
    return (json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n").encode("utf-8")


def _zip_info(path: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(path, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    info.create_system = 3  # UNIX：固定宿主类型，避免跨平台字节差异
    return info


def build_zip(files: list[tuple[str, bytes]]) -> bytes:
    """files: (zip 内路径, 字节)。按路径排序、固定元数据写入。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for path, data in sorted(files):
            z.writestr(_zip_info(path), data)
    return buf.getvalue()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 整包

def compile_pack(analysis: AnalysisResult, meta: dict,
                 locales: list[str], keys: list[str]) -> dict:
    """编译自包含语言包。

    meta: revision 元信息（rev/base_locale/label/input_hash/created_at）。
    任一条目出错抛 :class:`PackBlocked`；成功返回清单、模板与 ZIP 字节。
    """
    locales = sorted(set(locales))
    keys = sorted(set(keys))

    templates: dict[str, dict[str, str]] = {loc: {} for loc in locales}
    provenance: dict[str, dict[str, dict]] = {loc: {} for loc in locales}
    errors: list[PackError] = []

    for loc in locales:
        for key in keys:
            try:
                out = _compile_entry(analysis, loc, key)
            except PackError as e:
                errors.append(e)
                continue
            templates[loc][key] = out["template"]
            provenance[loc][key] = out["provenance"]

    # 语言标识需要能安全地作为 ZIP 内文件名
    for loc in locales:
        if not _LOCALE_ID_RE.fullmatch(loc):
            errors.append(PackError(
                INVALID_LOCALE_ID, loc, "",
                f"语言标识 {loc!r} 含非法字符（仅允许字母、数字、_、-），"
                "无法作为包内文件名"))

    if errors:
        errors.sort(key=lambda e: (e.locale, e.key, e.code,
                                   tuple(e.path), e.detail))
        raise PackBlocked(errors)

    rev = meta["rev"]
    file_records: list[dict] = []
    zip_files: list[tuple[str, bytes]] = []
    for loc in locales:
        path = f"{loc}.json"
        data = canonical_json_bytes(templates[loc])
        zip_files.append((path, data))
        file_records.append({"path": path, "locale": loc,
                             "bytes": len(data), "sha256": _sha256(data)})
    file_records.sort(key=lambda f: f["path"])

    manifest = {
        "manifest_version": MANIFEST_VERSION,
        "revision": {
            "rev": rev,
            "base_locale": meta["base_locale"],
            "label": meta.get("label", ""),
            "input_hash": meta["input_hash"],
            "created_at": meta["created_at"],
        },
        "compilation": {"locales": locales, "entry_keys": keys},
        "entries": provenance,
        "files": file_records,
    }
    manifest_bytes = canonical_json_bytes(manifest)
    zip_files.append(("manifest.json", manifest_bytes))
    package = build_zip(zip_files)
    filename = f"lrc-pack-{rev}.zip"

    return {
        "ok": True,
        "rev": rev,
        "locales": locales,
        "keys": keys,
        "templates": templates,
        "provenance": provenance,
        "manifest": manifest,
        "manifest_bytes": len(manifest_bytes),
        "files": file_records,
        "package": {"filename": filename, "media_type": PACKAGE_MEDIA_TYPE,
                    "bytes": len(package), "sha256": _sha256(package)},
        "_package_bytes": package,
    }
