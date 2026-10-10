#!/usr/bin/env python3
"""RKNN calibration crops for the axis-trained recognizer.

Same approach as the 2026-10-08 conversion (200 CCPD2020 green val plate crops,
files stored in model channel order RGB), but the crops are the runtime stage-2
crops (detector box + 0.08 expansion, bilinear) taken from the axisctx store
built by gen_platerec_axis_crops.py, without jitter. Green val is not part of
the evaluation set.

    .venv/bin/python training/make_platerec_axis_calib.py \
        --axisctx-dir data/processed/platerec_axisctx --out <dir> --n 200
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_platerec import axisctx_crop  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--axisctx-dir", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--n", type=int, default=200)
    ap.add_argument("--expand", type=float, default=0.08)
    args = ap.parse_args(argv)
    rows = [json.loads(l) for l in (args.axisctx_dir / "rows.jsonl").read_text(encoding="utf-8").splitlines()
            if l.strip()]
    val = [r for r in rows if r["split"] == "val"]
    step = max(1, len(val) // args.n)
    pick = val[::step][: args.n]
    crops = args.out / "crops_rgb"
    crops.mkdir(parents=True, exist_ok=True)
    lines, man = [], []
    for k, r in enumerate(pick):
        bgr = cv2.imread(str(args.axisctx_dir / r["png"]), cv2.IMREAD_COLOR)
        rgb = axisctx_crop(bgr[:, :, ::-1], r["box"], r["region_xy"], r["frame_wh"], args.expand)
        u8 = np.clip(np.floor(rgb + 0.5), 0, 255).astype(np.uint8)
        p = crops / f"{k:04d}.png"
        cv2.imwrite(str(p), u8)  # written as-is: cv2.imread returns RGB order (model order)
        lines.append(str(p.resolve()))
        man.append({"file": p.name, "plate": r["plate"], "src_path": r["src_path"],
                    "sha256": hashlib.sha256(p.read_bytes()).hexdigest()})
    (args.out / "dataset_rec.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (args.out / "calib.manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1) + "\n",
                                                  encoding="utf-8")
    print(f"wrote {len(lines)} crops -> {crops}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
