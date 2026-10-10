#!/usr/bin/env python3
"""Per-subset before/after table from two plate_e2e_eval.py result JSON files (markdown)."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

ORDER = ["green", "base", "db", "fn", "rotate", "tilt", "weather", "challenge", "blur",
         "held_out_det_rec_seen", "all"]
COLS = [("det_recall", "检测召回"), ("exact", "整牌准确率"), ("char_acc", "字符准确率"),
        ("gated_rate", "门控读数"), ("gated_precision", "门控精度"),
        ("publish_rate", "发布"), ("publish_precision", "发布精度"),
        ("published_correct_rate", "正确发布")]


def pct(v):
    return "—" if v is None else f"{v * 100:.1f}%"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--before", type=Path, required=True)
    ap.add_argument("--after", type=Path, required=True)
    ap.add_argument("--cols", default=",".join(c for c, _ in COLS))
    args = ap.parse_args(argv)
    a = json.loads(args.before.read_text(encoding="utf-8"))["summary"]
    b = json.loads(args.after.read_text(encoding="utf-8"))["summary"]
    cols = [c for c in COLS if c[0] in args.cols.split(",") and c[0] in b.get("all", {})]
    print("| 子集 | 张数 | " + " | ".join(f"{n} 前→后" for _, n in cols) + " |")
    print("|---|---|" + "---|" * len(cols))
    for k in ORDER + [k for k in b if k not in ORDER]:
        if k not in b or k not in a:
            continue
        cells = [f"{pct(a[k].get(c))} → **{pct(b[k].get(c))}**" for c, _ in cols]
        print(f"| {k} | {b[k]['n']} | " + " | ".join(cells) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
