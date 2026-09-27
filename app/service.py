"""应用服务层：导入快照、构建审查结果、差异报告、原子合并修复。"""
from __future__ import annotations

import hashlib
import json
from typing import Optional

from . import db as dbmod
from .faults import faults
from .analyzer import (AnalysisResult, FallbackGraph, Issue, ParsedBundle,
                       analyze, Fix)
from .icu import IcuError, parse_message
from .impact import analyze_impact, build_ref_graph, compare_graphs
from .resources import load_bundle
from . import rename as renamemod


# ---------------------------------------------------------------- 构建

def build_parsed(bundles: dict[str, dict]) -> dict[str, ParsedBundle]:
    from .resources import InvalidLeaf
    parsed: dict[str, ParsedBundle] = {}
    for loc, b in bundles.items():
        # 导入/快照中的 invalid 叶子以 dict 存储；分析器按对象属性访问，统一规范化
        invalid = [
            inv if not isinstance(inv, dict)
            else InvalidLeaf(key=inv["key"], kind=inv.get("kind", "non_string"),
                             value_repr=inv.get("value_repr", ""),
                             pos=inv.get("pos", 0))
            for inv in b.get("invalid", [])]
        pb = ParsedBundle(
            locale=loc, entries=dict(b["entries"]),
            parent_declared=b.get("parent"),
            invalid=invalid)
        for key, val in b["entries"].items():
            try:
                pb.shapes[key] = parse_message(val)
            except IcuError as e:
                pb.syntax_errors[key] = e
        parsed[loc] = pb
    return parsed


def analyze_snapshot(snap: dict) -> AnalysisResult:
    parsed = build_parsed(snap["bundles"])
    # 快照中的 parents 是权威来源（导入时合并了文件元数据与表单）
    for loc, parent in snap["parents"].items():
        if loc in parsed:
            parsed[loc].parent_declared = parent
    return analyze(snap["meta"]["base_locale"], parsed)


# ---------------------------------------------------------------- 导入

def _input_hash(payload: dict) -> str:
    """对规范化输入求哈希：文件名排序后拼接其内容 + 基准语言 + 父链映射。"""
    h = hashlib.sha256()
    files = sorted(payload["files"], key=lambda f: f["filename"])
    for f in files:
        h.update(f["filename"].encode("utf-8"))
        h.update(b"\0")
        h.update(f["content"].encode("utf-8"))
        h.update(b"\0")
    h.update(payload["base_locale"].encode("utf-8"))
    h.update(json.dumps(payload.get("parents", {}),
                        ensure_ascii=False, sort_keys=True,
                        separators=(",", ":")).encode("utf-8"))
    return h.hexdigest()


def import_snapshot(conn, *, base_locale: str, files: list[dict],
                    parents_form: Optional[dict] = None,
                    label: str = "") -> dict:
    """files: [{filename, content(bytes|str)}]。幂等：相同输入返回既有版本。"""
    payload = {"files": files, "base_locale": base_locale,
               "parents": parents_form or {}}
    input_hash = _input_hash(payload)

    loaded = []
    for f in files:
        loaded.append(load_bundle(f["filename"], f["content"]))

    # locale 决策：表单/显式参数 > 文件元数据 @@locale > 文件名
    def loc_of(b, idx: int) -> str:
        explicit = (parents_form or {}).get("__locales__", {}).get(b.filename)
        if explicit:
            return explicit
        if b.locale:
            return b.locale
        name = b.filename.rsplit("/", 1)[-1]
        stem = name.rsplit(".", 1)[0]
        return stem or f"locale_{idx}"

    locales = [loc_of(b, i) for i, b in enumerate(loaded)]
    dup = sorted({x for x in locales if locales.count(x) > 1})
    if dup:
        raise ValueError(f"存在重复语言标识: {', '.join(dup)}")

    bundles_out = []
    parents: dict[str, Optional[str]] = {}
    for b, loc in zip(loaded, locales):
        if b.parse_error:
            raise ValueError(f"{b.filename}: {b.parse_error}")
        parent_form = (parents_form or {}).get(loc)
        parent = parent_form if parent_form else b.parent
        parents[loc] = parent or None
        bundles_out.append({
            "locale": loc,
            "filename": b.filename,
            "entries": b.entries,
            "invalid": [{"key": i.key, "kind": i.kind,
                         "value_repr": i.value_repr, "pos": i.pos}
                        for i in b.invalid],
            "parse_error": None,
        })

    if base_locale not in locales:
        raise ValueError(
            f"基准语言 {base_locale!r} 不在导入语言中: {', '.join(sorted(locales))}")

    # rev: 输入哈希前 12 位，确定性
    rev = "r" + input_hash[:12]

    # 去重判定与写入共享同一事务：两个相同输入并发导入时，只有一个插入新版本，
    # 另一个在事务内重新按哈希查找并仅切换 current_rev。
    with dbmod.transaction(conn):
        existing = dbmod.find_revision_by_hash(conn, input_hash)
        if existing:
            dbmod.set_current_rev(conn, existing)
            return {"rev": existing, "deduplicated": True}

        faults.fire("import.after_dedup_check", conn=conn, rev=rev)
        dbmod.insert_revision(
            conn, rev=rev, base_locale=base_locale, input_hash=input_hash,
            label=label, parents=parents,
            bundles=sorted(bundles_out, key=lambda x: x["locale"]))
        faults.fire("import.after_insert_revision", conn=conn, rev=rev)
        dbmod.insert_audit(
            conn, actor="user", action="import", from_rev=None, to_rev=rev,
            base_rev=rev, detail={"files": [b["filename"] for b in bundles_out],
                                  "locales": sorted(locales),
                                  "base_locale": base_locale,
                                  "input_hash": input_hash})
        faults.fire("import.after_insert_audit", conn=conn, rev=rev)
        dbmod.set_current_rev(conn, rev)
        faults.fire("import.after_set_current", conn=conn, rev=rev)
    return {"rev": rev, "deduplicated": False}


# ---------------------------------------------------------------- 差异

def diff_snapshots(conn, rev_a: str, rev_b: str) -> dict:
    sa = dbmod.load_revision(conn, rev_a)
    sb = dbmod.load_revision(conn, rev_b)
    if sa is None or sb is None:
        raise KeyError("版本不存在")

    def index(snap):
        out = {}
        for loc, b in snap["bundles"].items():
            for k, v in b["entries"].items():
                out[(loc, k)] = v
        return out

    ia, ib = index(sa), index(sb)
    keys = sorted(set(ia) | set(ib))
    added, removed, changed = [], [], []
    for key in keys:
        va, vb = ia.get(key), ib.get(key)
        if va is None:
            added.append({"locale": key[0], "key": key[1], "value": vb})
        elif vb is None:
            removed.append({"locale": key[0], "key": key[1], "value": va})
        elif va != vb:
            changed.append({"locale": key[0], "key": key[1],
                            "from": va, "to": vb})
    la = set(sa["bundles"]); lb = set(sb["bundles"])
    return {
        "from_rev": rev_a, "to_rev": rev_b,
        "locales_added": sorted(lb - la),
        "locales_removed": sorted(la - lb),
        "added": added, "removed": removed, "changed": changed,
        "summary": {"added": len(added), "removed": len(removed),
                    "changed": len(changed),
                    "locales_added": len(lb - la),
                    "locales_removed": len(la - lb)},
    }


# ---------------------------------------------------------------- 合并修复

class ConflictError(Exception):
    pass


def _apply_patches(text: str, patches: list[dict]) -> str:
    # 从后向前应用，保证偏移有效；先校验区间不重叠且合法
    ordered = sorted(patches, key=lambda p: (-p["start"], -p["end"]))
    last_start = len(text) + 1
    for p in ordered:
        if not (0 <= p["start"] <= p["end"] <= len(text)):
            raise ValueError(f"补丁区间越界: [{p['start']},{p['end']}]")
        if p["start"] > last_start:
            raise ValueError("补丁区间重叠")
        last_start = p["start"]
        text = text[:p["start"]] + p["replacement"] + text[p["end"]:]
    return text


def materialize_fixes(snap: dict, fixes: list[dict]) -> tuple[dict, dict,
                                                               list[dict]]:
    """把修复集物化为「合并后快照」的纯内存视图（不落库）。

    原子合并与影响预演共用本函数：任一修复无法应用即抛 ValueError。
    返回 (bundles 新视图, parents 新映射, applied 记录)。
    """
    bundles: dict[str, dict] = {
        loc: {"locale": loc, "filename": b["filename"],
              "entries": dict(b["entries"]),
              "invalid": list(b.get("invalid", [])),
              "parse_error": None}
        for loc, b in snap["bundles"].items()}
    parents = dict(snap["parents"])
    applied: list[dict] = []

    # 去重：相同 (op,locale,key,parent) 只执行一次，确定性
    seen: set = set()
    for fx in fixes:
        op = fx.get("op")
        loc = fx.get("locale")
        key = fx.get("key")
        dedup = (op, loc, key, fx.get("parent"),
                 json.dumps(fx.get("patches"), sort_keys=True) if fx.get("patches") else None)
        if dedup in seen:
            continue
        seen.add(dedup)
        rec = {"op": op, "locale": loc, "key": key,
               "reason": fx.get("reason", "")}
        if op == "set_parent":
            if loc not in bundles:
                raise ValueError(f"语言 {loc} 不存在")
            target = fx.get("parent")
            if target is not None and target not in bundles and target != snap["meta"]["base_locale"]:
                raise ValueError(f"父语言 {target} 未导入")
            parents[loc] = target
            rec["parent"] = target
        elif loc not in bundles:
            raise ValueError(f"语言 {loc} 不存在")
        elif op == "upsert":
            if not key:
                raise ValueError("upsert 缺少 key")
            value = fx.get("value", "")
            if not isinstance(value, str):
                raise ValueError("upsert value 必须是字符串")
            bundles[loc]["entries"][key] = value
            bundles[loc]["invalid"] = [
                i for i in bundles[loc]["invalid"] if i["key"] != key]
            rec["value"] = value
        elif op == "delete":
            bundles[loc]["entries"].pop(key, None)
            bundles[loc]["invalid"] = [
                i for i in bundles[loc]["invalid"] if i["key"] != key]
        elif op == "text_patch":
            if key not in bundles[loc]["entries"]:
                raise ValueError(f"键 {key} 不存在，无法打补丁")
            old = bundles[loc]["entries"][key]
            new = _apply_patches(old, fx.get("patches", []))
            # 补丁后必须仍可解析
            try:
                parse_message(new)
            except IcuError as e:
                raise ValueError(f"补丁结果 ICU 语法错误: {e.message} @{e.pos}")
            bundles[loc]["entries"][key] = new
            rec["from"] = old
            rec["to"] = new
        else:
            raise ValueError(f"未知操作 {op!r}")
        applied.append(rec)
    return bundles, parents, applied


def apply_merge(conn, *, base_rev: str, fixes: list[dict],
                actor: str = "user") -> dict:
    """原子合并。

    fixes: [{op, locale, key?, value?, parent?, patches?, reason?}]
    仅当 base_rev 仍是当前版本时执行（乐观并发），否则抛 ConflictError。

    事务边界：基线复核（事务内）→ 新快照写入 → 合并审计 → current_rev 切换
    共享同一 ``BEGIN IMMEDIATE`` 事务，任一步失败整体回滚。两个并发请求即使
    在事务外都读到了相同的 current_rev，也会在写锁内被串行化，后进入者重新
    核对基线并得到 ConflictError，陈旧版本不可能覆盖当前指针。
    """
    # 快速预检：过期基线在拿写锁前即拒绝（权威判定仍在事务内）
    current = dbmod.get_current_rev(conn)
    if current != base_rev:
        raise ConflictError(
            f"版本已变化：审查基于 {base_rev}，当前为 {current}，请重新审查后再合并")

    snap = dbmod.load_revision(conn, base_rev)
    if snap is None:
        raise KeyError("基准版本不存在")

    try:
        bundles, parents, applied = materialize_fixes(snap, fixes)
    except ValueError as e:
        # 原子性：任一修复无效则整体不落地
        raise ValueError(f"存在无法应用的修复: {e}")

    # 以新内容构造新快照
    files = [{"filename": b["filename"],
              "content": json.dumps(b["entries"], ensure_ascii=False,
                                    indent=2, sort_keys=True)}
             for b in bundles.values()]
    payload = {"files": sorted(files, key=lambda f: f["filename"]),
               "base_locale": snap["meta"]["base_locale"],
               "parents": {k: v for k, v in parents.items() if v}}
    input_hash = _input_hash(payload)
    new_rev = "r" + input_hash[:12]

    # 单一事务：重新核对基线 → 幂等去重 → 写快照 → 写审计 → 切换 current_rev。
    # BEGIN IMMEDIATE 立即取写锁，使并发合并在临界区内串行；任一步失败，
    # revision/bundle/audit/state 全部回滚到合并前。
    with dbmod.transaction(conn):
        faults.fire("merge.before_recheck", conn=conn, base_rev=base_rev)
        current_now = dbmod.get_current_rev(conn)
        if current_now != base_rev:
            raise ConflictError(
                f"版本已变化：审查基于 {base_rev}，当前为 {current_now}，"
                "请重新审查后再合并")
        # 基线也必须仍存在（理论上快照不可变，防御性复核）
        if dbmod.get_revision_meta(conn, base_rev) is None:
            raise KeyError("基准版本不存在")

        # 事务内重做幂等去重：防止两个相同结果的合并并发各插一份
        existing = dbmod.find_revision_by_hash(conn, input_hash)
        if existing:
            # 合并后与某既有版本内容相同（例如幂等重放空操作集）
            faults.fire("merge.dedup.after_check", conn=conn, rev=existing)
            dbmod.insert_audit(
                conn, actor=actor, action="merge", from_rev=base_rev,
                to_rev=existing, base_rev=base_rev,
                detail={"applied": applied, "deduplicated": True})
            faults.fire("merge.dedup.after_audit", conn=conn, rev=existing)
            dbmod.set_current_rev(conn, existing)
            faults.fire("merge.dedup.after_set_current", conn=conn, rev=existing)
            return {"rev": existing, "from_rev": base_rev, "deduplicated": True,
                    "applied": applied}

        faults.fire("merge.after_recheck", conn=conn, base_rev=base_rev,
                    rev=new_rev)
        dbmod.insert_revision(
            conn, rev=new_rev, base_locale=snap["meta"]["base_locale"],
            input_hash=input_hash, label="",
            parents=parents,
            bundles=sorted(bundles.values(), key=lambda x: x["locale"]))
        faults.fire("merge.after_insert_revision", conn=conn, rev=new_rev)
        dbmod.insert_audit(
            conn, actor=actor, action="merge", from_rev=base_rev,
            to_rev=new_rev, base_rev=base_rev, detail={"applied": applied})
        faults.fire("merge.after_insert_audit", conn=conn, rev=new_rev)
        dbmod.set_current_rev(conn, new_rev)
        faults.fire("merge.after_set_current", conn=conn, rev=new_rev)
    return {"rev": new_rev, "from_rev": base_rev, "deduplicated": False,
            "applied": applied}


# ---------------------------------------------------------------- 资源键重命名迁移

def _rename_result_hash(snap: dict, bundles: dict, parents: dict) -> str:
    """重命名产物快照的规范化哈希（与 apply_merge 的产物构造保持一致）。"""
    files = [{"filename": b["filename"],
              "content": json.dumps(b["entries"], ensure_ascii=False,
                                    indent=2, sort_keys=True)}
             for b in bundles.values()]
    payload = {"files": sorted(files, key=lambda f: f["filename"]),
               "base_locale": snap["meta"]["base_locale"],
               "parents": {k: v for k, v in parents.items() if v}}
    return _input_hash(payload)


def rename_dry_run(conn, *, rev: str, mappings: list[dict]) -> dict:
    """只读试运行：返回完整改写计划、受影响引用与断裂风险，不写库。"""
    snap = dbmod.load_revision(conn, rev)
    if snap is None:
        raise KeyError("版本不存在")
    analysis = analyze_snapshot(snap)
    plan = renamemod.plan_rename(snap, analysis, mappings)
    plan["rev"] = rev
    plan["revision"] = revision_summary(conn, rev)
    return plan


class RenameReplayError(RuntimeError):
    """事务内核对发现已完成迁移的产物/审计关系不一致（数据完整性故障）。"""


# ---------------------------------------------------------------- 旧卷升级恢复

def _verify_rename_audit_group(conn, base_rev: str, plan_hash: str,
                               audits: list[dict]) -> tuple[Optional[dict],
                                                            list[str]]:
    """核对一组同源同计划的历史 rename 审计，决定是否可恢复为完成记录。

    通过时返回 ``(record_kwargs, [])``；冲突或残缺时返回 ``(None, problems)``
    （只读核对，不写库）。核对项：

    - 重复审计的（源版本, 目标版本, 基线）必须完全一致——关联一致才归并；
    - 审计的源版本必须等于基线（rename 提交始终以源版本为基线）；
    - 每条审计必须带 ``detail.plan_hash`` 与 ``mappings``，且重复审计的
      规范化映射集合一致；
    - 源版本与产物版本都必须存在；
    - 产物摘要核对：用审计中的映射在源版本快照上重算计划（计划摘要必须
      与审计一致）并物化，产物哈希必须等于产物版本的 ``input_hash``。
    """
    problems: list[str] = []
    relations = {(a["from_rev"], a["to_rev"], a["base_rev"]) for a in audits}
    if len(relations) > 1:
        problems.append("同源同计划的重复审计新旧版本或基线不一致，拒绝归并")
        return None, problems
    from_rev, to_rev, base = next(iter(relations))
    if not from_rev or not to_rev:
        problems.append("审计缺少源版本或目标版本")
    if from_rev != base_rev or base != base_rev:
        problems.append("审计的源版本与基线不一致")

    mapping_sets: set = set()
    for a in audits:
        detail = a.get("detail") or {}
        if detail.get("plan_hash") != plan_hash:
            problems.append("审计的计划摘要缺失或与分组不符")
        m = detail.get("mappings")
        if not isinstance(m, list) or not m:
            mapping_sets.add(None)
            continue
        try:
            norm = renamemod.normalize_mappings(m)
        except renamemod.RenameValidationError:
            mapping_sets.add(None)
            continue
        mapping_sets.add(tuple(sorted(
            (x["old_key"], x["new_key"]) for x in norm)))
    if None in mapping_sets:
        problems.append("审计缺少映射明细，无法核对产物摘要")
    if len(mapping_sets - {None}) > 1:
        problems.append("同源同计划的重复审计映射明细不一致，拒绝归并")
    if problems:
        return None, problems

    if dbmod.get_revision_meta(conn, from_rev) is None:
        problems.append("源版本缺失")
    dst_meta = dbmod.get_revision_meta(conn, to_rev)
    if dst_meta is None:
        problems.append("产物版本缺失")
    if problems:
        return None, problems

    # 产物摘要核对：按审计映射在源版本上重算计划与产物哈希
    snap = dbmod.load_revision(conn, from_rev)
    mappings = (audits[0].get("detail") or {}).get("mappings")
    try:
        plan = renamemod.plan_rename(snap, analyze_snapshot(snap), mappings)
    except renamemod.RenameValidationError:
        problems.append("审计映射相对源版本不再合法")
        return None, problems
    if plan["plan_hash"] != plan_hash:
        problems.append("审计映射重算的计划摘要与审计记录不符")
        return None, problems
    bundles, parents, _text = renamemod.materialize_rename(
        snap, plan["mappings"], plan["reference_rewrites"])
    expected_hash = _rename_result_hash(snap, bundles, parents)
    if expected_hash != dst_meta["input_hash"]:
        problems.append("产物摘要与源版本按计划重算的结果不符")
        return None, problems

    rec = {"base_rev": base_rev, "plan_hash": plan_hash,
           "from_rev": from_rev, "to_rev": to_rev,
           "input_hash": expected_hash,
           # 归并到首次完成审计（重复审计关联已核对一致）
           "audit_id": min(a["id"] for a in audits)}
    return rec, []


def recover_rename_records(conn) -> dict:
    """旧数据卷升级恢复：从历史 rename 审计事务化重建已完成迁移的登记记录。

    rename_record 表后建于 rename 审计：旧卷里已有成功审计与产物却没有
    登记记录，升级后客户端用原 rev/expected_rev/映射重试会误判 409。
    本函数在**单个** ``BEGIN IMMEDIATE`` 事务内扫描全部 rename 审计，
    按（源版本, 规范化计划摘要）分组核对并补登记：

    - 已有记录的组跳过（不覆盖已有有效记录）；
    - 同源同计划的重复审计关联一致才归并（锚定首次审计）；
    - 冲突或残缺的组明确拒绝（写入报告，不登记、不覆盖）；
    - 幂等：重复升级或并发重试不会重复登记（主键 + 写事务串行）。
    """
    groups: dict[tuple[str, Optional[str]], list[dict]] = {}
    for a in dbmod.list_rename_audits(conn):
        ph = (a.get("detail") or {}).get("plan_hash")
        if not isinstance(ph, str) or not ph:
            ph = None  # 残缺：缺少计划摘要的审计无法核对
        groups.setdefault((a["base_rev"], ph), []).append(a)

    report: dict = {"recovered": [], "skipped_existing": [], "rejected": []}
    with dbmod.transaction(conn):
        for key in sorted(groups, key=lambda k: (k[0] or "", k[1] or "")):
            base_rev, plan_hash = key
            audits = groups[key]
            entry = {"base_rev": base_rev, "plan_hash": plan_hash,
                     "audit_ids": [a["id"] for a in audits]}
            if plan_hash is None:
                report["rejected"].append(
                    {**entry, "reasons": ["审计缺少计划摘要，无法核对"]})
                continue
            if dbmod.find_rename_record(conn, base_rev, plan_hash) is not None:
                # 已有登记记录：以记录为准，不覆盖
                report["skipped_existing"].append(entry)
                continue
            rec, problems = _verify_rename_audit_group(
                conn, base_rev, plan_hash, audits)
            if problems:
                report["rejected"].append({**entry, "reasons": problems})
                continue
            dbmod.insert_rename_record(conn, **rec)
            report["recovered"].append(rec)
    return report


def _recover_rename_record_from_audits(conn, base_rev: str,
                                       plan_hash: str) -> Optional[dict]:
    """确认链路内的定点恢复（调用方必须持有写事务）。

    历史 rename 审计能证明（源版本, 计划摘要）已完成时，在当前事务内补
    登记并返回记录；审计冲突或残缺时抛 :class:`RenameReplayError` 明确
    拒绝回放（不落入 409 分支）；没有任何匹配审计时返回 None，由调用方
    继续走 current_rev 新鲜性判定。
    """
    audits = [a for a in dbmod.list_rename_audits(conn)
              if a["base_rev"] == base_rev
              and (a.get("detail") or {}).get("plan_hash") == plan_hash]
    if not audits:
        return None
    rec, problems = _verify_rename_audit_group(conn, base_rev, plan_hash,
                                               audits)
    if problems:
        raise RenameReplayError(
            f"历史重命名审计核对失败，拒绝把该迁移当作已完成回放"
            f"（{base_rev} / {plan_hash}）：{'；'.join(problems)}")
    dbmod.insert_rename_record(conn, **rec)
    faults.fire("rename.recover.after_insert_record", conn=conn,
                base_rev=base_rev)
    return dbmod.find_rename_record(conn, base_rev, plan_hash)


def rename_commit(conn, *, rev: str, expected_rev: str, mappings: list[dict],
                  actor: str = "user") -> dict:
    """确认迁移：单事务改写全部资源键与引用，生成新不可变版本与审计记录。

    幂等语义（修复重试 409 缺陷）
    -----------------------------
    - ``rev`` 为所选不可变版本，``expected_rev`` 为乐观并发令牌；
    - 已完成迁移按 **(源版本 rev, 规范化计划摘要 plan_hash)** 识别
      （计划摘要只依赖有序映射对，提交顺序无关）；该查找在写事务内进行，
      早于 current_rev 新鲜性判定：因此首次确认把 current_rev 推进到新版本
      后，客户端用**原 rev、原 expected_rev、同一映射**重试也能命中；
    - **旧数据卷升级**：rename_record 表后建于 rename 审计。登记记录缺失
      时，在同一事务内从历史 rename 审计定点恢复（核对源版本、产物摘要、
      审计的源/目标/基线与计划摘要；重复审计关联一致才归并，冲突或残缺
      抛 :class:`RenameReplayError` 明确拒绝回放，不落入 409 分支）；
    - 命中后在同一事务内**核对产物版本与审计关系**（产物存在且哈希相符、
      原始 rename 审计存在且 from/to/base/plan_hash 全部一致），核对通过即
      **回放**首次结果：返回相同的新旧版本与变更清单，不新建版本、不追加
      审计、不切换当前指针（响应标记 ``replayed=true``）；
    - 尚无成功记录也无历史审计的过期请求（current_rev != expected_rev）
      仍抛 :class:`ConflictError`（409）；
    - 记录行与 revision/audit/指针切换共享同一 ``BEGIN IMMEDIATE`` 事务，
      失败事务整体回滚，不可能留下「已完成」假象；(base_rev, plan_hash)
      的主键约束保证并发重复提交只有一次真正生效。

    计划先在锁外完整计算（含全部校验），映射非法抛 RenameValidationError。
    """
    if rev != expected_rev:
        # 允许对任意不可变版本做迁移，但必须携带它自己的当前性令牌
        raise ConflictError(
            f"expected_rev 与所选版本不一致：{expected_rev} != {rev}")

    snap = dbmod.load_revision(conn, rev)
    if snap is None:
        raise KeyError("基准版本不存在")

    # 计划在锁外完整计算（快照不可变，结果确定）：全部业务校验在此发生，
    # 映射非法直接抛 RenameValidationError（即使令牌已过期也优先报 400，
    # 与修复前行为一致）。计划摘要只依赖有序映射对，提交顺序无关。
    plan = renamemod.plan_rename(snap, analyze_snapshot(snap), mappings)
    plan_hash = plan["plan_hash"]
    bundles, parents, text_after = renamemod.materialize_rename(
        snap, plan["mappings"], plan["reference_rewrites"])
    input_hash = _rename_result_hash(snap, bundles, parents)
    new_rev = "r" + input_hash[:12]
    no_change = new_rev == rev

    detail = {
        "plan_hash": plan["plan_hash"],
        "mappings": plan["mappings"],
        "key_renames": [{"old_key": k["old_key"], "new_key": k["new_key"],
                         "locales": k["locales"]} for k in plan["key_renames"]],
        "reference_rewrite_count": len(plan["reference_rewrites"]),
        "risk_summary": plan["risks"]["summary"],
        "no_change": no_change,
    }

    with dbmod.transaction(conn):
        faults.fire("rename.before_recheck", conn=conn, rev=rev)

        # 1) 先按源版本 + 计划摘要识别已完成迁移（不要求令牌仍然新鲜）。
        rec = dbmod.find_rename_record(conn, rev, plan_hash)
        if rec is None:
            # 旧数据卷升级：rename_record 表是后建的，历史 rename 审计可能
            # 已证明该迁移完成——在同一事务内定点恢复登记；审计冲突或残缺
            # 时明确拒绝回放（RenameReplayError），不落入 409 分支。
            rec = _recover_rename_record_from_audits(conn, rev, plan_hash)
        if rec is not None:
            faults.fire("rename.replay.before_verify", conn=conn, rev=rev)
            existing = _verify_completed_rename(conn, rec)
            faults.fire("rename.replay.after_verify", conn=conn, rev=existing)
            # 重放：复用锁外按同一不可变快照算出的计划与变更清单，回放首次
            # 结果；不写任何东西（不新建版本、不追加审计、不切换指针）。
            return _rename_result_payload(
                conn, snap=snap, bundles=bundles, text_after=text_after,
                plan=plan, from_rev=rev, result_rev=existing,
                deduplicated=True, no_change=existing == rev, replayed=True)

        # 2) 没有成功记录：过期请求仍 409（失败事务不会被误认作已完成）。
        current_now = dbmod.get_current_rev(conn)
        if current_now != expected_rev:
            raise ConflictError(
                f"版本已变化：计划基于 {expected_rev}，当前为 {current_now}，"
                "请重新试运行后再确认")
        if dbmod.get_revision_meta(conn, rev) is None:
            raise KeyError("基准版本不存在")

        if no_change:
            # 空产物（理论上被校验拦截，防御）：幂等空操作，仅记审计。
            faults.fire("rename.dedup.no_change", conn=conn, rev=rev)
            audit_id = dbmod.insert_audit(
                conn, actor=actor, action="rename", from_rev=rev,
                to_rev=rev, base_rev=rev,
                detail={**detail, "deduplicated": True})
            dbmod.insert_rename_record(
                conn, base_rev=rev, plan_hash=plan_hash, from_rev=rev,
                to_rev=rev, input_hash=input_hash, audit_id=audit_id)
            faults.fire("rename.dedup.after_record", conn=conn, rev=rev)
            dbmod.set_current_rev(conn, rev)
            result_rev = rev
            deduplicated = True
        else:
            existing = dbmod.find_revision_by_hash(conn, input_hash)
            if existing:
                # 产物版本已存在但此前没有以 (源版本, 计划摘要) 登记
                # （例如内容等价的旧版本）：补审计、登记关系并切换指针。
                faults.fire("rename.dedup.after_check", conn=conn,
                            rev=existing)
                audit_id = dbmod.insert_audit(
                    conn, actor=actor, action="rename", from_rev=rev,
                    to_rev=existing, base_rev=rev,
                    detail={**detail, "deduplicated": True})
                faults.fire("rename.dedup.after_audit", conn=conn,
                            rev=existing)
                dbmod.insert_rename_record(
                    conn, base_rev=rev, plan_hash=plan_hash, from_rev=rev,
                    to_rev=existing, input_hash=input_hash, audit_id=audit_id)
                faults.fire("rename.dedup.after_record", conn=conn,
                            rev=existing)
                dbmod.set_current_rev(conn, existing)
                faults.fire("rename.dedup.after_set_current", conn=conn,
                            rev=existing)
                result_rev = existing
                deduplicated = True
            else:
                faults.fire("rename.after_recheck", conn=conn,
                            base_rev=rev, rev=new_rev)
                dbmod.insert_revision(
                    conn, rev=new_rev,
                    base_locale=snap["meta"]["base_locale"],
                    input_hash=input_hash, label="",
                    parents=parents,
                    bundles=sorted(bundles.values(),
                                   key=lambda x: x["locale"]))
                faults.fire("rename.after_insert_revision", conn=conn,
                            rev=new_rev)
                audit_id = dbmod.insert_audit(
                    conn, actor=actor, action="rename", from_rev=rev,
                    to_rev=new_rev, base_rev=rev,
                    detail={**detail, "deduplicated": False})
                faults.fire("rename.after_insert_audit", conn=conn,
                            rev=new_rev)
                dbmod.insert_rename_record(
                    conn, base_rev=rev, plan_hash=plan_hash, from_rev=rev,
                    to_rev=new_rev, input_hash=input_hash, audit_id=audit_id)
                faults.fire("rename.after_insert_record", conn=conn,
                            rev=new_rev)
                dbmod.set_current_rev(conn, new_rev)
                faults.fire("rename.after_set_current", conn=conn,
                            rev=new_rev)
                result_rev = new_rev
                deduplicated = False

        return _rename_result_payload(
            conn, snap=snap, bundles=bundles, text_after=text_after,
            plan=plan, from_rev=rev, result_rev=result_rev,
            deduplicated=deduplicated, no_change=no_change, replayed=False)


def _verify_completed_rename(conn, rec: dict) -> str:
    """事务内核对一条已完成重命名记录的产物与审计关系。

    任一项不符（产物/源版本缺失、产物哈希不符、rename 审计缺失或
    from/to/base/plan_hash 对不上）都视为数据完整性故障，拒绝把它当作
    已完成迁移回放。审计的 ``deduplicated`` 允许为 True：它表示产物版本
    行复用了既有内容哈希，但该审计仍是这条（源版本, 计划摘要）迁移关系
    的首次完成记录。核对通过返回产物版本号。
    """
    to_rev = rec["to_rev"]
    meta = dbmod.get_revision_meta(conn, to_rev)
    base_meta = dbmod.get_revision_meta(conn, rec["base_rev"])
    audit = dbmod.get_audit_by_id(conn, rec["audit_id"])
    problems = []
    if meta is None:
        problems.append("产物版本缺失")
    elif meta["input_hash"] != rec["input_hash"]:
        problems.append("产物哈希与记录不符")
    if base_meta is None:
        problems.append("源版本缺失")
    if audit is None:
        problems.append("审计记录缺失")
    else:
        if (audit["action"] != "rename"
                or audit["from_rev"] != rec["from_rev"]
                or audit["to_rev"] != rec["to_rev"]
                or audit["base_rev"] != rec["base_rev"]):
            problems.append("审计新旧版本或基线与记录不符")
        if (audit.get("detail") or {}).get("plan_hash") != rec["plan_hash"]:
            problems.append("审计计划摘要与记录不符")
    if problems:
        raise RenameReplayError(
            f"重命名完成记录核对失败（{rec['base_rev']} / "
            f"{rec['plan_hash']}）：{'；'.join(problems)}")
    return to_rev


def _rename_result_payload(conn, *, snap: dict, bundles: dict,
                           text_after: dict, plan: dict, from_rev: str,
                           result_rev: str, deduplicated: bool,
                           no_change: bool, replayed: bool) -> dict:
    """组装确认/回放响应：相同的新旧版本与变更清单。"""
    return {
        "rev": result_rev, "from_rev": from_rev, "new_rev": result_rev,
        "deduplicated": deduplicated, "no_change": no_change,
        "replayed": replayed, **plan,
        "revision": revision_summary(conn, result_rev),
        "changeset": _rename_changeset(snap, bundles, text_after, plan),
    }


def _rename_changeset(before: dict, after_bundles: dict,
                       text_after: dict, plan: dict) -> dict:
    """提交结果中的变更清单：键改名、值改写（引用）、未改动条目计数。"""
    key_changes = [{"old_key": k["old_key"], "new_key": k["new_key"],
                    "locales": k["locales"]} for k in plan["key_renames"]]
    value_changes: list[dict] = []
    for loc, by_key in text_after.items():
        for key, new_text in by_key.items():
            value_changes.append({
                "locale": loc, "key": key,
                "from": before["bundles"][loc]["entries"][key],
                "to": new_text,
            })
    # 同一消息多处引用会产生多条 rewrite，值改写按消息去重展示
    seen: set = set()
    uniq_value_changes = []
    for c in value_changes:
        k = (c["locale"], c["key"])
        if k in seen:
            continue
        seen.add(k)
        uniq_value_changes.append(c)
    uniq_value_changes.sort(key=lambda c: (c["locale"], c["key"]))
    return {
        "keys": key_changes,
        "values": uniq_value_changes,
        "value_change_count": len(uniq_value_changes),
    }


# ---------------------------------------------------------------- 消息实例预览

def preview_messages(conn, *, rev: str, key: str, locales: list[str],
                     variables: dict) -> dict:
    """只读：在同一不可变版本上用同一组变量为多个目标语言渲染消息实例。

    逐语言隔离：某语言渲染失败只影响该语言（结构化错误，不含部分文本），
    其余语言照常返回。
    """
    snap = dbmod.load_revision(conn, rev)
    if snap is None:
        raise KeyError("版本不存在")
    analysis = analyze_snapshot(snap)
    from .preview import RenderError, render_message

    results = []
    for loc in locales:
        entry: dict = {"locale": loc, "key": key}
        try:
            r = render_message(analysis, loc, key, variables)
            entry.update({
                "ok": True,
                "text": r.text,
                "resolved_locale": r.resolved_locale,
                "fell_back": r.fell_back,
                "fallback_chain": r.fallback_chain,
                "hops": [h.to_dict() for h in r.hops],
            })
        except RenderError as e:
            entry.update({"ok": False, "error": e.to_dict()})
        results.append(entry)

    ok_count = sum(1 for r in results if r.get("ok"))
    return {
        "rev": rev,
        "revision": revision_summary(conn, rev),
        "key": key,
        "variables": variables,
        "summary": {"locales": len(results), "ok": ok_count,
                    "errors": len(results) - ok_count},
        "results": results,
    }


# ---------------------------------------------------------------- 自包含语言包编译

def compile_language_pack(conn, *, rev: str, locales: list[str],
                          keys: list[str]) -> dict:
    """只读编译自包含语言包（不写快照/审计、不切换 current_rev）。

    成功返回清单、逐键来源、模板与 ZIP 字节；任一条目断裂引用/引用环/语法
    错误/不可解析则抛 :class:`app.pack.PackBlocked`（含全部错误）。
    版本不存在抛 KeyError；语言/键集合为空抛 ValueError。
    """
    if not locales:
        raise ValueError("locales 不能为空")
    if not keys:
        raise ValueError("keys 不能为空")
    snap = dbmod.load_revision(conn, rev)
    if snap is None:
        raise KeyError("版本不存在")
    analysis = analyze_snapshot(snap)
    from . import pack as packmod

    result = packmod.compile_pack(analysis, snap["meta"],
                                  list(locales), list(keys))
    result["revision"] = revision_summary(conn, rev)
    return result


# ---------------------------------------------------------------- 引用影响分析

def revision_summary(conn, rev: str) -> Optional[dict]:
    """版本摘要：影响结果始终绑定该摘要；current_rev 用于过期提示。"""
    meta = dbmod.get_revision_meta(conn, rev)
    if meta is None:
        return None
    return {
        "rev": rev,
        "base_locale": meta["base_locale"],
        "label": meta["label"],
        "created_at": meta["created_at"],
        "current_rev": dbmod.get_current_rev(conn),
        "is_current": dbmod.get_current_rev(conn) == rev,
        "bundle_count": meta["bundle_count"],
    }


def impact_for_target(conn, rev: str, locale: str, key: str) -> dict:
    snap = dbmod.load_revision(conn, rev)
    if snap is None:
        raise KeyError("版本不存在")
    analysis = analyze_snapshot(snap)
    try:
        result = analyze_impact(analysis, locale, key)
    except KeyError:
        raise LookupError(f"键 {locale}:{key} 不存在于版本 {rev}")
    result["rev"] = rev
    result["revision"] = revision_summary(conn, rev)
    return result


def preview_impact(conn, *, rev: str, locale: str, key: str,
                   fixes: list[dict]) -> dict:
    """对修复集做不落库的影响预演：物化 -> 重建引用图 -> 与基线对比。"""
    snap = dbmod.load_revision(conn, rev)
    if snap is None:
        raise KeyError("版本不存在")
    if locale not in snap["bundles"] or key not in snap["bundles"][locale]["entries"]:
        raise LookupError(f"键 {locale}:{key} 不存在于版本 {rev}")
    before = analyze_snapshot(snap)
    bundles, parents, applied = materialize_fixes(snap, fixes)

    # 仅内存中的虚拟快照，不写库
    virtual = {"meta": snap["meta"], "parents": parents,
               "bundles": {loc: b for loc, b in bundles.items()}}
    after = analyze_snapshot(virtual)

    g_before = build_ref_graph(before)
    g_after = build_ref_graph(after)

    deleted: set = set()
    changed: set = set()
    for rec in applied:
        node = (rec["locale"], rec.get("key"))
        if rec["op"] == "delete":
            deleted.add(node)
        elif rec["op"] in ("upsert", "text_patch"):
            changed.add(node)

    comparison = compare_graphs(g_before, g_after, deleted)
    target_exists = key in after.bundles.get(locale, ParsedBundle(
        locale=locale, entries={}, parent_declared=None)).entries

    impact_after = None
    if target_exists and locale in after.bundles:
        impact_after = analyze_impact(after, locale, key)
        impact_after.pop("graph", None)       # 预演只返回结论，不重复整图
        impact_after.pop("broken_refs", None)

    return {
        "rev": rev,
        "revision": revision_summary(conn, rev),
        "target": {"locale": locale, "key": key,
                   "exists_after": target_exists,
                   "deleted": (locale, key) in deleted,
                   "rewritten": (locale, key) in changed},
        "applied": applied,
        "broken_refs": comparison,
        "impact_after": impact_after,
    }
