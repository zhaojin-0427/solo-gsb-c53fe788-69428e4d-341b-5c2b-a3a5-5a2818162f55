import pytest

from app.analyzer import (ParsedBundle, analyze, FallbackGraph,
                          T_PLACEHOLDER, T_BRANCH, T_MISSING_KEY,
                          T_CYCLE, T_UNCOVERED, T_BROKEN_REF, T_EXTRA_KEY,
                          SEV_ERROR, SEV_WARNING)
from app.icu import parse_message, IcuError


def make(locale, entries, parent=None, invalid=None):
    pb = ParsedBundle(locale=locale, entries=entries, parent_declared=parent,
                      invalid=invalid or [])
    for k, v in entries.items():
        try:
            pb.shapes[k] = parse_message(v)
        except IcuError as e:
            pb.syntax_errors[k] = e
    return pb


def codes(result, loc=None):
    return [(i.code, i.key) for i in result.issues
            if loc is None or i.locale == loc]


def test_missing_and_extra_key():
    en = make("en", {"a": "A", "b": "B"})
    fr = make("fr", {"a": "A", "c": "C"}, parent="en")
    r = analyze("en", {"en": en, "fr": fr})
    assert (T_MISSING_KEY, "b") in codes(r, "fr")
    assert (T_EXTRA_KEY, "c") in codes(r, "fr")


def test_missing_key_covered_by_fallback_is_warning():
    en = make("en", {"a": "A"})
    pt = make("pt", {"a": "P"})
    br = make("pt_BR", {}, parent="pt")
    r = analyze("en", {"en": en, "pt": pt, "pt_BR": br})
    # pt_BR 没父链到 en，但 a 可从 pt 继承 -> 警告；
    # 同时 pt_BR 根是 pt，未到 en -> UNCOVERED 错误
    miss = [i for i in r.issues if i.code == T_MISSING_KEY and i.locale == "pt_BR"]
    assert len(miss) == 1 and miss[0].severity == SEV_WARNING
    assert any(i.code == T_UNCOVERED and i.locale == "pt_BR" for i in r.issues)


def test_placeholder_name_mismatch():
    en = make("en", {"k": "Hello {name}"})
    fr = make("fr", {"k": "Bonjour {user}"}, parent="en")
    r = analyze("en", {"en": en, "fr": fr})
    pc = [i for i in r.issues if i.locale == "fr" and i.code == T_PLACEHOLDER]
    details = " ".join(i.detail for i in pc)
    assert "name" in details and "user" in details
    # 排序稳定：locale,key,pos,code
    assert r.issues == sorted(r.issues, key=lambda i: i.sort_key())


def test_placeholder_type_mismatch():
    en = make("en", {"k": "{n, number}"})
    fr = make("fr", {"k": "{n}"}, parent="en")
    r = analyze("en", {"en": en, "fr": fr})
    assert any(i.code == T_PLACEHOLDER and i.expected == "number"
               and i.actual == "string" for i in r.issues)


def test_select_branch_must_match_exactly():
    en = make("en", {"k": "{g, select, male {m} female {f} other {o}}"})
    fr = make("fr", {"k": "{g, select, male {m} other {o}}"}, parent="en")
    r = analyze("en", {"en": en, "fr": fr})
    assert any(i.code == T_BRANCH and "female" in i.detail for i in r.issues)


def test_plural_allows_extra_labels_but_requires_base_labels():
    en = make("en", {"k": "{n, plural, one {1} other {#}}"})
    # fr 增加 many（允许），但缺 one（错误）
    fr = make("fr", {"k": "{n, plural, many {x} other {#}}"}, parent="en")
    r = analyze("en", {"en": en, "fr": fr})
    branches = [i for i in r.issues if i.code == T_BRANCH and i.locale == "fr"]
    assert any("one" in i.detail for i in branches)
    assert not any("many" in i.detail for i in branches)


def test_base_plural_requires_other():
    en = make("en", {"k": "{n, plural, one {1}}"})
    r = analyze("en", {"en": en})
    assert any(i.locale == "en" and i.code == T_BRANCH and "other" in i.detail
               for i in r.issues)


def test_cycle_detection():
    en = make("en", {"a": "x"})
    a = make("a", {"a": "x"}, parent="b")
    b = make("b", {"a": "x"}, parent="a")
    r = analyze("en", {"en": en, "a": a, "b": b})
    cyc = [i for i in r.issues if i.code == T_CYCLE]
    assert {i.locale for i in cyc} == {"a", "b"}
    # 循环节点不再重复报未覆盖
    assert not any(i.code == T_UNCOVERED for i in cyc)


def test_missing_parent_is_uncovered():
    en = make("en", {"a": "x"})
    fr = make("fr", {"a": "x"}, parent="de")  # de 未导入
    r = analyze("en", {"en": en, "fr": fr})
    assert any(i.code == T_UNCOVERED and i.locale == "fr"
               and "de" in i.detail for i in r.issues)


def test_broken_reference_along_chain():
    en = make("en", {"a": "{@missing}", "g": "G"})
    # en 本身引用 missing：en 链只有自己 -> 断
    fr = make("fr", {"a": "{@g}"}, parent="en")  # g 在 fr 自身没有，但父链 en 有
    r = analyze("en", {"en": en, "fr": fr})
    broken = [i for i in r.issues if i.code == T_BROKEN_REF]
    assert any(i.locale == "en" and i.key == "a" for i in broken)
    assert not any(i.locale == "fr" for i in broken)


def test_nested_placeholder_consistency():
    en = make("en", {"k": "{g, select, male {Hi {name}} other {Hi}}"})
    fr = make("fr", {"k": "{g, select, male {Salut {user}} other {Salut}}"},
              parent="en")
    r = analyze("en", {"en": en, "fr": fr})
    assert any(i.code == T_PLACEHOLDER and "name" in i.detail for i in r.issues)
    assert any(i.code == T_PLACEHOLDER and "user" in i.detail for i in r.issues)


def test_graph_chain_reason():
    g = FallbackGraph({"en": None, "fr": "en", "de": "xx", "a": "b", "b": "a"})
    assert g.chain("fr") == (["fr", "en"], None)
    chain, reason = g.chain("de")
    assert reason == "missing:xx" and chain[-1] == "xx"
    _, reason2 = g.chain("a")
    assert reason2.startswith("cycle:")


def test_syntax_error_reported_with_position():
    en = make("en", {"k": "{n, plural, one {x}}"})  # base 缺 other + 译文语法
    fr = make("fr", {"k": "{name"}, parent="en")
    r = analyze("en", {"en": en, "fr": fr})
    syn = [i for i in r.issues if i.locale == "fr" and i.code == "INVALID_SYNTAX"]
    assert syn and syn[0].pos >= 0
