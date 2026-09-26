"""资源键重命名迁移：纯逻辑（试运行计划 + 内存物化）。

语义约定
--------
- 重命名是**全局键空间**操作：old_key 在所有语言包中出现的条目（含非字符串
  invalid 叶子）都改名为 new_key；某语言缺失该键是正常的回退现象，不算风险。
- 校验（一次返回全部错误，稳定排序）：
  - 源键必须在资源键空间中存在（至少一个语言拥有）；
  - 目标键不得与**未迁移键**冲突（new_key ∈ 现存键 − old 键集合），否则要么
    同语言字典撞键，要么改变近遮蔽回退落点；
  - 映射一对一：old_key、new_key 均不可重复（完全相同的映射对视为冗余去重）；
  - 禁止链式歧义：new_key 不得同时是另一条映射的 old_key（拒绝 a→b,b→c
    与 a→b,b→a 之类的链/环，引用改写只允许“一跳到位”）。
- ICU 引用 ``{@key}`` 的改写严格按**每条引用所在语言的显式回退链**解析
  （本语言优先、逐级向上，与影响分析/预览/语言包编译共用同一规则）：
  只有当前解析成功的引用才会被改写；回退链上的遮蔽保证改名后仍解析到改名后
  的同一条消息，因此**不会引入新的断裂引用**。
- 无法可靠提取引用的消息（ICU 语法错误）其原文一律不动，仅把 best-effort
  扫描到的 ``{@old_key}`` 列为风险，提示人工处理；原本就断裂的引用同理。

试运行结果只依赖（快照、有序映射对），不写库；提交由服务层在单一事务中
物化并生成新不可变版本。
"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from .analyzer import AnalysisResult
from .icu import parse_message
from .impact import build_ref_graph

# 校验错误码
E_EMPTY_PLAN = "EMPTY_PLAN"
E_BAD_MAPPING = "BAD_MAPPING"
E_SELF_RENAME = "SELF_RENAME"
E_SOURCE_NOT_FOUND = "SOURCE_NOT_FOUND"
E_DUPLICATE_SOURCE = "DUPLICATE_SOURCE"
E_DUPLICATE_TARGET = "DUPLICATE_TARGET"
E_TARGET_CONFLICT = "TARGET_CONFLICT"
E_CHAIN_AMBIGUOUS = "CHAIN_AMBIGUOUS"
E_INVALID_KEY_CHAR = "INVALID_KEY_CHAR"

# 风险类型
R_BROKEN_REFERENCE = "BROKEN_REFERENCE"
R_UNPARSEABLE_REFERENCE = "UNPARSEABLE_REFERENCE"
R_NEW_BROKEN = "NEW_BROKEN_REFERENCE"

# 引用名允许的字符（与 icu._Parser 的 name 词法保持一致，去掉开头的 @）
_REF_NAME_CHARS = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")


class RenameValidationError(Exception):
    """映射未通过全部校验；errors 为稳定排序后的结构化错误列表。"""

    def __init__(self, errors: list[dict]):
        self.errors = errors
        super().__init__(f"重命名映射校验失败（{len(errors)} 个错误）")


# ---------------------------------------------------------------- 规范化与校验

def normalize_mappings(mappings: list[dict]) -> list[dict]:
    """把提交项规范为有序 [{old_key, new_key}]：

    - 每项必须是带非空字符串 old_key/new_key 的对象（否则结构化报错）；
    - 完全相同的映射对只保留第一次出现（与提交顺序一致的确定性去重）。
    """
    if not isinstance(mappings, list):
        raise RenameValidationError([{
            "code": E_BAD_MAPPING, "index": -1,
            "detail": "mappings 必须是数组"}])
    norm: list[dict] = []
    errors: list[dict] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, m in enumerate(mappings):
        if not isinstance(m, dict):
            errors.append({"code": E_BAD_MAPPING, "index": i,
                           "detail": f"第 {i + 1} 项必须是对象"})
            continue
        old, new = m.get("old_key"), m.get("new_key")
        if not isinstance(old, str) or not isinstance(new, str) or not old or not new:
            errors.append({"code": E_BAD_MAPPING, "index": i,
                           "old_key": old if isinstance(old, str) else "",
                           "new_key": new if isinstance(new, str) else "",
                           "detail": f"第 {i + 1} 项的 old_key/new_key 必须是非空字符串"})
            continue
        pair = (old, new)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        norm.append({"old_key": old, "new_key": new})

    if not norm and not errors:
        errors.append({"code": E_EMPTY_PLAN,
                       "detail": "映射列表为空（至少需要一条 old_key→new_key）"})
    if errors:
        raise RenameValidationError(_sort_errors(errors))
    return norm


def _valid_ref_name(key: str) -> bool:
    return bool(key) and key[0] != "@" and all(c in _REF_NAME_CHARS for c in key)


def _sort_errors(errors: list[dict]) -> list[dict]:
    return sorted(errors, key=lambda e: (
        e["code"], e.get("old_key") or "", e.get("new_key") or "",
        e.get("index", -1)))


def validate_mappings(mappings: list[dict],
                      key_locales: dict[str, list[str]]) -> list[dict]:
    """全部业务校验；返回结构化错误列表（空列表表示通过）。

    key_locales: 资源键空间中每个键当前存在的语言（有序）。
    """
    errors: list[dict] = []
    existing = set(key_locales)
    olds = [m["old_key"] for m in mappings]
    news = [m["new_key"] for m in mappings]
    old_set = set(olds)

    for m0 in mappings:
        o, n = m0["old_key"], m0["new_key"]
        if o == n:
            errors.append({"code": E_SELF_RENAME, "old_key": o, "new_key": n,
                           "detail": f"old_key 与 new_key 相同：{o!r}"})
        if o not in existing:
            errors.append({"code": E_SOURCE_NOT_FOUND, "old_key": o, "new_key": n,
                           "detail": f"源键 {o!r} 在资源中不存在（任何语言都没有该键）"})
        if not _valid_ref_name(n):
            errors.append({"code": E_INVALID_KEY_CHAR, "old_key": o, "new_key": n,
                           "detail": f"目标键 {n!r} 含非法字符：仅允许字母、数字、"
                                     "'.'、'_'、'-'，且不能以 '@' 开头"})

    # 一对一
    dup_old = sorted({o for o in olds if olds.count(o) > 1})
    for o in dup_old:
        targets = sorted({m["new_key"] for m in mappings if m["old_key"] == o})
        errors.append({"code": E_DUPLICATE_SOURCE, "old_key": o,
                       "new_key": targets[0] if len(targets) == 1 else "",
                       "targets": targets,
                       "detail": f"源键 {o!r} 出现多次且目标不一致：{targets}"})
    dup_new = sorted({n for n in news if news.count(n) > 1})
    for n in dup_new:
        sources = sorted({m["old_key"] for m in mappings if m["new_key"] == n})
        errors.append({"code": E_DUPLICATE_TARGET, "new_key": n,
                       "old_key": sources[0] if len(sources) == 1 else "",
                       "sources": sources,
                       "detail": f"目标键 {n!r} 被多个源键映射：{sources}"})

    # 目标不与未迁移键冲突
    for m0 in mappings:
        o, n = m0["old_key"], m0["new_key"]
        if n in existing and n not in old_set:
            errors.append({
                "code": E_TARGET_CONFLICT, "old_key": o, "new_key": n,
                "locales": key_locales[n],
                "detail": f"目标键 {n!r} 已存在且不在迁移集合中"
                          f"（语言：{', '.join(key_locales[n])}）；改名会造成撞键或"
                          "改变近遮蔽回退落点"})

    # 链式歧义：new_key 又是另一条映射的源键。给出从本条映射开始的完整链。
    by_old = {m["old_key"]: m["new_key"] for m in mappings}
    for m0 in mappings:
        o, n = m0["old_key"], m0["new_key"]
        if n in by_old:
            chain = [o]
            cur = n
            seen = {o}
            while cur in by_old and cur not in seen:
                chain.append(cur)
                seen.add(cur)
                cur = by_old[cur]
            chain.append(cur)  # 终止节点（环的重复点或链尾）
            errors.append({
                "code": E_CHAIN_AMBIGUOUS, "old_key": o, "new_key": n,
                "chain": chain,
                "detail": f"映射链存在歧义：{' → '.join(chain)}；"
                          "请合并为单跳映射或分步迁移"})

    return _sort_errors(errors)


# ---------------------------------------------------------------- 语法错误消息的容忍扫描

def scan_refs_tolerant(src: str) -> list[tuple[int, int, str]]:
    """best-effort 扫描原始文本中的 ``{@name}``（跳过单引号片段）。

    仅用于 ICU 语法错误消息的风险提示；该消息本身不会被物化改写。
    返回 [(起始位置, '}' 位置含, 引用名)]，按位置升序。
    """
    n = len(src)
    out: list[tuple[int, int, str]] = []
    i = 0
    while i < n:
        c = src[i]
        if c == "'":
            if i + 1 < n and src[i + 1] == "'":
                i += 2
                continue
            i += 1
            while i < n:
                if src[i] == "'":
                    if i + 1 < n and src[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        if c == "{" and i + 1 < n and src[i + 1] == "@":
            j = i + 2
            start = j
            while j < n and (src[j].isalnum() or src[j] in "._@-"):
                j += 1
            name = src[start:j]
            if name and j < n and src[j] == "}":
                out.append((i, j, name))
                i = j + 1
                continue
        i += 1
    return out


# ---------------------------------------------------------------- 计划

def _key_locales(snap: dict) -> dict[str, list[str]]:
    """资源键空间：字符串条目键 ∪ 非字符串 invalid 叶子键 -> 有序语言列表。"""
    out: dict[str, set[str]] = {}
    for loc, b in snap["bundles"].items():
        for k in b["entries"]:
            out.setdefault(k, set()).add(loc)
        for inv in b.get("invalid", []):
            out.setdefault(inv["key"], set()).add(loc)
    return {k: sorted(locs) for k, locs in out.items()}


def plan_hash(mappings: list[dict]) -> str:
    """计划摘要：对有序映射对规范化求 SHA-256（相同映射集合提交顺序无关）。"""
    pairs = sorted((m["old_key"], m["new_key"]) for m in mappings)
    blob = json.dumps(pairs, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def plan_rename(snap: dict, analysis: AnalysisResult,
                raw_mappings: list[dict]) -> dict:
    """计算完整重命名改写计划（纯内存，不写库）。

    映射不合法抛 :class:`RenameValidationError`。
    """
    mappings = normalize_mappings(raw_mappings)
    key_locales = _key_locales(snap)
    errors = validate_mappings(mappings, key_locales)
    if errors:
        raise RenameValidationError(errors)

    rename_map = {m["old_key"]: m["new_key"] for m in mappings}
    old_set = set(rename_map)

    # ---- 键改名条目（按 old_key 稳定排序） ----
    key_renames: list[dict] = []
    entry_count = 0
    locales_affected: set[str] = set()
    for old in sorted(rename_map):
        locs = key_locales.get(old, [])
        entry_count += len(locs)
        locales_affected.update(locs)
        key_renames.append({
            "old_key": old, "new_key": rename_map[old],
            "locales": locs,
            "absent_locales": sorted(set(snap["bundles"]) - set(locs)),
        })

    # ---- 引用改写：沿每条引用所在语言的回退链解析（复用引用图） ----
    # 图边给出解析落点与回退链；精确 token 区间从解析形态按
    # (locale,key,pos) 对齐取得（保留空白等原片段，只替换 @name 部分）。
    spans: dict[tuple[str, str, int], tuple[int, int]] = {}
    for loc, b in analysis.bundles.items():
        for key, shape in b.shapes.items():
            for v, _path in shape.walk_vars():
                if v.is_ref:
                    spans[(loc, key, v.pos)] = (v.pos, v.arg_end_pos)

    g = build_ref_graph(analysis)
    rewrites: list[dict] = []
    broken: list[dict] = []
    for e in g.edges:
        if e.dst is None:
            broken.append({
                "kind": R_BROKEN_REFERENCE,
                "locale": e.src[0], "key": e.src[1], "ref_key": e.ref_key,
                "pos": e.pos, "positions": e.positions, "chain": e.chain,
                "renamed_source": e.ref_key in old_set,
            })
            continue
        # 只有落点键本身被迁移时才需要改写 {@old} -> {@new}
        new_ref = rename_map.get(e.ref_key)
        if new_ref is None:
            continue
        for pos in e.positions:
            start, end = spans[(e.src[0], e.src[1], pos)]
            rewrites.append({
                "locale": e.src[0], "key": e.src[1], "pos": start, "end": end,
                "old_key": e.ref_key, "new_key": new_ref,
                "resolved_locale": e.dst[0], "resolved_key": e.dst[1],
                "chain": e.chain,
            })

    rewrites.sort(key=lambda r: (r["locale"], r["key"], r["pos"], r["new_key"]))

    # ---- 语法错误消息：容忍扫描其中指向迁移键的引用（只报风险，不改写） ----
    unparseable: list[dict] = []
    for loc in sorted(analysis.bundles):
        b = analysis.bundles[loc]
        for key in sorted(b.syntax_errors):
            raw = b.entries[key]
            hits = [(p, end, name) for p, end, name in scan_refs_tolerant(raw)
                    if name in old_set]
            if not hits:
                continue
            unparseable.append({
                "kind": R_UNPARSEABLE_REFERENCE,
                "locale": loc, "key": key,
                "pos": hits[0][0],
                "positions": [p for p, _e, _n in hits],
                "ref_keys": sorted({name for _p, _e, name in hits}),
                "detail": "该消息 ICU 语法错误，迁移不会自动改写其中的引用，"
                          "请人工确认后随新版本一并修正",
            })

    # ---- 物化后重建引用图：理论上无新增断裂；出现则列入风险（fail-closed） ----
    # 注意：impact.compare_graphs 按 (源语言,源键,引用键) 匹配前后边，而本模块
    # 会重命名源键本身，键不再相等，故这里直接比较「断裂边集合」。
    bundles_after, _parents, _text_after = materialize_rename(snap, mappings, rewrites)
    virtual = {"meta": snap["meta"], "parents": snap["parents"],
               "bundles": bundles_after}
    from .service import analyze_snapshot
    after = analyze_snapshot(virtual)
    g_after = build_ref_graph(after)

    def broken_set(g_, new_to_old=None):
        # 迁移后断裂边的源消息键已被改名；比对时统一映射回旧键名，
        # 否则原本就断裂的引用会因源键改名被误判为「新增断裂」。
        out = set()
        for e in g_.edges:
            if e.dst is None:
                src_key = e.src[1]
                if new_to_old is not None:
                    src_key = new_to_old.get(src_key, src_key)
                out.add((e.src[0], src_key, e.ref_key, tuple(e.chain)))
        return out

    new_to_old = {new: old for old, new in rename_map.items()}
    broken_before_set = broken_set(g)
    broken_after_set = broken_set(g_after, new_to_old)
    new_broken: list[dict] = []
    for loc, key, ref_key, chain in sorted(broken_after_set - broken_before_set):
        new_broken.append({
            "kind": R_NEW_BROKEN, "locale": loc, "key": key,
            "ref_key": ref_key, "chain": list(chain),
            "detail": "重命名后新出现的断裂引用（理论上不应发生，请检查映射）"})

    risks = broken + unparseable + new_broken
    risks.sort(key=lambda r: (
        r["kind"], r["locale"], r["key"], r.get("pos", 0),
        r.get("ref_key") or r.get("new_key") or ""))

    messages_rewritten = {(r["locale"], r["key"]) for r in rewrites}
    hash_value = plan_hash(mappings)
    return {
        "base_locale": snap["meta"]["base_locale"],
        "mappings": mappings,
        "plan_hash": hash_value,
        "key_renames": key_renames,
        "reference_rewrites": rewrites,
        "risks": {
            "items": risks,
            "summary": {
                R_BROKEN_REFERENCE: len(broken),
                R_UNPARSEABLE_REFERENCE: len(unparseable),
                R_NEW_BROKEN: len(new_broken),
                "total": len(risks),
            },
        },
        "graph_comparison": {
            "broken_before": len(broken_before_set),
            "broken_after": len(broken_after_set),
        },
        "summary": {
            "mapping_count": len(mappings),
            "key_count": len(key_renames),
            "entry_count": entry_count,
            "locales_affected": sorted(locales_affected),
            "reference_count": len(rewrites),
            "message_count": len(messages_rewritten),
            "risk_count": len(risks),
            "broken_before": len(broken_before_set),
            "broken_after": len(broken_after_set),
        },
    }


# ---------------------------------------------------------------- 物化

def materialize_rename(snap: dict, mappings: list[dict],
                       rewrites: Optional[list[dict]] = None
                       ) -> tuple[dict, dict, dict[str, dict[str, str]]]:
    """把重命名计划物化为新快照的 bundles/parents（纯内存）。

    步骤（每个语言包内）：先按精确 token 区间倒序改写消息文本中的 ``{@key}``，
    再统一重命名字典键（含 invalid 叶子）。改写后消息必须仍可解析。

    第三个返回值 ``text_after`` 是键改名**之前**的消息值视图（仅含被引用
    改写过的消息），供变更清单按旧键展示 from/to。
    """
    rename_map = {m["old_key"]: m["new_key"] for m in mappings}

    edits: dict[tuple[str, str], list[tuple[int, int, str]]] = {}
    if rewrites is not None:
        for r in rewrites:
            edits.setdefault((r["locale"], r["key"]), []).append(
                (r["pos"], r["end"], r["new_key"]))

    bundles: dict[str, dict] = {}
    text_after: dict[str, dict[str, str]] = {}
    for loc, b in snap["bundles"].items():
        entries = dict(b["entries"])
        # 1) 引用文本改写（倒序拼接，保持偏移有效）
        for (l, key), spans in edits.items():
            if l != loc or key not in entries:
                continue
            text = entries[key]
            last_start = len(text) + 1
            for start, end, new_key in sorted(spans, key=lambda x: -x[0]):
                if not (0 <= start <= end < len(text)) or start > last_start:
                    raise ValueError(
                        f"引用改写区间非法：{loc}:{key} [{start},{end}]")
                last_start = start
                text = text[:start] + "{@" + new_key + "}" + text[end + 1:]
            # 防御：改写后必须仍是合法 ICU（非法目标键已在前序校验拒绝）
            parse_message(text)
            entries[key] = text
            text_after.setdefault(loc, {})[key] = text
        # 2) 键重命名
        renamed_entries = {rename_map.get(k, k): v for k, v in entries.items()}
        invalid = []
        for inv in b.get("invalid", []):
            inv = dict(inv)
            inv["key"] = rename_map.get(inv["key"], inv["key"])
            invalid.append(inv)
        bundles[loc] = {
            "locale": loc, "filename": b["filename"],
            "entries": renamed_entries, "invalid": invalid,
            "parse_error": b.get("parse_error"),
        }
    return bundles, dict(snap["parents"]), text_after
