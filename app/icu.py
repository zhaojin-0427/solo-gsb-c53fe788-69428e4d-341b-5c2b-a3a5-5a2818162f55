"""ICU MessageFormat 子集解析器（零三方依赖）。

支持的语法：
  - 简单变量:           {name}, {count, number}, {d, date, short}
  - 复数:               {count, plural, =0 {...} one {...} other {...}}
  - 选择:               {gender, select, male {...} female {...} other {...}}
  - 选择序数:           {n, selectordinal, one {...} other {...}}
  - 引用键（本工具约定）: {@some.key}
  - 转义:               '' 表示字面单引号；'{...}' 花括号内无需转义；
                        ICU 双单引号转义规则

解析结果为 :class:`Shape` —— 不追求格式还原，只提取占位符名称/类型、
分支标签、嵌套结构、引用键，用于跨语言契约一致性比对。
所有解析为确定性的（无正则回溯歧义、不依赖 dict 顺序）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

PLURAL_KEYWORDS = {"zero", "one", "two", "few", "many", "other"}
SELECTORDINAL_KEYWORDS = PLURAL_KEYWORDS
ARG_TYPES = {"", "number", "date", "time", "plural", "select", "selectordinal",
             "spellout", "duration"}


class IcuError(Exception):
    """带位置的解析错误。"""

    def __init__(self, message: str, pos: int):
        super().__init__(message)
        self.message = message
        self.pos = pos


@dataclass(frozen=True)
class Ref:
    key: str
    pos: int


@dataclass
class Case:
    label: str
    kw_pos: int          # 标签关键字起始位置
    open_pos: int        # '{' 起始
    close_pos: int       # 匹配 '}' 的结束位置（含该字符的索引）
    body: "Shape"


@dataclass
class Var:
    name: str
    type: str            # "" / number / date / plural / select ...
    style: Optional[str]
    pos: int             # '{' 的位置
    arg_end_pos: int     # 匹配 '}' 的位置索引（含）
    cases: list[Case] = field(default_factory=list)
    is_ref: bool = False


@dataclass
class Shape:
    text_len: int = 0                       # 形态覆盖的源文本长度
    vars: list[Var] = field(default_factory=list)
    refs: list[Ref] = field(default_factory=list)
    hash_count: int = 0                     # '#' 出现次数（复数上下文）
    sub_shapes: list["Shape"] = field(default_factory=list)  # 各分支形态，按出现顺序
    # 有序渲染事件（供预览渲染器重放原文）：
    #   ("text", start, end)  原文片段（可能含 ICU 单引号转义，渲染时去义）
    #   ("hash", pos)         复数上下文中的 # 数量符号
    #   ("arg", Var)          占位符（与 vars 中同一对象）
    items: list[tuple] = field(default_factory=list)

    def walk_vars(self) -> list[tuple[Var, tuple[int, ...]]]:
        """返回所有变量及其分支路径（分支索引序列，顶层为空元组）。"""
        out: list[tuple[Var, tuple[int, ...]]] = []

        def rec(shape: Shape, path: tuple[int, ...]) -> None:
            for v in shape.vars:
                out.append((v, path))
                for i, sub in enumerate(v.cases and [c.body for c in v.cases] or []):
                    rec(sub, path + (i,))
        rec(self, ())
        return out


# ---------------------------------------------------------------- 解析器

class _Parser:
    def __init__(self, src: str):
        self.s = src
        self.n = len(src)
        self.i = 0

    def err(self, msg: str, pos: Optional[int] = None) -> IcuError:
        return IcuError(msg, self.i if pos is None else pos)

    def skip_ws(self) -> None:
        s = self.s
        while self.i < self.n and s[self.i] in " \t\r\n":
            self.i += 1

    def parse_sequence(self, stops: frozenset[str], plural_ctx: bool) -> Shape:
        """解析到 stops 中的字符（'}': 由调用方消费）为止。"""
        s = self.s
        shape = Shape()
        start = self.i
        seg_start = self.i   # 当前文字段起点（遇到事件时落盘前序原文）

        def flush(until: int) -> None:
            if until > seg_start:
                shape.items.append(("text", seg_start, until))

        while self.i < self.n:
            c = s[self.i]
            if c in stops:
                flush(self.i)
                shape.text_len = self.i - start
                return shape
            if c == "'":
                self._read_quoted()
                continue
            if c == "#" and plural_ctx:
                flush(self.i)
                shape.hash_count += 1
                shape.items.append(("hash", self.i))
                self.i += 1
                seg_start = self.i
                continue
            if c == "{":
                flush(self.i)
                v = self.parse_arg(plural_ctx)
                shape.vars.append(v)
                shape.items.append(("arg", v))
                if v.is_ref:
                    shape.refs.append(Ref(v.name, v.pos))
                if v.cases:
                    shape.sub_shapes.extend(case.body for case in v.cases)
                seg_start = self.i
                continue
            self.i += 1
        flush(self.n)
        shape.text_len = self.n - start
        if "}" in stops:
            raise self.err("未闭合的 '{'，缺少 '}'")
        return shape

    def _read_quoted(self) -> None:
        """消费单引号片段。规则：'' -> 字面 '；'...' -> 原文。"""
        s = self.s
        assert s[self.i] == "'"
        # 双单引号 = 转义的单引号
        if self.i + 1 < self.n and s[self.i + 1] == "'":
            self.i += 2
            return
        self.i += 1
        while self.i < self.n:
            if s[self.i] == "'":
                # 单个结束引号（'' 在引号内按 ICU 规则也是字面引号）
                if self.i + 1 < self.n and s[self.i + 1] == "'":
                    self.i += 2
                    continue
                self.i += 1
                return
            self.i += 1
        raise self.err("未闭合的引号 \"'\"")

    def parse_arg(self, plural_ctx: bool) -> Var:
        s = self.s
        open_pos = self.i
        self.i += 1  # {
        self.skip_ws()
        name = self._read_name()
        if not name:
            raise self.err("占位符名称为空", open_pos)
        is_ref = name.startswith("@")
        if is_ref:
            ref_key = name[1:]
            self.skip_ws()
            if self.i >= self.n or s[self.i] != "}":
                raise self.err("引用键 {@...} 不允许类型声明")
            self.i += 1  # }
            if not ref_key:
                raise self.err("引用键名称为空", open_pos)
            return Var(name=ref_key, type="ref", style=None,
                       pos=open_pos, arg_end_pos=self.i - 1, is_ref=True)

        self.skip_ws()
        # 无类型：{name}
        if self.i < self.n and s[self.i] == "}":
            self.i += 1
            return Var(name=name, type="", style=None,
                       pos=open_pos, arg_end_pos=self.i - 1)

        if self.i >= self.n or s[self.i] != ",":
            raise self.err("占位符中期望 ',' 或 '}'")
        self.i += 1
        self.skip_ws()
        arg_type = self._read_type_word()
        if arg_type not in ARG_TYPES:
            raise self.err(f"未知占位符类型 {arg_type!r}")
        self.skip_ws()

        if arg_type in ("plural", "select", "selectordinal"):
            if self.i >= self.n or s[self.i] != ",":
                raise self.err(f"{arg_type} 后缺少 ','")
            self.i += 1
            self.skip_ws()
            cases = self.parse_cases(arg_type)
            self.skip_ws()
            if self.i >= self.n or s[self.i] != "}":
                raise self.err(f"{arg_type} 参数缺少结束 '}}'")
            end = self.i
            self.i += 1
            return Var(name=name, type=arg_type, style=None, pos=open_pos,
                       arg_end_pos=end, cases=cases)

        # 简单类型：{x, number} 或 {x, date, style}
        style: Optional[str] = None
        if self.i < self.n and s[self.i] == ",":
            self.i += 1
            # 样式允许引号包裹，整体原样保留
            style = self._read_simple_style()
        self.skip_ws()
        if self.i >= self.n or s[self.i] != "}":
            raise self.err("简单占位符缺少结束 '}'")
        end = self.i
        self.i += 1
        return Var(name=name, type=arg_type, style=style, pos=open_pos,
                   arg_end_pos=end)

    def _read_name(self) -> str:
        s = self.s
        start = self.i
        if self.i < self.n and s[self.i] == "@":
            self.i += 1
        while self.i < self.n and (s[self.i].isalnum() or s[self.i] in "._@-"):
            self.i += 1
        return s[start:self.i]

    def _read_type_word(self) -> str:
        s = self.s
        start = self.i
        while self.i < self.n and s[self.i].isalpha():
            self.i += 1
        return s[start:self.i]

    def _read_simple_style(self) -> str:
        """读取简单类型的样式段（到匹配的 } 之前），去首尾空白。"""
        s = self.s
        depth = 0
        start = self.i
        while self.i < self.n:
            c = s[self.i]
            if c == "'":
                self._read_quoted()
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                if depth == 0:
                    return s[start:self.i].strip()
                depth -= 1
            self.i += 1
        raise self.err("样式段未结束")

    def parse_cases(self, arg_type: str) -> list[Case]:
        s = self.s
        cases: list[Case] = []
        seen: set[str] = set()
        while True:
            self.skip_ws()
            if self.i >= self.n:
                raise self.err(f"{arg_type} 缺少结束 '}}'")
            if s[self.i] == "}":
                break
            kw_pos = self.i
            label = self._read_case_label()
            if not label:
                raise self.err("缺少分支标签", kw_pos)
            if label in seen:
                raise self.err(f"分支标签 {label!r} 重复", kw_pos)
            if arg_type in ("plural", "selectordinal"):
                if not (label.startswith("=") or label in PLURAL_KEYWORDS):
                    raise self.err(f"非法复数标签 {label!r}", kw_pos)
            seen.add(label)
            self.skip_ws()
            if self.i >= self.n or s[self.i] != "{":
                raise self.err(f"分支 {label!r} 后缺少 '{{'")
            open_pos = self.i
            self.i += 1  # {
            inner_plural = arg_type in ("plural", "selectordinal")
            body = self.parse_sequence(frozenset("}"), inner_plural)
            if self.i >= self.n:
                raise self.err("分支体未闭合")
            close_pos = self.i
            self.i += 1  # }
            cases.append(Case(label=label, kw_pos=kw_pos, open_pos=open_pos,
                              close_pos=close_pos, body=body))
        if not cases:
            raise self.err(f"{arg_type} 至少需要一个分支")
        return cases

    def _read_case_label(self) -> str:
        s = self.s
        start = self.i
        if s[self.i] == "=":
            self.i += 1
            while self.i < self.n and s[self.i].isdigit():
                self.i += 1
            return s[start:self.i]
        while self.i < self.n and (s[self.i].isalnum() or s[self.i] in "-_"):
            self.i += 1
        return s[start:self.i]


def parse_message(src: str) -> Shape:
    """解析 ICU 消息，失败抛 :class:`IcuError`（携带 pos）。"""
    p = _Parser(src)
    shape = p.parse_sequence(frozenset(), False)
    if p.i != p.n:  # 顶层不应提前停止
        raise p.err("解析未到达输入结尾")
    return shape
