#!/usr/bin/env python3
"""CCPD 文件名解析（NRD-3 §7 / P4）。

CCPD 文件名格式（官方 README）：

    序号-面积/倾斜度-bbox-四角点-车牌字符索引-亮度-模糊度.jpg

例如（CCPD2020 绿牌，真实样例）：

    00360785590278-91_265-311&485_406&524-406&524_313&520_311&485_402&489-0_0_3_24_28_24_31_33-117-16.jpg

字段：
  0. 序号（采集序号）
  1. area：tilt_h_horizontal_tilt_h_vertical（ccpd_green 实测 2 个值；2019 为 3 个值）
  2. bbox：x1&y1_x2&y2（源图像像素坐标）
  3. vertices：左上/右上/右下/左下四角点，x&y_x&y_x&y_x&y
  4. indices：字符索引，下划线分隔；第 1 位省份、第 2 位发牌机关字母、其余为序号
     （CCPD2019 蓝牌 7 位，CCPD2020 绿牌 8 位）
  5. brightness、6. blur：整数，越大越亮/越模糊

字符表（对 CCPD2020 ccpd_green 全量 11776 张实证核对，2026-10，见 EVIDENCE）：
  - 省份索引只出现 {0..29}，0（皖）占 96.5%，与「CCPD 采集于安徽」一致；使用社区通用 35 项省份表。
  - 第 2 位索引只出现 {0..16}，0（A，合肥）占 97.5%；对应 24 字母表（A–Z 去 I、O，GA 36）。
  - 序号位索引 0..33；第 3 位出现 {3,5,24,33}＝{D,F,0,9}（新能源 D/F 占 99.8%），
    尾部大量出现 24–33（数字 0–9）→ 序号表为「24 字母在前、数字在后」共 34 项。
    若序号表为「数字在前」，第 3 位将是 {3,5,Q,Z}，与新能源牌 D/F 规则矛盾，故排除。

CCPD2019 到位后需按 §7 重新核对蓝牌（7 位）索引表——本解析按同一表处理，届时跑
全量正则核验（见仓库 P4 报告）。

只用标准库；图片尺寸从 JPEG SOF 段读取（不依赖 PIL）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from pathlib import Path

# 官方/社区通用的省份索引表（35 项；index 0 = 皖）
PROVINCES = (
    "皖沪津渝冀晋蒙辽吉黑苏浙京闽赣鲁豫鄂湘桂琼川贵云藏陕青宁新警学O使港澳"
)

# 发牌机关字母表：A–Z 去 I、O（24 项；GA 36：序号不用 I、O）
PLATE_LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"

# 序号字符表：24 字母在前（index 0–23），数字 0–9 在后（index 24–33）
ADS = PLATE_LETTERS + "0123456789"


@dataclass(frozen=True)
class CcpdName:
    """解析后的 CCPD 文件名。坐标均为源图像像素。"""

    stem: str
    serial: str
    area: tuple[int, ...]
    bbox: tuple[int, int, int, int]  # x1, y1, x2, y2
    vertices: tuple[tuple[int, int], ...]  # 四角点 左上/右上/右下/左下
    indices: tuple[int, ...]
    brightness: int
    blur: int

    @property
    def plate(self) -> str:
        return decode_plate(self.indices)


def parse_ccpd_name(filename: str) -> CcpdName:
    """解析 CCPD 文件名；格式不符抛 ValueError。"""
    stem = filename.rsplit(".", 1)[0]
    parts = stem.split("-")
    if len(parts) != 7:
        raise ValueError(f"expected 7 dash-separated fields (serial-area-bbox-verts-indices-bright-blur), got {len(parts)}: {filename}")
    serial = parts[0]
    area = tuple(int(v) for v in parts[1].split("_"))
    # CCPD2020 ccpd_green 实测为 2 个值（水平/垂直倾斜）；CCPD2019 官方为 3 个
    if len(area) not in (2, 3):
        raise ValueError(f"area field must have 2-3 values: {filename}")
    bx1, by1 = parts[2].split("_")[0].split("&")
    bx2, by2 = parts[2].split("_")[1].split("&")
    verts = []
    for p in parts[3].split("_"):
        vx, vy = p.split("&")
        verts.append((int(vx), int(vy)))
    if len(verts) != 4:
        raise ValueError(f"vertices field must have 4 points: {filename}")
    indices = tuple(int(v) for v in parts[4].split("_"))
    if len(indices) not in (7, 8):
        raise ValueError(f"plate indices must be 7 (blue) or 8 (green): {filename}")
    decode_plate(indices)  # 越界即抛 ValueError
    return CcpdName(
        stem=stem,
        serial=serial,
        area=area,  # type: ignore[arg-type]
        bbox=(int(bx1), int(by1), int(bx2), int(by2)),
        vertices=tuple(verts),  # type: ignore[arg-type]
        indices=indices,
        brightness=int(parts[5]),
        blur=int(parts[6]),
    )


def decode_plate(indices: tuple[int, ...]) -> str:
    """字符索引 → 车牌文本。索引越界抛 ValueError。"""
    if len(indices) < 2:
        raise ValueError("plate needs at least 2 indices")
    if indices[0] >= len(PROVINCES) or indices[1] >= len(PLATE_LETTERS):
        raise ValueError(f"province/alphabet index out of range: {indices[:2]}")
    chars = [PROVINCES[indices[0]], PLATE_LETTERS[indices[1]]]
    for i in indices[2:]:
        if i >= len(ADS):
            raise ValueError(f"ads index out of range: {i}")
        chars.append(ADS[i])
    return "".join(chars)


def jpeg_size(path: Path) -> tuple[int, int]:
    """读 JPEG SOF 段取 (width, height)；只支持 baseline/progressive JPEG（CCPD 全部为 .jpg）。"""
    with open(path, "rb") as f:
        head = f.read(2)
        if head != b"\xff\xd8":
            raise ValueError(f"not a JPEG: {path}")
        while True:
            marker = f.read(2)
            if len(marker) < 2 or marker[0] != 0xFF:
                raise ValueError(f"bad JPEG marker in {path}")
            if marker[1] in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                sof = f.read(7)  # length(2) precision(1) height(2) width(2)
                h = int.from_bytes(sof[3:5], "big")
                w = int.from_bytes(sof[5:7], "big")
                return w, h
            length = int.from_bytes(f.read(2), "big")
            f.seek(length - 2, 1)


def to_coco(src: Path, split: str, category: str = "plate") -> dict:
    """把一个目录的 CCPD 图片转成 COCO dict（detection，1 类）。

    file_name 为相对 src 的路径（如 train/xxxx.jpg），不含 CCPD 原始绝对路径。
    """
    coco = {
        "images": [],
        "annotations": [],
        "categories": [{"id": 1, "name": category, "supercategory": "none"}],
    }
    files = sorted(p for p in src.rglob("*.jpg"))
    ann_id = 1
    bad = 0
    for n, img_path in enumerate(files, 1):
        rel = img_path.relative_to(src).as_posix()
        try:
            info = parse_ccpd_name(img_path.name)
            w, h = jpeg_size(img_path)
        except ValueError as e:
            print(f"SKIP {rel}: {e}", file=sys.stderr)
            bad += 1
            continue
        x1, y1, x2, y2 = info.bbox
        coco["images"].append({"id": n, "file_name": rel, "width": w, "height": h})
        bw, bh = x2 - x1, y2 - y1
        coco["annotations"].append({
            "id": ann_id,
            "image_id": n,
            "category_id": 1,
            "bbox": [x1, y1, bw, bh],
            "area": bw * bh,
            "iscrowd": 0,
            # 四角点与解码文本随附（裁切/识别训练用；COCO 检测器可忽略）
            "segmentation": [[c for pt in info.vertices for c in pt]],
            "plate": info.plate,
        })
        ann_id += 1
    print(f"{split}: {len(coco['images'])} images, {len(coco['annotations'])} annotations, {bad} skipped")
    return coco


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CCPD -> COCO detection JSON (NRD-3 P4)")
    ap.add_argument("--src", required=True, type=Path,
                    help="CCPD 根目录（含 train/val/test 子目录）或单个图片目录")
    ap.add_argument("--out", required=True, type=Path, help="输出目录")
    ap.add_argument("--splits", default="train,val,test", help="逗号分隔；src 为单目录时忽略")
    ap.add_argument("--split", default=None, help="src 为单目录时的输出 split 名（默认 'all'）")
    args = ap.parse_args(argv)

    args.out.mkdir(parents=True, exist_ok=True)
    total = 0
    if (args.src / "train").is_dir() or (args.src / "val").is_dir():
        for split in args.splits.split(","):
            d = args.src / split
            if not d.is_dir():
                print(f"no split dir {d}, skipped", file=sys.stderr)
                continue
            coco = to_coco(d, split)
            total += len(coco["images"])
            (args.out / f"ccpd2020_green_{split}.json").write_text(
                json.dumps(coco, ensure_ascii=False), encoding="utf-8")
    else:
        split = args.split or "all"
        coco = to_coco(args.src, split)
        total = len(coco["images"])
        (args.out / f"ccpd2020_green_{split}.json").write_text(
            json.dumps(coco, ensure_ascii=False), encoding="utf-8")
    print(f"total images: {total}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
