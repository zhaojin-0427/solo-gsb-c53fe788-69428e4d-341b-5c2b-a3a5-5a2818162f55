"""契约审查核心：问题模型、形态比对、回退图分析。"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

from .icu import IcuError, Shape, Var, parse_message

# 问题严重级别
SEV_ERROR = "error"
SEV_WARNING = "warning"

# 问题类型
T_MISSING_KEY = "MISSING_KEY"
T_EXTRA_KEY = "EXTRA_KEY"
T_PLACEHOLDER = "PLACEHOLDER_MISMATCH"
T_BRANCH = "BRANCH_MISMATCH"
T_INVALID_SYNTAX = "INVALID_SYNTAX"
T_BROKEN_REF = "BROKEN_REFERENCE"
T_CYCLE = "CYCLE_FALLBACK"
T_UNCOVERED = "UNCOVERED_LANGUAGE"
T_INVALID_RESOURCE = "INVALID_RESOURCE"
T_BASE_MISSING_OTHER = "BRANCH_MISMATCH"          # 复用分支类型


@dataclass
class Fix:
    """确定性修复建议。op: upsert/delete/set_parent/text_patch。"""
    op: str
    auto: bool = False          # 可安全自动应用
    review: bool = True         # 应用前建议人工确认
    locale: Optional[str] = None
    key: Optional[str] = None
    value: Optional[str] = None
    parent: Optional[str] = None
    patches: Optional[list[dict]] = None
    reason: str = ""


@dataclass
class Issue:
    code: str
    severity: str
    locale: str
    key: str
    pos: int
    detail: str
    expected: str = ""
    actual: str = ""
    fix: Optional[Fix] = None
    # 排序键的第四段（同 code 时再区分），默认用 detail
    sort_tail: str = ""
    # 分支问题对应的变量完整路径；元素 ("v", 名称) 为变量，("c", 标签) 为分支
    var_path: tuple[tuple[str, str], ...] = ()

    def sort_key(self) -> tuple:
        return (self.locale, self.key, self.pos, self.code,
                self.sort_tail or self.detail)

    def to_dict(self) -> dict:
        d = asdict(self)
        d["var_path"] = list(self.var_path)
        return d


@dataclass
class ParsedBundle:
    locale: str
    entries: dict[str, str]
    parent_declared: Optional[str]
    shapes: dict[str, Shape] = field(default_factory=dict)
    syntax_errors: dict[str, IcuError] = field(default_factory=dict)
    invalid: list = field(default_factory=list)


# ---------------------------------------------------------------- 形态比对

def _var_map(shape: Shape) -> dict[str, Var]:
    return {v.name: v for v in shape.vars}


def compare_shapes(base: Shape, trans: Shape, locale: str, key: str,
                   base_src: str, trans_src: str) -> list[Issue]:
    """比对同一条消息在基准语言与某语言下的形态差异。

    路径：顶层参数按名称对齐；分支参数（plural/select）要求标签契约一致，
    再递归比对每个共同分支。引用键（@key）集合按名称比对。
    """
    issues: list[Issue] = []
    _compare_level(base, trans, (), (), issues, locale, key)
    return issues


def _vpos(v: Optional[Var]) -> int:
    return v.pos if v is not None else 0


def _path_text(tagged: tuple[tuple[str, str], ...]) -> str:
    """n.one.g 形式的可读路径（变量名与分支标签交替）。"""
    return ".".join(name for _, name in tagged)


def _compare_level(base: Shape, trans: Shape,
                   tagged: tuple[tuple[str, str], ...],
                   disp: tuple[str, ...],
                   issues: list[Issue], locale: str, key: str) -> None:
    bvars = _var_map(base)
    tvars = _var_map(trans)
    pfx = ".".join(disp) + ("." if disp else "")

    # 缺失 / 多余 / 类型不一致的占位符
    for name in sorted(set(bvars) - set(tvars)):
        bv = bvars[name]
        issues.append(Issue(
            code=T_PLACEHOLDER, severity=SEV_ERROR, locale=locale, key=key,
            pos=_vpos(bv),
            detail=f"缺少占位符 {{{pfx}{name}}}",
            expected=f"{pfx}{name}:{bv.type or 'string'}",
            actual="<缺失>",
            sort_tail=f"missing:{pfx}{name}"))
    for name in sorted(set(tvars) - set(bvars)):
        tv = tvars[name]
        issues.append(Issue(
            code=T_PLACEHOLDER, severity=SEV_ERROR, locale=locale, key=key,
            pos=_vpos(tv),
            detail=f"多余占位符 {{{pfx}{name}}}",
            expected="<不存在>",
            actual=f"{pfx}{name}:{tv.type or 'string'}",
            sort_tail=f"extra:{pfx}{name}"))
    for name in sorted(set(bvars) & set(tvars)):
        bv, tv = bvars[name], tvars[name]
        bt, tt = bv.type or "string", tv.type or "string"
        if bt != tt:
            issues.append(Issue(
                code=T_PLACEHOLDER, severity=SEV_ERROR, locale=locale, key=key,
                pos=_vpos(tv),
                detail=f"占位符 {{{pfx}{name}}} 类型不一致",
                expected=bt, actual=tt,
                sort_tail=f"type:{pfx}{name}"))
            continue  # 类型不同，不再深入分支
        if bv.cases or tv.cases:
            _compare_cases(bv, tv, tagged + (("v", name),), disp + (name,),
                           issues, locale, key)

    # 引用键集合
    brefs = {r.key for r in base.refs}
    trefs = {r.key for r in trans.refs}
    for rk in sorted(brefs ^ trefs):
        tref = next((r for r in trans.refs if r.key == rk), None)
        bref = next((r for r in base.refs if r.key == rk), None)
        if rk in brefs:
            issues.append(Issue(
                code=T_PLACEHOLDER, severity=SEV_ERROR, locale=locale, key=key,
                pos=bref.pos if bref else 0,
                detail=f"缺少引用键 {{@{rk}}}", expected=f"@{{{rk}}}",
                actual="<缺失>", sort_tail=f"refmiss:{rk}"))
        else:
            issues.append(Issue(
                code=T_PLACEHOLDER, severity=SEV_ERROR, locale=locale, key=key,
                pos=tref.pos if tref else 0,
                detail=f"多余引用键 {{@{rk}}}", expected="<不存在>",
                actual=f"@{{{rk}}}", sort_tail=f"refextra:{rk}"))


def _compare_cases(bv: Var, tv: Var,
                   tagged: tuple[tuple[str, str], ...],
                   disp: tuple[str, ...],
                   issues: list[Issue], locale: str, key: str) -> None:
    blabels = [c.label for c in bv.cases]
    tlabels = [c.label for c in tv.cases]
    bset, tset = set(blabels), set(tlabels)
    is_select = bv.type == "select"
    path_text = ".".join(disp)

    if is_select:
        # select：标签契约必须精确一致
        for lab in sorted(bset - tset):
            issues.append(Issue(
                code=T_BRANCH, severity=SEV_ERROR, locale=locale, key=key,
                pos=tv.pos,
                detail=f"select 参数 {{{path_text}}} 缺少分支 {lab!r}",
                expected=",".join(sorted(bset)), actual=",".join(sorted(tset)),
                sort_tail=f"selmiss:{lab}", var_path=tagged))
        for lab in sorted(tset - bset):
            case = next(c for c in tv.cases if c.label == lab)
            issues.append(Issue(
                code=T_BRANCH, severity=SEV_ERROR, locale=locale, key=key,
                pos=case.kw_pos,
                detail=f"select 参数 {{{path_text}}} 存在多余分支 {lab!r}",
                expected=",".join(sorted(bset)), actual=",".join(sorted(tset)),
                sort_tail=f"selextra:{lab}", var_path=tagged))
    else:
        # plural / selectordinal：译文必须覆盖基准的全部标签；允许译文新增
        # =N 或细粒度 CLDR 标签（多余标签不报错，属正常语言差异）。
        for lab in sorted(bset - tset):
            issues.append(Issue(
                code=T_BRANCH, severity=SEV_ERROR, locale=locale, key=key,
                pos=tv.pos,
                detail=f"{bv.type} 参数 {{{path_text}}} 缺少分支 {lab!r}",
                expected=",".join(sorted(bset)), actual=",".join(sorted(tset)),
                sort_tail=f"plmiss:{lab}", var_path=tagged))

    # 共同分支递归比对
    for lab in sorted(bset & tset):
        bc = next(c for c in bv.cases if c.label == lab)
        tc = next(c for c in tv.cases if c.label == lab)
        _compare_level(bc.body, tc.body,
                       tagged + (("c", lab),), disp + (lab,),
                       issues, locale, key)


# ---------------------------------------------------------------- 回退图

@dataclass
class FallbackGraph:
    parents: dict[str, Optional[str]]   # locale -> 声明的父（可能指向缺失语言）

    def chain(self, locale: str) -> tuple[list[str], Optional[str]]:
        """返回 (locale 起向上的链, 中断原因)。

        中断原因 None 表示链正常终止（父为 null）。
        循环时链中包含重复节点一次，原因 'cycle:<locale>'。
        父语言缺失时原因 'missing:<parent>'。
        """
        chain: list[str] = []
        seen: set[str] = set()
        cur: Optional[str] = locale
        while cur is not None:
            if cur in seen:
                return chain, f"cycle:{cur}"
            seen.add(cur)
            chain.append(cur)
            cur = self.parents.get(cur)
            if cur is not None and cur not in self.parents:
                chain.append(cur)
                return chain, f"missing:{cur}"
        return chain, None

    def find_cycles(self) -> list[list[str]]:
        """确定性地找出所有简单环（返回排序后的节点环）。"""
        cycles: set[tuple[str, ...]] = set()
        for start in sorted(self.parents):
            chain, reason = self.chain(start)
            if reason and reason.startswith("cycle:"):
                rep = reason.split(":", 1)[1]
                idx = chain.index(rep)
                cyc = chain[idx:]
                # 规范旋转：从字典序最小节点开始
                k = min(range(len(cyc)), key=lambda i: cyc[i])
                norm = tuple(cyc[k:] + cyc[:k])
                cycles.add(norm)
        return [list(c) for c in sorted(cycles)]


# ---------------------------------------------------------------- 分析

@dataclass
class AnalysisResult:
    base_locale: str
    bundles: dict[str, ParsedBundle]
    graph: FallbackGraph
    issues: list[Issue]
    cycles: list[list[str]]


def _resolve_ref(bundles: dict[str, ParsedBundle], graph: FallbackGraph,
                 locale: str, ref_key: str) -> bool:
    chain, reason = graph.chain(locale)
    if reason and reason.startswith("cycle:"):
        rep = reason.split(":", 1)[1]
        chain = chain[: chain.index(rep) + 1]
    for loc in chain:
        b = bundles.get(loc)
        if b is not None and ref_key in b.entries:
            return True
    return False


def analyze(base_locale: str, parsed: dict[str, ParsedBundle]) -> AnalysisResult:
    parents = {loc: (b.parent_declared if b.parent_declared else None)
               for loc, b in parsed.items()}
    graph = FallbackGraph(parents=parents)
    issues: list[Issue] = []

    base = parsed.get(base_locale)
    cycles = graph.find_cycles()
    cyc_nodes: set[str] = {n for c in cycles for n in c}

    # 1) 循环回退
    for cyc in cycles:
        readable = " -> ".join(cyc + [cyc[0]])
        for node in cyc:
            issues.append(Issue(
                code=T_CYCLE, severity=SEV_ERROR, locale=node, key="<fallback>",
                pos=0, detail=f"回退链存在循环: {readable}",
                expected=f"父链应能到达基准语言 {base_locale}",
                actual=readable,
                fix=Fix(op="set_parent", locale=node, parent=base_locale,
                        auto=False, review=True,
                        reason=f"将 {node} 的父语言直接设为基准语言 {base_locale} 以打破循环"),
                sort_tail=readable))

    # 2) 未覆盖语言：父链无法到达基准语言
    for loc in sorted(parsed):
        if loc in cyc_nodes:
            continue
        chain, reason = graph.chain(loc)
        if reason and reason.startswith("missing:"):
            miss = reason.split(":", 1)[1]
            issues.append(Issue(
                code=T_UNCOVERED, severity=SEV_ERROR, locale=loc,
                key="<fallback>", pos=0,
                detail=f"父语言 {miss!r} 未导入，回退链中断",
                expected=f"链应到达基准语言 {base_locale}",
                actual=" -> ".join(chain),
                fix=Fix(op="set_parent", locale=loc, parent=base_locale,
                        auto=False, review=True,
                        reason=f"将 {loc} 的父语言改为 {base_locale}"),
                sort_tail=f"missing:{miss}"))
        elif chain[-1] != base_locale:
            issues.append(Issue(
                code=T_UNCOVERED, severity=SEV_ERROR, locale=loc,
                key="<fallback>", pos=0,
                detail=f"回退链未到达基准语言 {base_locale}",
                expected=f"链应到达基准语言 {base_locale}",
                actual=" -> ".join(chain) if chain else "(空)",
                fix=Fix(op="set_parent", locale=loc, parent=base_locale,
                        auto=False, review=True,
                        reason=f"将 {loc} 的父语言设为 {base_locale}"),
                sort_tail="root"))

    if base is None:
        issues.sort(key=Issue.sort_key)
        return AnalysisResult(base_locale, parsed, graph, issues, cycles)

    base_keys = set(base.entries)

    # 3) 基准语言自身的语法 / other 分支检查
    for key in sorted(base_keys):
        shape = base.shapes.get(key)
        if shape is None:
            err = base.syntax_errors[key]
            issues.append(Issue(
                code=T_INVALID_SYNTAX, severity=SEV_ERROR, locale=base_locale,
                key=key, pos=err.pos,
                detail=f"基准消息 ICU 语法错误: {err.message}",
                expected="合法的 ICU MessageFormat",
                actual=base.entries[key][:80],
                sort_tail=err.message))
            continue
        for v, vpath in shape.walk_vars():
            if v.type in ("plural", "selectordinal") and \
                    "other" not in {c.label for c in v.cases}:
                issues.append(Issue(
                    code=T_BRANCH, severity=SEV_ERROR, locale=base_locale,
                    key=key, pos=v.pos,
                    detail=f"基准 {v.type} 参数 {{{v.name}}} 缺少必需的 'other' 分支",
                    expected="包含 other 分支",
                    actual=",".join(c.label for c in v.cases),
                    fix=Fix(op="text_patch", locale=base_locale, key=key,
                            auto=True, review=False,
                            patches=[{"start": v.arg_end_pos, "end": v.arg_end_pos,
                                      "replacement": " other{}"}],
                            reason="在参数结束 } 前插入空的 other{} 分支"),
                    sort_tail="base:other"))

        # 基准语言自身的断裂引用
        for rk in sorted({r.key for r in shape.refs}):
            if rk not in base.entries:
                rpos = next((r.pos for r in shape.refs if r.key == rk), 0)
                issues.append(Issue(
                    code=T_BROKEN_REF, severity=SEV_ERROR, locale=base_locale,
                    key=key, pos=rpos,
                    detail=f"引用键 {rk!r} 在基准语言中不存在",
                    expected=f"键 {rk!r} 存在", actual="<无法解析>",
                    sort_tail=rk))

    # 4) 各语言逐条检查
    for loc in sorted(parsed):
        bundle = parsed[loc]
        is_base = loc == base_locale

        # 无效资源叶子（非字符串值）
        for inv in bundle.invalid:
            issues.append(Issue(
                code=T_INVALID_RESOURCE, severity=SEV_WARNING, locale=loc,
                key=inv.key, pos=getattr(inv, "pos", 0),
                detail=f"资源值不是字符串（{inv.value_repr[:60]}）",
                expected="字符串", actual=inv.kind,
                fix=Fix(op="delete", locale=loc, key=inv.key,
                        auto=True, review=False, reason="删除非字符串资源键"),
                sort_tail="non_string"))

        if is_base:
            continue

        loc_keys = set(bundle.entries)

        # 缺失键：沿父链找不到即报错；建议值取最近祖先（确定性：父链顺序）
        for key in sorted(base_keys - loc_keys):
            chain, reason = graph.chain(loc)
            if reason and reason.startswith("cycle:"):
                rep = reason.split(":", 1)[1]
                chain = chain[: chain.index(rep) + 1]
            inherited = None
            inherited_from = None
            for anc in chain[1:]:
                ab = parsed.get(anc)
                if ab and key in ab.entries:
                    inherited = ab.entries[key]
                    inherited_from = anc
                    break
            if inherited is not None:
                fix = Fix(op="upsert", locale=loc, key=key, value=inherited,
                          auto=False, review=True,
                          reason=f"沿父链从 {inherited_from} 继承值（回退可覆盖，仅警告）")
                sev = SEV_WARNING
                detail = (f"键 {key!r} 缺失，但可由父语言 {inherited_from} 回退覆盖")
            else:
                fix = Fix(op="upsert", locale=loc, key=key,
                          value=base.entries[key], auto=False, review=True,
                          reason=f"复制基准语言 {base_locale} 的值，待人工翻译")
                sev = SEV_ERROR
                detail = f"键 {key!r} 缺失，且父链中无回退值"
            issues.append(Issue(
                code=T_MISSING_KEY, severity=sev, locale=loc, key=key, pos=0,
                detail=detail,
                expected="存在该键（及父链回退）", actual="<缺失>",
                fix=fix, sort_tail=""))

        # 多余键（基准中不存在）：警告
        for key in sorted(loc_keys - base_keys):
            issues.append(Issue(
                code=T_EXTRA_KEY, severity=SEV_WARNING, locale=loc, key=key,
                pos=0, detail=f"键 {key!r} 在基准语言 {base_locale} 中不存在",
                expected="<不存在>", actual="存在",
                fix=Fix(op="delete", locale=loc, key=key,
                        auto=False, review=True, reason="删除基准之外的多余键"),
                sort_tail=""))

        # 逐条消息（仅对共同键做契约比对）
        for key in sorted(base_keys & loc_keys):
            tshape = bundle.shapes.get(key)
            if tshape is None:
                err = bundle.syntax_errors[key]
                issues.append(Issue(
                    code=T_INVALID_SYNTAX, severity=SEV_ERROR, locale=loc,
                    key=key, pos=err.pos,
                    detail=f"ICU 语法错误: {err.message}",
                    expected="合法的 ICU MessageFormat",
                    actual=bundle.entries[key][:80],
                    fix=Fix(op="upsert", locale=loc, key=key,
                            value=base.entries[key], auto=False, review=True,
                            reason=f"暂用基准语言 {base_locale} 文案替换，待人工修正"),
                    sort_tail=err.message))
                continue

            bshape = base.shapes.get(key)
            if bshape is None:
                continue  # 基准语法错误已单独报告

            sub_issues = compare_shapes(bshape, tshape, loc, key,
                                        base.entries[key], bundle.entries[key])
            # 为占位符/引用不一致附加 upsert 修复（以基准值为草稿）
            for it in sub_issues:
                if it.code == T_PLACEHOLDER:
                    it.fix = Fix(
                        op="upsert", locale=loc, key=key,
                        value=base.entries[key], auto=False, review=True,
                        reason="以基准文案为草稿替换，待人工保持占位符契约后翻译")
                elif it.code == T_BRANCH:
                    patch = _branch_patch(bshape, tshape, it,
                                          base.entries[key],
                                          bundle.entries[key])
                    it.fix = patch
            issues.extend(sub_issues)

        # 断裂引用：对该语言所有合法消息检查（含基准之外的多余键）
        for key in sorted(loc_keys):
            tshape = bundle.shapes.get(key)
            if tshape is None:
                continue
            for rk in sorted({r.key for r in tshape.refs}):
                if not _resolve_ref(parsed, graph, loc, rk):
                    rpos = next((r.pos for r in tshape.refs if r.key == rk), 0)
                    issues.append(Issue(
                        code=T_BROKEN_REF, severity=SEV_ERROR, locale=loc,
                        key=key, pos=rpos,
                        detail=f"引用键 {rk!r} 沿父链无法解析",
                        expected=f"键 {rk!r} 存在于本语言或父链中",
                        actual="<无法解析>",
                        sort_tail=rk))

    issues.sort(key=Issue.sort_key)
    return AnalysisResult(base_locale, parsed, graph, issues, cycles)


def _find_var_by_path(shape: Shape,
                      path: tuple[tuple[str, str], ...]) -> Optional[Var]:
    """按带标记路径定位变量：("v",名称) 进入参数，("c",标签) 进入分支。"""
    cur: Shape = shape
    target: Optional[Var] = None
    for kind, name in path:
        if kind == "v":
            target = next((v for v in cur.vars if v.name == name), None)
            if target is None:
                return None
        else:  # 进入当前变量的指定分支体
            if target is None:
                return None
            case = next((c for c in target.cases if c.label == name), None)
            if case is None:
                return None
            cur = case.body
    return target


def _branch_patch(bshape: Shape, tshape: Shape, issue: Issue,
                  base_src: str, trans_src: str) -> Fix:
    """为分支不一致生成确定性文本补丁；无法精确定位时退化为 upsert。"""
    kind, _, label = issue.sort_tail.partition(":")
    path = issue.var_path
    name_text = _path_text(path)
    tv = _find_var_by_path(tshape, path)
    bv = _find_var_by_path(bshape, path)

    if kind in ("selmiss", "plmiss"):
        base_span = None
        if bv is not None:
            for bc in bv.cases:
                if bc.label == label:
                    base_span = base_src[bc.open_pos:bc.close_pos + 1]
        if tv is not None and base_span is not None:
            return Fix(
                op="text_patch", locale=issue.locale, key=issue.key,
                auto=(kind == "plmiss"),
                review=(kind != "plmiss"),
                patches=[{"start": tv.arg_end_pos, "end": tv.arg_end_pos,
                          "replacement": f" {label} {base_span}"}],
                reason=f"在参数 {name_text} 中插入缺失分支 {label!r}"
                       f"（内容取自基准语言，需翻译）")
    if kind == "selextra" and tv is not None:
        matches = [c for c in tv.cases if c.label == label]
        if len(matches) == 1:
            c = matches[0]
            return Fix(
                op="text_patch", locale=issue.locale, key=issue.key,
                auto=True, review=False,
                patches=[{"start": c.kw_pos, "end": c.close_pos + 1,
                          "replacement": ""}],
                reason=f"删除 select 参数 {name_text} 中基准不存在的分支 {label!r}")
    # 退路
    return Fix(op="upsert", locale=issue.locale, key=issue.key,
               value=base_src, auto=False, review=True,
               reason="以基准文案为草稿替换，待人工修正分支结构后翻译")
