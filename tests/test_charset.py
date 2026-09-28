"""字符集、normalize、plate_type 与码点模式匹配（§6.3、§6.4、§8 P1）。

``match_patterns`` 与 Python 正则逐条一致（fixture 为 cn，§0.1 本期范围）。
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from core_parking.plate import charset as cs

FIXTURE = Path(__file__).parent / "fixtures" / "plate_accept.json"


def _cases() -> list[dict]:
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    cases = data["cases"]
    assert len(cases) >= 20  # §6.4：每制式 ≥ 20 条（本期只有 cn）
    return cases


def test_charset_cn():
    table = cs.charset("cn")
    assert table[0] == "<blank>"
    assert len(table) == 1 + 31 + 24 + 10
    assert len(set(table)) == len(table)
    assert "I" not in table and "O" not in table
    assert "学" not in table  # 特种字符不在字符集（§2 不做特种牌）


def test_charset_unknown_region():
    with pytest.raises(ValueError):
        cs.charset("us")


def test_normalize():
    assert cs.normalize(" 京a-12345 ", "cn") == "京A12345"
    assert cs.normalize("京A·1234O", "cn") == "京A12340"
    assert cs.normalize("京A1I34O", "cn") == "京A11340"


def test_plate_type():
    assert cs.plate_type(cs.normalize("粤B12345", "cn"), "cn") == "cn_blue"
    assert cs.plate_type(cs.normalize("粤BD12345", "cn"), "cn") == "cn_green"
    assert cs.plate_type("粤B1234", "cn") is None
    assert cs.plate_type("粤B12345", "us") is None


def test_match_patterns_agrees_with_regex():
    patterns = cs.accept_patterns("cn")
    for case in _cases():
        text, expect = case["text"], case["expect"]
        regex = bool(cs.CN_BLUE_RE.fullmatch(text) or cs.CN_GREEN_RE.fullmatch(text))
        assert regex == expect, f"fixture expectation wrong for {text!r}"
        assert cs.match_patterns(text, patterns) == expect, f"match_patterns != regex for {text!r}"


def test_accept_pattern_shape():
    for pattern in cs.accept_patterns("cn"):
        for seg in pattern:
            assert set(seg) == {"set", "min", "max"} and seg["min"] <= seg["max"]
