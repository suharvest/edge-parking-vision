"""字符集、号码格式、normalize 与投票过滤模式（规格 §6.3 normalize、§6.4）。

- ``cn``：31 个省级简称 + 24 个字母（A–Z 去 I、O）+ 10 个数字 + CTC blank。
  蓝牌 7 位、新能源绿牌 8 位（GA 36；序号不使用 I、O）。
- ``de``：A–Z、0–9；`-` 为分隔符不入字符集。Umlaut 本期不支持（§11 D5），
  正则保留 ÄÖÜ 位置以与规格 §6.4 原文一致，但识别字符集与 accept 模式不含
  Umlaut，实际输出不可能命中（fixture 不含 Umlaut 用例）。

投票过滤不下发正则，而下发与正则等价的码点模式（``accept_patterns``，
基座 C++ ``text_vote`` 按 UTF-8 码点匹配，NRD-2 §S13 B6）；``match_patterns``
为其 Python 参考实现，与基座 C++ 实现共用 fixture ``text_match_{cn,de}.json``。
"""
from __future__ import annotations

import re

__all__ = [
    "CN_PROVINCES", "CN_LETTERS", "DIGITS", "DE_LETTERS",
    "CN_BLUE_RE", "CN_GREEN_RE", "DE_RE",
    "normalize", "plate_type", "accept_patterns", "match_patterns", "charset",
]

#: 31 个省级简称（GA 36）
CN_PROVINCES = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼"
#: 24 个字母（A–Z 去 I、O，GA 36 序号不使用 I、O）
CN_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
DIGITS = "0123456789"
#: DE 字符集字母（Umlaut 本期不支持，§11 D5）
DE_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

CN_BLUE_RE = re.compile(r"^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-HJ-NP-Z][A-HJ-NP-Z0-9]{5}$")
CN_GREEN_RE = re.compile(r"^[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼][A-HJ-NP-Z][A-HJ-NP-Z0-9]{6}$")
#: 规格 §6.4 原文（含 ÄÖÜ 位；去掉分隔符后匹配）。Umlaut 不支持见模块 docstring。
DE_RE = re.compile(r"^[A-ZÄÖÜ]{1,3}[A-Z]{1,2}[1-9][0-9]{0,3}[EH]?$")

_FORMATS = {
    "cn": (("cn_blue", CN_BLUE_RE), ("cn_green", CN_GREEN_RE)),
    "de": (("de", DE_RE),),
}


def normalize(plate: str, region: str) -> str:
    """规格 §6.3：大写、去空格/连字符/间隔号/点；cn 另做 O→0、I→1。

    大陆车牌序号不使用 I、O（GA 36，原文需核实），识别把 0/1 误成 O/I 时在此纠回。
    """
    s = plate.upper().replace(" ", "").replace("-", "").replace("·", "").replace(".", "")
    if region == "cn":
        s = s.replace("O", "0").replace("I", "1")
    return s


def plate_type(plate_normalized: str, region: str) -> str | None:
    """返回 ``cn_blue|cn_green|de``；不匹配任何制式返回 None（规格 §6.1）。"""
    for name, rx in _FORMATS.get(region, ()):  # 未知 region → None
        if rx.fullmatch(plate_normalized):
            return name
    return None


def accept_patterns(region: str) -> list[list[dict]]:
    """与正则等价的码点模式（规格 §6.4 原文）。

    每个模式是若干段，每段 ``{"set": 允许的字符（按码点）, "min": 最少次数,
    "max": 最多次数}``；任一模式整串匹配即接受。
    """
    P, L, D = CN_PROVINCES, CN_LETTERS, DIGITS
    if region == "cn":
        return [
            [{"set": P, "min": 1, "max": 1}, {"set": L, "min": 1, "max": 1},
             {"set": L + D, "min": 5, "max": 5}],   # 蓝牌
            [{"set": P, "min": 1, "max": 1}, {"set": L, "min": 1, "max": 1},
             {"set": L + D, "min": 6, "max": 6}],   # 绿牌
        ]
    if region == "de":
        AZ = DE_LETTERS
        return [[{"set": AZ, "min": 1, "max": 3}, {"set": AZ, "min": 1, "max": 2},
                 {"set": "123456789", "min": 1, "max": 1},
                 {"set": D, "min": 0, "max": 3}, {"set": "EH", "min": 0, "max": 1}]]
    raise ValueError(f"unknown region: {region!r}")


def match_patterns(text: str, patterns: list[list[dict]]) -> bool:
    """码点模式回溯匹配（Python 参考实现，与基座 C++ 实现共用 fixture）。"""
    for pattern in patterns:
        if _match_pattern(text, pattern):
            return True
    return False


def _match_pattern(text: str, pattern: list[dict]) -> bool:
    def rec(i: int, seg: int, count: int) -> bool:
        if seg == len(pattern):
            return i == len(text)
        s = pattern[seg]
        # 结束当前段（须已达 min），进入下一段
        if count >= s["min"] and rec(i, seg + 1, 0):
            return True
        # 当前段再吃一个码点
        if count < s["max"] and i < len(text) and text[i] in s["set"]:
            return rec(i + 1, seg, count + 1)
        return False

    return rec(0, 0, 0)


def charset(region: str) -> list[str]:
    """CTC 字符表（index 0 为 blank），写入 ``stage2.charset``；长度即 ``num_classes``。"""
    if region == "cn":
        return ["<blank>"] + list(CN_PROVINCES) + list(CN_LETTERS) + list(DIGITS)
    if region == "de":
        return ["<blank>"] + list(DE_LETTERS) + list(DIGITS)
    raise ValueError(f"unknown region: {region!r}")
