#!/usr/bin/env python3
"""Build the held-out CCPD evaluation list for the end-to-end plate pipeline.

Held-out rules (derived from the training code and the training data on the GPU x86 workstation):

* Detector (plate_det_yolox_tiny_640): trained/validated on 50 000 + 2 500
  ccpd_base images (data/coco/plate_det/{train2017,val2017}/b) and the
  CCPD2020 green train/val splits. Every other CCPD image is unseen by it.
* Recognizer (plate_rec_cn_96x32, training/outputs/platerec_cn_full): trained
  on *all* CCPD2019 subsets except np (training/train_platerec.py
  TRAIN_MANIFESTS, log "train=357701") plus CCPD2020 green train; val =
  CCPD2020 green val. So the only images unseen by BOTH models are
  CCPD2020 green test. CCPD2019 rows are held out for the detector only and
  are reported in a separate tier ("rec-seen").

Output: JSON lines {id, subset, tier, path, plate, bbox:[x1,y1,x2,y2]}.
Only stdlib; run on the GPU x86 workstation where the dataset lives.
"""
from __future__ import annotations

import argparse
import os
import json
import random
from pathlib import Path

SUBSETS_2019 = ["base", "db", "fn", "rotate", "tilt", "weather", "challenge", "blur"]


def read_manifest(path: Path) -> list[tuple[str, str]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rel, plate, _c = line.split("\t")
            rows.append((rel, plate))
    return rows


def bbox_from_name(name: str) -> list[int]:
    a, b = name.split("-")[2].split("_")
    x1, y1 = (int(v) for v in a.split("&"))
    x2, y2 = (int(v) for v in b.split("&"))
    return [x1, y1, x2, y2]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", type=Path, default=Path(os.environ.get("EPV_DATA_ROOT", ".")))
    ap.add_argument("--per-subset-2019", type=int, default=300)
    ap.add_argument("--green-test", type=int, default=1000)
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    root = args.data_root
    rec = root / "data/processed/ccpd_rec"
    det_used = set()
    for split in ("train2017", "val2017"):
        d = root / "data/coco/plate_det" / split / "b"
        det_used.update(p.name for p in d.iterdir())
    rng = random.Random(args.seed)
    out = []

    green = read_manifest(rec / "ccpd2020_green_test.txt")
    rng.shuffle(green)
    for rel, plate in green[: args.green_test]:
        out.append({"subset": "green", "tier": "held_out_both",
                    "path": str(Path("CCPD2020/ccpd_green/test") / rel),
                    "plate": plate, "bbox": bbox_from_name(rel)})

    for sub in SUBSETS_2019:
        rows = read_manifest(rec / f"ccpd2019_{sub}_all.txt")
        if sub == "base":
            before = len(rows)
            rows = [r for r in rows if r[0] not in det_used]
            print(f"[base] removed {before - len(rows)} detector train/val images")
        rng.shuffle(rows)
        for rel, plate in rows[: args.per_subset_2019]:
            out.append({"subset": sub, "tier": "held_out_det_rec_seen",
                        "path": str(Path(f"CCPD2019/CCPD2019/ccpd_{sub}") / rel),
                        "plate": plate, "bbox": bbox_from_name(rel)})

    for i, r in enumerate(out):
        r["id"] = f"{r['subset']}-{i:05d}"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as f:
        for r in out:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"[done] {len(out)} rows -> {args.out} (det_used={len(det_used)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
