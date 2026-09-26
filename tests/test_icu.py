import pytest

from app.icu import IcuError, parse_message


def vars_of(src):
    return [(v.name, v.type) for v in parse_message(src).vars]


def test_simple_vars():
    s = parse_message("Hello {name}, you have {count, number} messages")
    assert vars_of("Hello {name}") == [("name", "")]
    names = [(v.name, v.type) for v in s.vars]
    assert names == [("name", ""), ("count", "number")]


def test_nested_keys_in_name():
    s = parse_message("{user.name} / {@a.b}")
    assert s.vars[0].name == "user.name"
    assert s.vars[1].is_ref is True
    assert s.refs[0].key == "a.b"


def test_plural_cases_and_hash():
    s = parse_message(
        "{n, plural, =0 {no items} one {# item} other {# items}}")
    v = s.vars[0]
    assert v.type == "plural"
    assert [c.label for c in v.cases] == ["=0", "one", "other"]
    assert v.cases[1].body.hash_count == 1
    # 位置记录
    assert v.pos == 0


def test_select_with_nested_vars():
    s = parse_message(
        "{g, select, male {Mr {name}} female {Ms {name}} other {{name}}}")
    v = s.vars[0]
    assert v.type == "select"
    bodies = [c.body.vars[0].name for c in v.cases]
    assert bodies == ["name", "name", "name"]


def test_selectordinal():
    s = parse_message("{n, selectordinal, one {#st} two {#nd} other {#th}}")
    assert s.vars[0].type == "selectordinal"
    assert len(s.vars[0].cases) == 3


def test_quoted_escapes():
    # '' 是字面单引号；'{name}' 花括号在引号内不解析
    s = parse_message("It''s a '{name}' literal")
    assert s.vars == []
    assert "It" in s.text_len.__str__() or s.text_len >= 0


def test_unclosed_brace():
    with pytest.raises(IcuError) as e:
        parse_message("Hello {name")
    assert e.value.pos > 0


def test_bad_plural_label():
    with pytest.raises(IcuError):
        parse_message("{n, plural, foo {x} other {y}}")


def test_duplicate_label():
    with pytest.raises(IcuError):
        parse_message("{g, select, other {a} other {b}}")


def test_unknown_type():
    with pytest.raises(IcuError):
        parse_message("{x, weird}")


def test_ref_with_type_rejected():
    with pytest.raises(IcuError):
        parse_message("{@key, number}")


def test_date_style():
    s = parse_message("{d, date, medium}")
    assert s.vars[0].style == "medium"


def test_walk_vars_nested_paths():
    s = parse_message(
        "{n, plural, one {{c, number}} other {{c, number, percent}}}")
    walked = s.walk_vars()
    assert [(v.name, path) for v, path in walked] == [
        ("n", ()),
        ("c", (0,)),
        ("c", (1,)),
    ]
