#!/usr/bin/env python3
"""PlateRecNet CN 测试集整牌准确率评测（NRD-3 P6-B 收尾）。

数据：ccpd2020_green_test 清单（N=5006），逐张裁切 → warp 到 96×32 → 模型 →
greedy CTC 解码 → 与标签整串比对。warp_crop / decode 逻辑直接复用
training/train_platerec.py（不另写一套解码）。

用法：
    .venv/bin/python training/eval_platerec.py \
        --ckpt /home/harvest/edge-parking-vision/training/outputs/platerec_cn_full/best.pth \
        --data-root /home/harvest/edge-parking-vision
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

import numpy as np
import torch
from torch.utils.data import DataLoader

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platerec import PlateRecNet, cn_charset  # noqa: E402
from train_platerec import (RecDataset, build_items, collate, decode_texts,  # noqa: E402
                            greedy_decode)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ckpt", type=Path, required=True)
    ap.add_argument("--data-root", type=Path, default=Path("/home/harvest/edge-parking-vision"))
    ap.add_argument("--manifest", default="ccpd2020_green_test.txt")
    ap.add_argument("--batch-size", type=int, default=256)
    ap.add_argument("--num-workers", type=int, default=4)
    args = ap.parse_args(argv)

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    charset = cn_charset()
    idx2char = charset
    char2idx = {c: i for i, c in enumerate(charset)}

    rec_root = args.data_root / "data" / "processed" / "ccpd_rec"
    paths, plates, corners = build_items([args.manifest], rec_root, None)
    n = len(paths)
    print(f"[data] manifest={args.manifest} N={n}")

    model = PlateRecNet(len(charset)).to(device)
    sd = torch.load(args.ckpt, map_location=device, weights_only=True)
    if isinstance(sd, dict) and "model" in sd:
        sd = sd["model"]
    model.load_state_dict(sd)
    model.eval()

    ds = RecDataset(list(zip(paths, plates)), char2idx, train=False).with_corners(corners)
    dl = DataLoader(ds, batch_size=args.batch_size, shuffle=False,
                    collate_fn=collate, num_workers=args.num_workers, pin_memory=True)

    correct = 0
    len_ok_char_wrong = 0
    len_wrong = 0
    per_frame_ms: list[float] = []  # 每个 batch 的均摊单帧推理耗时
    done = 0
    with torch.no_grad():
        for x, labels, lengths in dl:
            x = x.to(device, non_blocking=True)
            if device.type == "cuda":
                torch.cuda.synchronize()
            t0 = time.perf_counter()
            logits = model(x).squeeze(2)
            if device.type == "cuda":
                torch.cuda.synchronize()
            per_frame_ms.append((time.perf_counter() - t0) * 1000 / len(x))
            texts = decode_texts(greedy_decode(logits), idx2char)
            gts = ["".join(idx2char[int(t)] for t in lab)
                   for lab in torch.split(labels, list(lengths))]
            for p, g in zip(texts, gts):
                if p == g:
                    correct += 1
                elif len(p) == len(g):
                    len_ok_char_wrong += 1
                else:
                    len_wrong += 1
            done += len(x)
            if done % (args.batch_size * 5) < args.batch_size:
                print(f"  [progress] {done}/{n} acc={correct/done:.4f}", flush=True)

    p50 = float(np.percentile(per_frame_ms, 50))
    print(f"\n[result] N={n}")
    print(f"[result] 整牌准确率 = {correct}/{n} = {correct/n:.4f}")
    print(f"[result] 错误分类: 长度对但字符错 = {len_ok_char_wrong}, 长度也不对 = {len_wrong}")
    print(f"[result] 单帧批量推理耗时 p50 = {p50:.3f} ms/帧 (batch_size={args.batch_size}, "
          f"{len(per_frame_ms)} batches)")
    print(f"[result] model={args.ckpt}")
    print(f"[result] data={rec_root / args.manifest}")
    print(f"[result] device={device}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
