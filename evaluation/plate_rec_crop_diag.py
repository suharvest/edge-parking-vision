#!/usr/bin/env python3
"""Recognizer-only diagnostic: same images, two ways to cut the plate.

* quad: CCPD four-corner perspective warp to 96x32 (training/train_platerec.py
  warp_crop, the crop the recognizer was trained on).
* axis: the GT axis-aligned box expanded 0.08 per side and bilinear-sampled
  to 96x32 (what the runtime stage2 does with a detector box).

Isolates how much of the end-to-end gap comes from the crop geometry rather
than from detection. ONNX FP32, spark only.
"""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("e2e", HERE / "plate_e2e_eval.py")
e2e = importlib.util.module_from_spec(spec)
spec.loader.exec_module(e2e)


def corners_from_name(name: str) -> np.ndarray:
    pts = [tuple(int(v) for v in p.split("&")) for p in name.split("-")[3].split("_")]
    return np.array(pts, np.float32)  # RB, LB, LT, RT


def quad_crop(img_rgb: np.ndarray, c: np.ndarray) -> np.ndarray:
    dst = np.array([[0, 0], [95, 0], [95, 31], [0, 31]], np.float32)
    src = np.array([c[2], c[3], c[0], c[1]], np.float32)
    m = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img_rgb, m, (96, 32), flags=cv2.INTER_LINEAR).astype(np.float32)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", type=Path, required=True)
    ap.add_argument("--det", required=True)
    ap.add_argument("--rec", required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    rows = [json.loads(l) for l in args.set.read_text(encoding="utf-8").splitlines() if l.strip()]
    be = e2e.OnnxBackend(args.det, args.rec)
    stats: dict[str, dict[str, int]] = {}
    for r in rows:
        img = e2e.load_rgb(r["path"])
        h, w = img.shape[:2]
        box = e2e.expand_box(r["bbox"], w, h)
        q = e2e.ctc_greedy(be.run_rec(quad_crop(img, corners_from_name(Path(r["path"]).name))))[0]
        a = e2e.ctc_greedy(be.run_rec(e2e.crop_rec(img, box)))[0]
        for key in (r["subset"], r["tier"], "all"):
            s = stats.setdefault(key, {"n": 0, "quad_exact": 0, "axis_gt_exact": 0})
            s["n"] += 1
            s["quad_exact"] += q == r["plate"]
            s["axis_gt_exact"] += a == r["plate"]
    summ = {k: {"n": v["n"], "quad_exact": round(v["quad_exact"] / v["n"], 4),
                "axis_gt_exact": round(v["axis_gt_exact"] / v["n"], 4)} for k, v in stats.items()}
    args.out.write_text(json.dumps({"set": str(args.set), "rec": args.rec, "summary": summ},
                                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps(summ, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
