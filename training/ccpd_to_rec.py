#!/usr/bin/env python3
"""CCPD → 识别训练标签（NRD-3 §7 / P4）。

按文件名字符索引解码车牌字符串，输出识别训练用的标签文件（每行一条）：

    <相对图片路径>\t<车牌文本>\t<四角点 x1,y1,x2,y2,...（8 个整数，逗号分隔）>

裁切（按四角点透视矫正到 96×32）由 P6 训练脚本完成——本转换只用标准库，
不解码图片像素，可在 spark 的裸 python3 下运行。

绿牌第 3 位实证分布 {3,5,24,33} = {D,F,0,9}（新能源 D/F 占 99.8%），
个别标注噪声（'0'/'9' 落在 D/F 位）按 --strict 过滤：默认保留并计数，
--strict 时只保留满足大陆单行牌正则（蓝 7 位 / 绿 8 位）的样本。

用法（spark，数据目录内）：

    python3 training/ccpd_to_rec.py --src data/raw/ccpd/CCPD2020/ccpd_green \
        --out data/processed/ccpd_rec

输出：<out>/ccpd2020_green_{split}.txt（split ∈ train,val,test）。
CCPD 图片与产物只留在 spark 数据目录，不入库不分发（§7 隐私要求）。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from ccpd_to_coco import parse_ccpd_name  # noqa: E402

# §6.4 大陆单行车牌正则（蓝 7 位 / 绿 8 位）；省份字符集与 ccpd_to_coco.PROVINCES 一致
_PROV = "皖沪津渝冀晋蒙辽吉黑苏浙京闽赣鲁豫鄂湘桂琼川贵云藏陕青宁新警学O使港澳"
_LETTER = "A-HJ-NP-Z"
CN_PLATE_RE = re.compile(
    f"^[{_PROV}][{_LETTER}][{_LETTER}0-9]{{5}}$|^[{_PROV}][{_LETTER}][{_LETTER}0-9]{{6}}$"
)


def to_rec(src: Path, split: str, strict: bool, out_file: Path) -> int:
    """解析 src 下全部 .jpg，写标签文件；返回保留条数。"""
    files = sorted(p for p in src.rglob("*.jpg"))
    good = bad = 0
    lines = []
    for img_path in files:
        rel = img_path.relative_to(src).as_posix()
        try:
            info = parse_ccpd_name(img_path.name)
        except ValueError as e:
            print(f"SKIP {rel}: {e}", file=sys.stderr)
            bad += 1
            continue
        if strict and not CN_PLATE_RE.fullmatch(info.plate):
            bad += 1
            continue
        corners = ",".join(str(c) for pt in info.vertices for c in pt)
        lines.append(f"{rel}\t{info.plate}\t{corners}")
        good += 1
    out_file.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    print(f"{split}: {good} labels, {bad} skipped")
    return good


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CCPD -> recognition label files (NRD-3 P4)")
    ap.add_argument("--src", required=True, type=Path,
                    help="CCPD 根目录（含 train/val/test 子目录）或单个图片目录")
    ap.add_argument("--out", required=True, type=Path, help="输出目录")
    ap.add_argument("--splits", default="train,val,test", help="逗号分隔；src 为单目录时忽略")
    ap.add_argument("--split", default=None, help="src 为单目录时的输出 split 名（默认 'all'）")
    ap.add_argument("--strict", action="store_true",
                    help="只保留满足大陆单行牌正则的样本（过滤标注噪声）")
    ap.add_argument("--tag", default="ccpd2020_green", help="输出文件名前缀（见 ccpd_to_coco.py --tag）")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    total = 0
    splits: list[tuple[str, Path]]
    if (args.src / "train").is_dir() or (args.src / "val").is_dir():
        splits = [(s, args.src / s) for s in args.splits.split(",") if (args.src / s).is_dir()]
    else:
        splits = [(args.split or "all", args.src)]
    for split, d in splits:
        out_file = args.out / f"{args.tag}_{split}.txt"
        total += to_rec(d, split, args.strict, out_file)
    print(f"total labels: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
