"""消息实例预览：沿显式父语言链解析消息并用变量渲染最终文本（只读）。

语义
----
- 用户选择 *目标语言* ``locale`` 与消息键 ``key``：消息本身先沿该语言的显式
  父语言链解析（本语言优先、逐级向上），得到「实际取值语言」与回退轨迹。
- 消息内的跨语言引用 ``{@key}`` 从 **引用所在消息的语言** 重新沿其父链解析
  （近遮蔽优先），因此被引用消息可能落在另一个语言；其变量与外层共用同一组
  输入变量。
- 数字、日期/时间与复数/序数分支一律按 **目标语言** 的 CLDR 规则处理
  （Babel）；``#`` 数量符号按目标语言默认十进制格式输出当前复数参数。
- plural/selectordinal 分支选择：精确数值分支 ``=N`` 优先，其次 CLDR 类别
  （zero/one/two/few/many），最后回退 ``other``；都没有则报错。
- 日期时间参数接受 ISO 8601 字符串。

任一语言渲染失败（缺参数、类型不符、无可用分支、断裂引用、引用环、非法 ICU
等）都抛 :class:`RenderError`——携带语言、键与结构化路径；调用方逐语言隔离，
出错语言不输出任何部分文本，其余语言照常预览。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from babel import localedata
from babel.core import Locale, UnknownLocaleError
from babel.dates import format_date, format_datetime, format_time
from babel.numbers import (format_compact_decimal, format_currency,
                          format_decimal, format_percent, format_scientific)
from babel.plural import to_python

from .analyzer import AnalysisResult, FallbackGraph, ParsedBundle
from .icu import Shape, Var

# ---------------------------------------------------------------- 错误码

MISSING_PARAM = "MISSING_PARAM"
TYPE_MISMATCH = "TYPE_MISMATCH"
NO_BRANCH = "NO_BRANCH"
BROKEN_REFERENCE = "BROKEN_REFERENCE"
REFERENCE_CYCLE = "REFERENCE_CYCLE"
MESSAGE_NOT_FOUND = "MESSAGE_NOT_FOUND"
INVALID_MESSAGE = "INVALID_MESSAGE"
UNSUPPORTED_LOCALE = "UNSUPPORTED_LOCALE"
UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
FORMAT_ERROR = "FORMAT_ERROR"


class RenderError(Exception):
    """携带语言、键、结构化路径的渲染错误（不输出部分文本）。"""

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


@dataclass
class Hop:
    """一次 {@key} 引用展开（按实际求值顺序记录）。"""
    ref_key: str
    from_locale: str
    from_key: str
    to_locale: str
    to_key: str
    chain: list[str]

    def to_dict(self) -> dict:
        return {"ref_key": self.ref_key,
                "from": {"locale": self.from_locale, "key": self.from_key},
                "to": {"locale": self.to_locale, "key": self.to_key},
                "chain": list(self.chain)}


@dataclass
class RenderResult:
    text: str
    target_locale: str
    key: str
    resolved_locale: str
    fell_back: bool
    fallback_chain: list[str]
    hops: list[Hop] = field(default_factory=list)


# ---------------------------------------------------------------- Babel 缓存

class _LocaleRules:
    """目标语言的 Babel 句柄与 CLDR 单复数规则（惰性、按语言缓存）。"""

    def __init__(self, locale_id: str):
        norm = locale_id.replace("-", "_")
        try:
            babel_loc = Locale.parse(norm)
            data = localedata.load(str(babel_loc))
        except (UnknownLocaleError, ValueError, FileNotFoundError) as e:
            raise RenderError(
                UNSUPPORTED_LOCALE, locale_id, "",
                f"语言 {locale_id!r} 缺少可用的 CLDR 数据（{e}）")
        self.id = str(babel_loc)
        self.cardinal = to_python(data["plural_form"])
        ordinal_form = data.get("ordinal_form")
        self.ordinal = (to_python(ordinal_form) if ordinal_form is not None
                        else self.cardinal)


_cache: dict[str, _LocaleRules] = {}


def _rules(locale_id: str) -> _LocaleRules:
    r = _cache.get(locale_id)
    if r is None:
        r = _LocaleRules(locale_id)
        _cache[locale_id] = r
    return r


# ---------------------------------------------------------------- 原文去义

def _unquote(s: str) -> str:
    """去除 ICU 单引号转义，规则与 icu._Parser._read_quoted 完全一致。

    '' -> 字面 '；'...' 内的字符原样输出（{、}、# 不再有结构含义）。
    """
    out: list[str] = []
    n = len(s)
    i = 0
    quoted = False
    while i < n:
        c = s[i]
        if c == "'":
            if i + 1 < n and s[i + 1] == "'":
                out.append("'")
                i += 2
                continue
            quoted = not quoted
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


# ---------------------------------------------------------------- 参数取值

def _as_decimal(v: Any) -> Decimal:
    if isinstance(v, bool):
        raise InvalidOperation
    if isinstance(v, int):
        return Decimal(v)
    if isinstance(v, float):
        return Decimal(repr(v))
    raise InvalidOperation


def _require_number(value: Any, locale: str, key: str,
                    path: list[str], what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RenderError(
            TYPE_MISMATCH, locale, key,
            f"{what} 需要数字（number），实际为 {_type_name(value)}", path)
    try:
        return _as_decimal(value)
    except InvalidOperation:
        raise RenderError(
            TYPE_MISMATCH, locale, key,
            f"{what} 不是合法数字", path)


def _type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    if isinstance(v, list):
        return "array"
    if isinstance(v, dict):
        return "object"
    return type(v).__name__


# ---------------------------------------------------------------- 格式化

def _format_number(value: Decimal, style: Optional[str], rules: _LocaleRules,
                   locale: str, key: str, path: list[str]) -> str:
    """number 占位符。支持命名样式、LDML 数字模式与 ``::`` 骨架子集。"""
    style = (style or "").strip()
    loc = rules.id
    try:
        if not style:
            return format_decimal(value, locale=loc)
        if style == "integer":
            return format_decimal(value, format="#", locale=loc)
        if style == "percent":
            return format_percent(value, locale=loc)
        if style == "permille":
            return f"{format_decimal(value * 1000, locale=loc)}‰"
        if style == "scientific":
            return format_scientific(value, locale=loc)
        if style.startswith("compact"):
            return format_compact_decimal(
                value, format_type="long" if style.endswith("long") else "short",
                locale=loc)
        if style.startswith("::"):
            skel = style[2:].strip()
            if skel.startswith("currency/"):
                code = skel.split("/", 1)[1].strip()
                return format_currency(value, code, locale=loc)
            if skel in ("percent",):
                return format_percent(value, locale=loc)
            if skel in ("permille",):
                return f"{format_decimal(value * 1000, locale=loc)}‰"
            return format_decimal(value, format=skel, locale=loc)
        # 其余按 LDML 数字模式处理（如 000、#,##0.00）
        return format_decimal(value, format=style, locale=loc)
    except RenderError:
        raise
    except Exception as e:  # Babel 对非法模式/货币码抛 ValueError 等
        raise RenderError(
            FORMAT_ERROR, locale, key,
            f"数字格式 {style!r} 无法应用于 {value}: {e}", path,
            extra={"style": style})


def _parse_iso(value: Any, kind: str, locale: str, key: str,
               path: list[str]):
    if not isinstance(value, str):
        raise RenderError(
            TYPE_MISMATCH, locale, key,
            f"{kind} 参数需要 ISO 8601 字符串，实际为 {_type_name(value)}", path)
    text = value.strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    if kind == "date":
        try:
            return date.fromisoformat(text)
        except ValueError:
            pass
    else:
        try:
            return time.fromisoformat(text)
        except ValueError:
            pass
    raise RenderError(
        TYPE_MISMATCH, locale, key,
        f"{kind} 参数 {value!r} 不是合法的 ISO 8601 字符串"
        "（如 2026-03-09 / 2026-03-09T13:05:07 / 13:05:07）", path)


def _format_date(value: Any, style: Optional[str], rules: _LocaleRules,
                 locale: str, key: str, path: list[str]) -> str:
    dtv = _parse_iso(value, "date", locale, key, path)
    style = (style or "medium").strip()
    try:
        return format_date(dtv, format=style, locale=rules.id)
    except Exception as e:
        raise RenderError(
            FORMAT_ERROR, locale, key,
            f"日期格式 {style!r} 无法应用: {e}", path, extra={"style": style})


def _format_time(value: Any, style: Optional[str], rules: _LocaleRules,
                 locale: str, key: str, path: list[str]) -> str:
    dtv = _parse_iso(value, "time", locale, key, path)
    style = (style or "short").strip()
    try:
        return format_time(dtv, format=style, locale=rules.id)
    except Exception as e:
        raise RenderError(
            FORMAT_ERROR, locale, key,
            f"时间格式 {style!r} 无法应用: {e}", path, extra={"style": style})


# ---------------------------------------------------------------- 分支选择

def _match_case(var: Var, num: Decimal, rule_fn) -> Optional[str]:
    """精确数值分支优先；其次 CLDR 类别；最后 other。无可用分支返回 None。"""
    labels = [c.label for c in var.cases]
    for c in var.cases:
        if c.label.startswith("="):
            try:
                if Decimal(c.label[1:]) == num:
                    return c.label
            except InvalidOperation:
                continue
    category = rule_fn(num)
    if category in labels:
        return category
    if "other" in labels:
        return "other"
    return None


# ---------------------------------------------------------------- 渲染核心

def _chain_for(graph: FallbackGraph, locale: str) -> list[str]:
    chain, reason = graph.chain(locale)
    if reason and reason.startswith("cycle:"):
        rep = reason.split(":", 1)[1]
        chain = chain[: chain.index(rep) + 1]
    return chain


def _resolve(bundles: dict[str, ParsedBundle], graph: FallbackGraph,
             locale: str, key: str) -> tuple[Optional[str], list[str]]:
    """沿 locale 的显式父链解析 key（本语言优先）。返回 (实际语言, 尝试链)。"""
    chain = _chain_for(graph, locale)
    for loc in chain:
        b = bundles.get(loc)
        if b is not None and key in b.entries:
            return loc, chain
    return None, chain


def _render_shape(shape: Shape, src: str, variables: dict[str, Any], *,
                  analysis: AnalysisResult, rules: _LocaleRules,
                  target_locale: str, resolved_locale: str, msg_key: str,
                  path: list[str], stack: list[tuple[str, str]],
                  hops: list[Hop], num_stack: list[Decimal]) -> str:
    bundles, graph = analysis.bundles, analysis.graph
    parts: list[str] = []

    for kind, *rest in shape.items:
        if kind == "text":
            start, end = rest
            parts.append(_unquote(src[start:end]))
        elif kind == "hash":
            if not num_stack:
                # 解析器只在复数分支体内产出 # 事件，此处理论不可达
                raise RenderError(INVALID_MESSAGE, target_locale, msg_key,
                                  "# 出现在复数上下文之外", path)
            parts.append(format_decimal(num_stack[-1], locale=rules.id))
        else:
            v: Var = rest[0]
            var_path = path + [v.name]
            if v.is_ref:
                parts.append(_render_ref(
                    v, analysis=analysis, rules=rules,
                    target_locale=target_locale, variables=variables,
                    path=path, stack=stack, hops=hops, num_stack=num_stack))
                continue

            if v.name not in variables:
                raise RenderError(
                    MISSING_PARAM, target_locale, msg_key,
                    f"缺少变量 {v.name!r}（{v.type or 'string'}）", var_path)
            value = variables[v.name]

            if v.type == "":
                if isinstance(value, bool) or value is None or \
                        isinstance(value, (dict, list)):
                    raise RenderError(
                        TYPE_MISMATCH, target_locale, msg_key,
                        f"变量 {v.name!r} 需要字符串或数字，实际为 "
                        f"{_type_name(value)}", var_path)
                parts.append(str(value))
            elif v.type == "number":
                num = _require_number(value, target_locale, msg_key,
                                      var_path, f"变量 {v.name!r}")
                parts.append(_format_number(num, v.style, rules,
                                            target_locale, msg_key, var_path))
            elif v.type == "date":
                parts.append(_format_date(value, v.style, rules,
                                          target_locale, msg_key, var_path))
            elif v.type == "time":
                parts.append(_format_time(value, v.style, rules,
                                          target_locale, msg_key, var_path))
            elif v.type in ("spellout", "duration"):
                raise RenderError(
                    UNSUPPORTED_TYPE, target_locale, msg_key,
                    f"暂不支持的占位符类型 {v.type!r}（变量 {v.name!r}）",
                    var_path)
            elif v.type in ("plural", "selectordinal"):
                num = _require_number(value, target_locale, msg_key,
                                      var_path, f"变量 {v.name!r}")
                rule_fn = (rules.ordinal if v.type == "selectordinal"
                           else rules.cardinal)
                label = _match_case(v, num, rule_fn)
                if label is None:
                    raise RenderError(
                        NO_BRANCH, target_locale, msg_key,
                        f"{v.type} 变量 {v.name!r}={num} 无可用分支"
                        f"（已有：{', '.join(c.label for c in v.cases)}）",
                        var_path, extra={"value": str(num),
                                         "available": [c.label for c in v.cases]})
                case = next(c for c in v.cases if c.label == label)
                num_stack.append(num)
                parts.append(_render_shape(
                    case.body, src,  # 分支体与所属消息共用同一源串与偏移
                    variables, analysis=analysis, rules=rules,
                    target_locale=target_locale,
                    resolved_locale=resolved_locale, msg_key=msg_key,
                    path=path + [f"{v.name}:{v.type}:{label}"],
                    stack=stack, hops=hops, num_stack=num_stack))
                num_stack.pop()
            elif v.type == "select":
                if not isinstance(value, str):
                    raise RenderError(
                        TYPE_MISMATCH, target_locale, msg_key,
                        f"select 变量 {v.name!r} 需要字符串，实际为 "
                        f"{_type_name(value)}", var_path)
                labels = [c.label for c in v.cases]
                label = value if value in labels else (
                    "other" if "other" in labels else None)
                if label is None:
                    raise RenderError(
                        NO_BRANCH, target_locale, msg_key,
                        f"select 变量 {v.name!r}={value!r} 无可用分支"
                        f"（已有：{', '.join(labels)}）", var_path,
                        extra={"value": value, "available": labels})
                case = next(c for c in v.cases if c.label == label)
                parts.append(_render_shape(
                    case.body, src, variables, analysis=analysis,
                    rules=rules, target_locale=target_locale,
                    resolved_locale=resolved_locale, msg_key=msg_key,
                    path=path + [f"{v.name}:select:{label}"],
                    stack=stack, hops=hops, num_stack=num_stack))
            else:
                raise RenderError(
                    UNSUPPORTED_TYPE, target_locale, msg_key,
                    f"未知占位符类型 {v.type!r}（变量 {v.name!r}）", var_path)
    return "".join(parts)


def _render_ref(v: Var, *, analysis: AnalysisResult, rules: _LocaleRules,
                target_locale: str, variables: dict[str, Any],
                path: list[str], stack: list[tuple[str, str]],
                hops: list[Hop], num_stack: list[Decimal]) -> str:
    """渲染 {@key}：从引用所在消息的语言继续沿父链解析。"""
    bundles, graph = analysis.bundles, analysis.graph
    ref_key = v.name
    # stack 顶 = 正在渲染的引用所在消息
    from_locale, from_key = stack[-1]
    ref_path = path + [f"@{ref_key}"]

    loc, chain = _resolve(bundles, graph, from_locale, ref_key)
    if loc is None:
        raise RenderError(
            BROKEN_REFERENCE, target_locale, from_key,
            f"引用 {{@{ref_key}}} 沿父链无法解析（链：{' -> '.join(chain)}）",
            ref_path, extra={"ref_key": ref_key, "chain": list(chain)})

    node = (loc, ref_key)
    if node in stack:
        cycle = [f"{l}:{k}" for l, k in stack[stack.index(node):]]
        cycle.append(f"{loc}:{ref_key}")
        raise RenderError(
            REFERENCE_CYCLE, target_locale, from_key,
            f"检测到引用环：{' -> '.join(cycle)}", ref_path,
            extra={"ref_key": ref_key, "cycle": cycle})

    bundle = bundles[loc]
    shape = bundle.shapes.get(ref_key)
    if shape is None:
        err = bundle.syntax_errors.get(ref_key)
        detail = f"被引用消息 {loc}:{ref_key} 存在 ICU 语法错误"
        if err is not None:
            detail += f"：{err.message}（位置 {err.pos}）"
        raise RenderError(INVALID_MESSAGE, target_locale, ref_key, detail,
                          ref_path)

    hops.append(Hop(ref_key=ref_key, from_locale=from_locale,
                    from_key=from_key, to_locale=loc, to_key=ref_key,
                    chain=list(chain)))
    return _render_shape(
        shape, bundle.entries[ref_key], variables, analysis=analysis,
        rules=rules, target_locale=target_locale, resolved_locale=loc,
        msg_key=ref_key, path=ref_path, stack=stack + [node],
        hops=hops, num_stack=num_stack)


def render_message(analysis: AnalysisResult, target_locale: str, key: str,
                   variables: dict[str, Any]) -> RenderResult:
    """渲染单条消息实例。任何失败抛 :class:`RenderError`。"""
    bundles, graph = analysis.bundles, analysis.graph
    if target_locale not in bundles:
        raise RenderError(
            MESSAGE_NOT_FOUND, target_locale, key,
            f"目标语言 {target_locale!r} 不在该版本中")
    rules = _rules(target_locale)

    loc, chain = _resolve(bundles, graph, target_locale, key)
    if loc is None:
        raise RenderError(
            MESSAGE_NOT_FOUND, target_locale, key,
            f"消息键 {key!r} 沿父链无法解析"
            f"（链：{' -> '.join(chain)}）",
            extra={"chain": list(chain)})

    bundle = bundles[loc]
    shape = bundle.shapes.get(key)
    if shape is None:
        err = bundle.syntax_errors.get(key)
        detail = f"消息 {loc}:{key} 存在 ICU 语法错误"
        if err is not None:
            detail += f"：{err.message}（位置 {err.pos}）"
        raise RenderError(INVALID_MESSAGE, target_locale, key, detail)

    hops: list[Hop] = []
    text = _render_shape(
        shape, bundle.entries[key], variables, analysis=analysis,
        rules=rules, target_locale=target_locale, resolved_locale=loc,
        msg_key=key, path=[], stack=[(loc, key)], hops=hops, num_stack=[])
    return RenderResult(
        text=text, target_locale=target_locale, key=key, resolved_locale=loc,
        fell_back=loc != target_locale, fallback_chain=list(chain),
        hops=hops)
