"""白名单解析与匹配（规格 §6.3：UTF-8 CSV、固定表头、normalize 后精确匹配）。

CSV 首行表头固定 ``plate,label,valid_from,valid_until``：

    plate,label,valid_from,valid_until
    粤B12345,员工-张,2026-09-01,2027-08-31

- ``plate`` 必填；``valid_from``/``valid_until`` 为 ``YYYY-MM-DD`` 或空（不限）；
  ``label`` ≤ 64 字符。
- 校验失败抛 ``ValueError``，消息带行号（HTTP 层据此返回 400）。
- 匹配：``normalize()`` 后**精确匹配**，不做模糊匹配（安全考虑，§11 D4）；
  命中且在有效期内（含边界）才算命中。

仅标准库。德国制式本期不做（§0.1）。
"""
from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import date

from .charset import normalize

__all__ = ["WhitelistEntry", "WhitelistError", "parse_whitelist", "Whitelist"]

_HEADER = ["plate", "label", "valid_from", "valid_until"]


class WhitelistError(ValueError):
    """白名单 CSV 校验失败；``line`` 为 1 基行号。"""


@dataclass(frozen=True)
class WhitelistEntry:
    plate: str
    label: str
    valid_from: date | None
    valid_until: date | None


def _parse_date(value: str, line: int, column: str) -> date | None:
    if value == "":
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise WhitelistError(f"line {line}: {column} 不是 YYYY-MM-DD: {value!r}") from None


def parse_whitelist(text: str) -> list[WhitelistEntry]:
    """解析 CSV 文本（接受 BOM），返回条目列表；任何错误抛 ``WhitelistError``。"""
    # utf-8-sig 语义：同时接受带与不带 BOM 的输入（§8 P1「BOM」）
    text = text.lstrip("\ufeff")
    reader = csv.reader(io.StringIO(text, newline=""))
    entries: list[WhitelistEntry] = []
    seen: dict[str, int] = {}  # normalized plate -> 首现行号
    header_seen = False
    for lineno, row in enumerate(reader, start=1):
        if not row or (len(row) == 1 and row[0].strip() == ""):
            continue  # 空行跳过
        if not header_seen:
            if row != _HEADER:
                raise WhitelistError(f"line {lineno}: 表头必须为 {','.join(_HEADER)}")
            header_seen = True
            continue
        if len(row) != 4:
            raise WhitelistError(f"line {lineno}: 需要 4 列，得到 {len(row)} 列")
        plate, label, vf_s, vu_s = (c.strip() for c in row)
        if not plate:
            raise WhitelistError(f"line {lineno}: plate 为空")
        if len(label) > 64:
            raise WhitelistError(f"line {lineno}: label 超过 64 字符")
        vf = _parse_date(vf_s, lineno, "valid_from")
        vu = _parse_date(vu_s, lineno, "valid_until")
        if vf and vu and vu < vf:
            raise WhitelistError(f"line {lineno}: valid_until 早于 valid_from")
        key = normalize(plate, "cn")
        if key in seen:
            raise WhitelistError(f"line {lineno}: plate 重复（首见于 line {seen[key]}）: {plate}")
        seen[key] = lineno
        entries.append(WhitelistEntry(plate, label, vf, vu))
    return entries


class Whitelist:
    """normalize 后精确匹配 + 有效期判断。region 固定 cn（本期范围，§0.1）。"""

    def __init__(self, entries: list[WhitelistEntry]):
        self._entries = entries
        self._index = {normalize(e.plate, "cn"): e for e in entries}

    @classmethod
    def from_csv(cls, text: str) -> "Whitelist":
        return cls(parse_whitelist(text))

    def __len__(self) -> int:
        return len(self._entries)

    def entries(self) -> list[WhitelistEntry]:
        return list(self._entries)

    def match(self, plate: str, region: str = "cn", *, on: date | None = None) -> WhitelistEntry | None:
        """命中返回条目；未登记或不在有效期返回 None。"""
        if region != "cn":
            return None
        entry = self._index.get(normalize(plate, region))
        if entry is None:
            return None
        today = on or date.today()
        if entry.valid_from and today < entry.valid_from:
            return None
        if entry.valid_until and today > entry.valid_until:
            return None
        return entry
