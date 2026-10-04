#!/usr/bin/env python3
"""生成 PlateRecNet CN 一致性夹具（NRD-3 §8 P6 → Q2）：evaluation/data/parity/plate_rec_cn/。

来源：ccpd2020_green_val 清单（data/processed/ccpd_rec/ccpd2020_green_val.txt）均匀抽
200 条，按四角点透视矫正到 96×32（与训练同一 warp 逻辑，无增强），另存
crops.jsonl（{index, image, plate}），供基座 ctc_greedy 一致性测试用。

用法（spark 工作区）：
    .venv/bin/python training/make_platerec_parity.py \
        --data-root /home/harvest/edge-parking-vision \
        --out /home/harvest/project/edge-parking-vision/evaluation/data/parity/plate_rec_cn
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from train_platerec import VAL_MANIFESTS, image_root_for, warp_crop  # noqa: E402


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", type=Path, default=Path("/home/harvest/edge-parking-vision"))
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--count", type=int, default=200)
    args = ap.parse_args(argv)

    rec_root = args.data_root / "data" / "processed" / "ccpd_rec"
    manifest = VAL_MANIFESTS[0]
    root_rel = None  # 按清单名推导（train_platerec.image_root_for）
    lines = [l for l in (rec_root / manifest).read_text(encoding="utf-8").splitlines() if l.strip()]
    idxs = np.linspace(0, len(lines) - 1, args.count).astype(int)
    img_root = args.data_root / image_root_for(manifest)

    crops_dir = args.out / "crops"
    crops_dir.mkdir(parents=True, exist_ok=True)
    meta = []
    for n, i in enumerate(idxs):
        rel, plate, c = lines[i].split("\t")
        img = cv2.imread(str(img_root / rel))
        corners = np.array([int(v) for v in c.split(",")], dtype=np.float32).reshape(4, 2)
        crop = warp_crop(img, corners)
        name = f"{n:04d}.png"
        cv2.imwrite(str(crops_dir / name), crop)
        meta.append({"index": n, "image": f"crops/{name}", "source_image": rel, "plate": plate})
    (args.out / "crops.jsonl").write_text(
        "\n".join(json.dumps(m, ensure_ascii=False) for m in meta) + "\n", encoding="utf-8")
    (args.out / "README.md").write_text(
        "# plate_rec_cn parity 夹具（NRD-3 §8 P6 / Q2）\n\n"
        f"来源：ccpd2020_green_val（data/processed/ccpd_rec/{manifest}，均匀抽 {args.count} 张），\n"
        "由 training/make_platerec_parity.py 按标签四角点透视矫正到 96×32 生成（无增强）。\n"
        "标签在 crops.jsonl（plate 字段）；图片仅用于本仓库测试，CCPD 原图不入库不分发（§7）。\n",
        encoding="utf-8")
    print(f"wrote {len(meta)} crops to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
