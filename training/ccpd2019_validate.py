#!/usr/bin/env python3
"""CCPD2019（蓝牌）文件名格式实证验证（NRD-3 P4，验收附件）。

不假设 CCPD2019 与 CCPD2020 格式相同。对文件名做全量/抽样统计：

  - 破折号字段数、area 值个数、四角点个数、字符索引个数的分布
  - 各位置字符索引范围（省份位 / 发牌机关位 / 序号位）
  - 序号位中数字段（letters-first 表下 index 24-33）占比——用来佐证
    「24 字母在前、数字在后」的序号表对蓝牌同样成立：蓝牌后 5 位以数字为主，
    若表序相反（数字在前），数字会落在 0-9 而不是 24-33
  - 解码文本对 §6.4 蓝牌正则的匹配率

用法（GPU x86 工作站裸 python3，只用标准库）：

    python3 training/ccpd2019_validate.py --src data/raw/ccpd/CCPD2019/ccpd_base [--limit 5000]
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ccpd_to_coco import ADS, PLATE_LETTERS, PROVINCES, parse_ccpd_name  # noqa: E402

BLUE_RE = re.compile(
    f"^[{PROVINCES}][A-HJ-NP-Z][A-HJ-NP-Z0-9]{{5}}$"
)
GREEN_RE = re.compile(
    f"^[{PROVINCES}][A-HJ-NP-Z][A-HJ-NP-Z0-9]{{6}}$"
)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--limit", type=int, default=0, help="只统计前 N 个文件（0=全量）")
    args = ap.parse_args(argv)

    files = sorted(args.src.rglob("*.jpg"))
    if args.limit:
        files = files[: args.limit]
    print(f"files: {len(files)} (src={args.src})")

    n_dash_fields: Counter[int] = Counter()
    n_area: Counter[int] = Counter()
    n_verts: Counter[int] = Counter()
    n_indices: Counter[int] = Counter()
    pos_max: list[int] = []  # 每个位置观测到的最大索引
    pos_min: list[int] = []
    digit_frac = []  # 序号位（第 3 位起）落在 ADS 数字段（index>=24）的比例，按位置
    blue_ok = green_ok = 0
    bad = 0
    prov_hist: Counter[int] = Counter()
    letter2_hist: Counter[int] = Counter()
    bad_examples: list[str] = []

    for i, p in enumerate(files):
        name = p.name
        stem = name.rsplit(".", 1)[0]
        n_dash_fields[len(stem.split("-"))] += 1
        try:
            info = parse_ccpd_name(name)
        except ValueError as e:
            bad += 1
            if len(bad_examples) < 5:
                bad_examples.append(f"{name}: {e}")
            continue
        n_area[len(info.area)] += 1
        n_verts[len(info.vertices)] += 1
        n_indices[len(info.indices)] += 1
        if not pos_max:
            pos_max = [0] * len(info.indices)
            pos_min = [10**9] * len(info.indices)
            digit_frac = [[0, 0] for _ in info.indices]  # [digit_count, total] per pos
        for j, idx in enumerate(info.indices):
            pos_max[j] = max(pos_max[j], idx)
            pos_min[j] = min(pos_min[j], idx)
            digit_frac[j][1] += 1
            if j >= 2 and idx >= len(PLATE_LETTERS):  # 数字段
                digit_frac[j][0] += 1
        prov_hist[info.indices[0]] += 1
        letter2_hist[info.indices[1]] += 1
        if BLUE_RE.fullmatch(info.plate):
            blue_ok += 1
        if GREEN_RE.fullmatch(info.plate):
            green_ok += 1

    parsed = sum(n_indices.values())
    print(f"parsed ok: {parsed}, parse failures: {bad}")
    for ex in bad_examples:
        print(f"  FAIL {ex}")
    print(f"dash fields: {dict(n_dash_fields)}")
    print(f"area values: {dict(n_area)}")
    print(f"vertices:    {dict(n_verts)}")
    print(f"indices len: {dict(n_indices)}")
    if pos_max:
        print("per-position index ranges (min..max) and digit-segment fraction:")
        for j in range(len(pos_max)):
            dc, tot = digit_frac[j]
            frac = dc / tot if tot else 0.0
            seg = ""
            if j == 0:
                seg = f"(province table size {len(PROVINCES)})"
            elif j == 1:
                seg = f"(issuer letter table size {len(PLATE_LETTERS)})"
            else:
                seg = f"(ADS table size {len(ADS)}, digit idx>={len(PLATE_LETTERS)})"
            print(f"  pos{j}: {pos_min[j]:3d}..{pos_max[j]:3d}  digit-frac={frac:.4f} {seg}")
    print(f"blue regex fullmatch: {blue_ok}/{parsed} = {blue_ok / parsed:.4%}" if parsed else "no parsed")
    print(f"green regex fullmatch: {green_ok}/{parsed}" if parsed else "")
    top_prov = ", ".join(f"{PROVINCES[i]}({i}):{c}" for i, c in prov_hist.most_common(5))
    top_l2 = ", ".join(f"{PLATE_LETTERS[i]}({i}):{c}" for i, c in letter2_hist.most_common(5))
    print(f"top provinces: {top_prov}")
    print(f"top issuer letters: {top_l2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
