#!/usr/bin/env python3
"""组装 YOLOX-Tiny 车牌检测训练集（NRD-3 §7 / P5，GPU x86 工作站上运行）。

按 §7「检测：CCPD2019 base 子集（随机 5 万张）+ CCPD2020 全部；验证 5%」：
  train2017 = ccpd2019_base 随机 50000（seed 42）+ ccpd2020_green train 全部
  val2017   = ccpd2019_base 与 train 不相交的 2500（= 50000 的 5%）
              + ccpd2020_green val 全部
本期不做海外制式（§0.1），不含合成 DE 整图 2 万；恢复时在 build 输入里加一路即可。

输入为 P4 转换产物（COCO JSON，file_name 相对各数据子集根）。
图片用硬链接（同文件系统零拷贝）放进 <out>/train2017/{b,g}/… 与
<out>/val2017/{b,g}/…（b = ccpd2019_base，g = ccpd2020_green），标注写
<out>/annotations/instances_{train,val}2017.json。

只用标准库。用法：

    python3 training/build_det_dataset.py \
        --base-json data/processed/ccpd_coco/ccpd2019_base_all.json \
        --base-img  data/raw/ccpd/CCPD2019/CCPD2019/ccpd_base \
        --green-train-json data/processed/ccpd_coco/ccpd2020_green_train.json \
        --green-val-json   data/processed/ccpd_coco/ccpd2020_green_val.json \
        --green-img  data/raw/ccpd/CCPD2020/ccpd_green \
        --out data/coco/plate_det
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

TRAIN_N = 50_000
VAL_N = 2_500  # 50000 的 5%
SEED = 42


def link(src: Path, dst: Path) -> None:
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        dst.unlink()
    import os
    try:
        os.link(src, dst)  # 硬链接（Path.link_to 在 Python 3.12 已移除）
    except OSError:  # 跨设备兜底
        import shutil
        shutil.copy2(src, dst)


def emit(entries, img_root: Path, prefix: str, img_dir: Path,
         id_offset: int, category_id_seen: set[int]) -> tuple[list, list, int]:
    """entries: [(image_dict, [ann_dict])]；返回 COCO images/annotations 片段。"""
    images, anns = [], []
    img_id = id_offset
    ann_id = 1
    for img, img_anns in entries:
        rel = f"{prefix}/{img['file_name']}"
        link(img_root / img["file_name"], img_dir / rel)
        images.append({"id": img_id, "file_name": rel,
                       "width": img["width"], "height": img["height"]})
        for a in img_anns:
            assert a["category_id"] == 1, f"unexpected category {a['category_id']}"
            category_id_seen.add(a["category_id"])
            anns.append({"id": ann_id, "image_id": img_id, "category_id": 1,
                         "bbox": a["bbox"], "area": a["area"], "iscrowd": 0})
            ann_id += 1
        img_id += 1
    return images, anns, ann_id


def index_by_image(coco: dict):
    anns_by_img: dict[int, list] = {}
    for a in coco["annotations"]:
        anns_by_img.setdefault(a["image_id"], []).append(a)
    return [(img, anns_by_img.get(img["id"], [])) for img in coco["images"]]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--base-json", required=True, type=Path)
    ap.add_argument("--base-img", required=True, type=Path)
    ap.add_argument("--green-train-json", required=True, type=Path)
    ap.add_argument("--green-val-json", required=True, type=Path)
    ap.add_argument("--green-img", required=True, type=Path,
                    help="ccpd_green 根（其下为 train/val/test；green JSON 的 file_name 相对各 split 目录）")
    ap.add_argument("--out", required=True, type=Path)
    args = ap.parse_args()

    base = json.loads(args.base_json.read_text(encoding="utf-8"))
    gtrain = json.loads(args.green_train_json.read_text(encoding="utf-8"))
    gval = json.loads(args.green_val_json.read_text(encoding="utf-8"))

    base_entries = index_by_image(base)
    rng = random.Random(SEED)
    rng.shuffle(base_entries)
    base_train = base_entries[:TRAIN_N]
    base_val = base_entries[TRAIN_N:TRAIN_N + VAL_N]
    assert len(base_train) == TRAIN_N and len(base_val) == VAL_N

    cats = [{"id": 1, "name": "plate", "supercategory": "none"}]
    ann_dir = args.out / "annotations"
    ann_dir.mkdir(parents=True, exist_ok=True)
    seen: set[int] = set()

    for name, parts in {
        "train2017": [(base_train, args.base_img, "b"),
                      (index_by_image(gtrain), args.green_img / "train", "g/train")],
        "val2017": [(base_val, args.base_img, "b"),
                    (index_by_image(gval), args.green_img / "val", "g/val")],
    }.items():
        images, anns = [], []
        img_dir = args.out / name
        next_img_id = 1
        for entries, root, prefix in parts:
            imgs, ans, _ = emit(entries, root, prefix, img_dir, next_img_id, seen)
            for a in ans:
                a["id"] = len(anns) + 1
                anns.append(a)
            images.extend(imgs)
            next_img_id += len(imgs)
        (ann_dir / f"instances_{name}.json").write_text(json.dumps(
            {"images": images, "annotations": anns, "categories": cats},
            ensure_ascii=False), encoding="utf-8")
        print(f"{name}: {len(images)} images, {len(anns)} annotations")
    print(f"done -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
