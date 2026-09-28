"""白名单解析与匹配（§6.3、§8 P1）。德国带连字符用例本期跳过（§0.1）。"""
from __future__ import annotations

import pytest

from core_parking.plate.whitelist import Whitelist, WhitelistError, parse_whitelist

HEADER = "plate,label,valid_from,valid_until"


def test_parse_ok():
    entries = parse_whitelist(
        f"{HEADER}\n"
        "粤B12345,员工-张,2026-09-01,2027-08-31\n"
        "京A12345,,,\n"
    )
    assert len(entries) == 2
    assert entries[0].plate == "粤B12345"
    assert entries[0].label == "员工-张"
    assert entries[1].valid_from is None and entries[1].valid_until is None


def test_match_validity_window():
    wl = Whitelist.from_csv(
        f"{HEADER}\n"
        "粤B12345,员工,2026-09-01,2027-08-31\n"
        "京A12345,访客,,\n"
    )
    import datetime as dt
    d = dt.date
    assert wl.match("粤B12345", on=d(2026, 9, 1)) is not None      # 含边界
    assert wl.match("粤B12345", on=d(2027, 8, 31)) is not None
    assert wl.match("粤B12345", on=d(2026, 8, 31)) is None         # 未生效
    assert wl.match("粤B12345", on=d(2027, 9, 1)) is None          # 过期
    assert wl.match("京A12345", on=d(2020, 1, 1)) is not None      # 不限日期
    assert wl.match("京A12346", on=d(2026, 10, 1)) is None         # 未登记


def test_match_normalizes():
    wl = Whitelist.from_csv(f"{HEADER}\n京A12340,,,\n京A11340,,,\n")
    # 识别常见误读：小写、空格、间隔号、O→0、I→1（§6.3 normalize）
    assert wl.match("京A12340") is not None
    assert wl.match(" 京a12340 ") is not None
    assert wl.match("京A1234O") is not None      # O→0
    assert wl.match("京A1I34O") is not None      # I→1 且 O→0


def test_bad_header():
    with pytest.raises(WhitelistError, match="表头"):
        parse_whitelist("plate,label,valid_from\n京A12345,,,\n")
    with pytest.raises(WhitelistError, match="表头"):
        parse_whitelist("京A12345,,,\n")


def test_duplicate():
    with pytest.raises(WhitelistError, match="重复"):
        parse_whitelist(f"{HEADER}\n京A12345,,,\n京A12345,访客,,\n")
    # normalize 后相同也算重复（京A1234O → 京A12340）
    with pytest.raises(WhitelistError, match="重复"):
        parse_whitelist(f"{HEADER}\n京A12340,,,\n京A1234O,,,\n")


def test_blank_lines_and_trailing_newline():
    entries = parse_whitelist(f"{HEADER}\n\n京A12345,,,\n\n\n粤B12345,,,\n")
    assert len(entries) == 2


def test_bom():
    text = "﻿" + f"{HEADER}\n京A12345,,,\n"
    assert len(parse_whitelist(text)) == 1


def test_field_errors_carry_line_number():
    with pytest.raises(WhitelistError, match="line 2.*plate 为空"):
        parse_whitelist(f"{HEADER}\n,标签,,\n")
    with pytest.raises(WhitelistError, match="line 3.*YYYY-MM-DD"):
        parse_whitelist(f"{HEADER}\n京A12345,,,\n粤B12345,,2026/09/01,\n")
    with pytest.raises(WhitelistError, match="line 2.*64"):
        parse_whitelist(f"{HEADER}\n京A12345,{'x' * 65},,\n")
    with pytest.raises(WhitelistError, match="line 2.*4 列"):
        parse_whitelist(f"{HEADER}\n京A12345,标签\n")
    with pytest.raises(WhitelistError, match="line 2.*早于"):
        parse_whitelist(f"{HEADER}\n京A12345,,2027-01-01,2026-01-01\n")
