"""资源文件加载：JSON/ARB -> 扁平键值。

约定：
  - 嵌套对象以 "." 连接展开（{a:{b:"x"}} -> "a.b"）。
  - ARB 元数据：顶层 "@key" 键（描述 key 的伴生元数据）与 "@@key"
    （应用级元数据，如 locale 约定）一律跳过；其中 "@@parent"
    为父语言的约定写法，调用方可读取后作为该语言的 fallback 父节点。
  - 非字符串叶子值记录为 invalid 条目（携带确定性的修复建议：删除）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

PARENT_META_KEY = "@@parent"
LOCALE_META_KEY = "@@locale"


class ResourceError(Exception):
    pass


@dataclass
class InvalidLeaf:
    key: str
    kind: str           # "non_string"
    value_repr: str
    pos: int            # 字节偏移在 best-effort 下给 0（JSON 解析失败另行处理）


@dataclass
class LoadedBundle:
    filename: str
    locale: str | None
    parent: str | None          # 来自文件元数据 @@parent
    entries: dict[str, str]    # 保持插入顺序（Python dict 有序）
    invalid: list[InvalidLeaf]
    raw_text: str
    parse_error: str | None = None


def _looks_like_arb(top_keys: list[str]) -> bool:
    return any(k.startswith("@@") for k in top_keys) or any(
        k.startswith("@") for k in top_keys
    )


def load_bundle(filename: str, raw: bytes | str) -> LoadedBundle:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return LoadedBundle(
            filename=filename, locale=None, parent=None, entries={},
            invalid=[], raw_text=raw, parse_error=f"{e.msg} (行 {e.lineno} 列 {e.colno})")
    if not isinstance(data, dict):
        return LoadedBundle(
            filename=filename, locale=None, parent=None, entries={},
            invalid=[], raw_text=raw, parse_error="顶层必须是 JSON 对象")

    top_keys = [str(k) for k in data.keys()]
    arb = _looks_like_arb(top_keys)
    locale = None
    parent = None
    if arb:
        lv = data.get(LOCALE_META_KEY)
        if isinstance(lv, str) and lv:
            locale = lv
        pv = data.get(PARENT_META_KEY)
        if isinstance(pv, str) and pv:
            parent = pv

    entries: dict[str, str] = {}
    invalid: list[InvalidLeaf] = []

    def walk(obj: Any, prefix: str, top_level: bool) -> None:
        if not isinstance(obj, dict):
            return
        for k, v in obj.items():
            key = str(k)
            path = key if not prefix else f"{prefix}.{key}"
            # ARB 元数据键
            if arb and top_level and key.startswith("@"):
                continue
            if isinstance(v, dict):
                walk(v, path, False)
            elif isinstance(v, str):
                entries[path] = v
            else:
                invalid.append(InvalidLeaf(
                    key=path, kind="non_string",
                    value_repr=json.dumps(v, ensure_ascii=False, sort_keys=True),
                    pos=0))

    walk(data, "", True)
    return LoadedBundle(filename=filename, locale=locale, parent=parent,
                        entries=entries, invalid=invalid, raw_text=raw)
