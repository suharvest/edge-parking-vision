#!/usr/bin/env python3
"""PlateRecNet 训练（NRD-3 §6.5 / §8 P6，阶段 A 只做 CN）。

数据：data/processed/ccpd_rec/*.txt 清单（相对路径 \t 车牌文本 \t 四角点），
在线按四角点透视矫正到 96×32（worker 进程内 PIL/OpenCV，不预载全量图片）。

train = ccpd2019 各子集（除 np）+ ccpd2020_green_train；val = ccpd2020_green_val。

内存护栏（GB10 统一内存，显存吃系统 RAM）：启动时 MemAvailable < 30 GB 拒绝启动；
训练中每 --mem-check-interval step 检查本进程 RSS，超 --max-rss-gb 则保存
checkpoint 后主动退出（exit code 3）。

用法（spark，训练工作区 /home/harvest/edge-parking-vision）：

    .venv/bin/python training/train_platerec.py --epochs 60 --batch-size 128
冒烟：--limit 512 --epochs 1 --batch-size 32
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

os.environ.setdefault("PYTORCH_CUDA_ALLOC_CONF", "expandable_segments:True")

import cv2
import numpy as np
import torch
import torch.nn as nn
from torch.utils.data import DataLoader, Dataset

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platerec import PlateRecNet, cn_charset  # noqa: E402

# 清单 → 图片根目录（相对 data_root；相对路径为图片文件名，子目录按清单名推导）
CCPD2020_ROOT = "data/raw/ccpd/CCPD2020/ccpd_green"


def image_root_for(fname: str) -> str:
    """ccpd2019_base_all.txt → .../CCPD2019/ccpd_base；ccpd2020_green_val.txt → .../ccpd_green/val"""
    if fname.startswith("ccpd2020_green_"):
        return f"{CCPD2020_ROOT}/{fname.removeprefix('ccpd2020_green_').removesuffix('.txt')}"
    subset = fname.removeprefix("ccpd2019_").removesuffix("_all.txt")
    return f"data/raw/ccpd/CCPD2019/CCPD2019/ccpd_{subset}"
TRAIN_MANIFESTS = [
    "ccpd2019_base_all.txt",
    "ccpd2019_blur_all.txt",
    "ccpd2019_challenge_all.txt",
    "ccpd2019_db_all.txt",
    "ccpd2019_fn_all.txt",
    "ccpd2019_rotate_all.txt",
    "ccpd2019_tilt_all.txt",
    "ccpd2019_weather_all.txt",
    "ccpd2020_green_train.txt",
]
VAL_MANIFESTS = ["ccpd2020_green_val.txt"]

RNG = np.random.default_rng(0)


def mem_available_gb() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1048576
    raise RuntimeError("MemAvailable not found in /proc/meminfo")


def rss_gb() -> float:
    for line in Path("/proc/self/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1]) / 1048576
    return 0.0


def load_manifest(manifest: Path, root: Path, limit: int | None) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rel, plate, _corners = line.split("\t")
        items.append((str(root / rel), plate))
        if limit is not None and len(items) >= limit:
            break
    return items


def warp_crop(img: np.ndarray, corners: np.ndarray) -> np.ndarray:
    """四角点（RB, LB, LT, RT 顺序，CCPD 约定）透视矫正到 96×32 (W×H)。"""
    dst = np.array([[0, 0], [95, 0], [95, 31], [0, 31]], dtype=np.float32)  # TL,TR,BR,BL
    src = np.array([corners[2], corners[3], corners[0], corners[1]], dtype=np.float32)
    M = cv2.getPerspectiveTransform(src, dst)
    return cv2.warpPerspective(img, M, (96, 32), flags=cv2.INTER_LINEAR)


def augment(img: np.ndarray) -> np.ndarray:
    """§6.5 增强：仿射 ±5°、透视、亮度对比度、运动模糊、JPEG 30–90、高斯噪声（uint8 域）。"""
    h, w = img.shape[:2]
    if RNG.random() < 0.7:  # 随机仿射 ±5° + 轻微缩放平移
        ang = float(RNG.uniform(-5, 5))
        M = cv2.getRotationMatrix2D((w / 2, h / 2), ang, float(RNG.uniform(0.95, 1.05)))
        M[0, 2] += float(RNG.uniform(-2, 2))
        M[1, 2] += float(RNG.uniform(-2, 2))
        img = cv2.warpAffine(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
    if RNG.random() < 0.3:  # 轻微透视抖动
        d = 3.0
        src = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        dst = src + RNG.uniform(-d, d, size=(4, 2)).astype(np.float32)
        M = cv2.getPerspectiveTransform(src, dst)
        img = cv2.warpPerspective(img, M, (w, h), borderMode=cv2.BORDER_REPLICATE)
    if RNG.random() < 0.7:  # 亮度对比度
        img = np.clip(img.astype(np.float32) * RNG.uniform(0.7, 1.3) + RNG.uniform(-25, 25), 0, 255).astype(np.uint8)
    if RNG.random() < 0.3:  # 运动模糊（水平核，奇数）
        k = int(RNG.integers(3, 8))
        kern = np.zeros((1, k), np.float32)
        kern[0, :] = 1.0 / k
        img = cv2.filter2D(img, -1, kern)
    if RNG.random() < 0.5:  # JPEG 质量 30–90
        q = int(RNG.integers(30, 91))
        ok, enc = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), q])
        if ok:
            img = cv2.imdecode(enc, cv2.IMREAD_COLOR)
    if RNG.random() < 0.3:  # 高斯噪声
        img = np.clip(img.astype(np.float32) + RNG.normal(0, 6, img.shape), 0, 255).astype(np.uint8)
    return img


class RecDataset(Dataset):
    def __init__(self, items: list[tuple[str, str]], char2idx: dict[str, int], train: bool) -> None:
        self.items = items
        self.char2idx = char2idx
        self.train = train
        self._corners: list[np.ndarray] = []

    def __len__(self) -> int:  # noqa: D105
        return len(self.items)

    def __getitem__(self, i: int):
        path, plate = self.items[i]
        img = cv2.imread(path)  # BGR，逐张读取，不预载
        if img is None:
            raise FileNotFoundError(path)
        name = Path(path).name
        # 四角点在文件名第 3 段（与标签第 3 列一致，直接从文件名解析可避免再存一份）；
        # 这里用标签文件的四角点列 —— 存在 self.items? 我们存 corners 更直接：
        corners = self._corners[i]
        crop = warp_crop(img, corners)
        if self.train:
            crop = augment(crop)
        x = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB).astype(np.float32) / 255.0
        x = torch.from_numpy(x.transpose(2, 0, 1))
        label = torch.tensor([self.char2idx[c] for c in plate], dtype=torch.long)
        return x, label

    def with_corners(self, corners_list: list[np.ndarray]) -> "RecDataset":
        self._corners = corners_list
        return self


def build_items(manifests: list[str], rec_root: Path, limit: int | None):
    """返回 (paths, plates, corners)。"""
    paths: list[str] = []
    plates: list[str] = []
    corners: list[np.ndarray] = []
    for fname in manifests:
        root = image_root_for(fname)
        for line in (rec_root / fname).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            rel, plate, c = line.split("\t")
            paths.append(str((rec_root.parents[2] / root / rel).resolve()))  # data_root / <data/raw 根> / rel
            plates.append(plate)
            corners.append(np.array([int(v) for v in c.split(",")], dtype=np.float32).reshape(4, 2))
            if limit is not None and len(paths) >= limit:
                return paths, plates, corners
    return paths, plates, corners


def collate(batch):
    xs, labels, lengths = [], [], []
    for x, lab in batch:
        xs.append(x)
        labels.append(lab)
        lengths.append(len(lab))
    return torch.stack(xs), torch.cat(labels), torch.tensor(lengths, dtype=torch.long)


def greedy_decode(logits: torch.Tensor) -> list[str]:
    # logits (N, C, T) → 按 CTC 贪心（T 优先，基座 ctc_layout "ct" 一致）
    pred = logits.argmax(dim=1)  # (N, T)
    return pred.tolist()


def decode_texts(pred_ids: list[list[int]], idx2char: list[str]) -> list[str]:
    out = []
    for seq in pred_ids:
        s, prev = [], -1
        for t in seq:
            if t != prev and t != 0:
                s.append(idx2char[t])
            prev = t
        out.append("".join(s))
    return out


def ctc_loss(ctc: nn.CTCLoss, log_probs: torch.Tensor, labels: torch.Tensor,
             target_lengths: torch.Tensor) -> torch.Tensor:
    """正确的 CTC loss：input_lengths = T（每样本都相同），不是 target 长度。

    log_probs 形状 (T,N,C)。曾把 target_lengths 误传为 input_lengths，导致对齐窗口
    被掐到 target 长度：含相邻重复字符的车牌（如 皖AD18887）不可行（zero_infinity
    静默记 0），无重复样本也失去对齐自由度。
    """
    T, N = log_probs.size(0), log_probs.size(1)
    tl = target_lengths.to(log_probs.device)
    if int(tl.max()) > T:
        raise ValueError(f"CTC infeasible: target length {int(tl.max())} > T={T}")
    # 断言：每样本 target 长度 + 相邻重复字符数 <= T，否则 CTC 对齐不可行，直接报错
    need = 0
    for lab in torch.split(labels.cpu(), target_lengths.cpu().tolist()):
        L = lab.tolist()
        need = max(need, len(L) + sum(1 for a, b in zip(L, L[1:]) if a == b))
    if need > T:
        raise ValueError(f"CTC infeasible: max(target_len + adjacent_repeats) = {need} > T={T}")
    input_lengths = torch.full((N,), T, dtype=torch.long, device=log_probs.device)
    return ctc(log_probs, labels.to(log_probs.device), input_lengths, tl)


@torch.no_grad()
def evaluate(model, loader, device, idx2char) -> tuple[float, float]:
    model.eval()
    correct = total = 0
    loss_sum, n = 0.0, 0
    ctc = nn.CTCLoss(blank=0, zero_infinity=True)
    for x, labels, lengths in loader:
        x = x.to(device, non_blocking=True)
        logits = model(x).squeeze(2)  # (N,C,T)（导出图为 (N,C,1,T)，训练侧压缩 H=1）
        log_probs = logits.log_softmax(1).permute(2, 0, 1)  # (T,N,C)
        loss = ctc_loss(ctc, log_probs, labels, lengths)
        loss_sum += float(loss.detach()) * len(x)
        n += len(x)
        texts = decode_texts(greedy_decode(logits), idx2char)
        gts = ["".join(idx2char[int(t)] for t in lab) for lab in torch.split(labels, list(lengths))]
        for p, g in zip(texts, gts):
            total += 1
            correct += int(p == g)
    return correct / max(total, 1), loss_sum / max(n, 1)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="PlateRecNet CN training (NRD-3 P6)")
    ap.add_argument("--data-root", type=Path, default=Path("/home/harvest/edge-parking-vision"))
    ap.add_argument("--epochs", type=int, default=60)
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--lr", type=float, default=1e-3)
    ap.add_argument("--num-workers", type=int, default=4)
    ap.add_argument("--prefetch-factor", type=int, default=2)
    ap.add_argument("--limit", type=int, default=None, help="训练样本上限（冒烟用）")
    ap.add_argument("--val-limit", type=int, default=None)
    ap.add_argument("--out", type=Path, default=Path("training/outputs/platerec_cn"))
    ap.add_argument("--log-file", type=Path, default=None)
    ap.add_argument("--resume", type=Path, default=None)
    ap.add_argument("--max-rss-gb", type=float, default=60.0)
    ap.add_argument("--min-mem-available-gb", type=float, default=30.0)
    ap.add_argument("--mem-check-interval", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)

    avail = mem_available_gb()
    print(f"[mem] MemAvailable at start: {avail:.1f} GB", flush=True)
    if avail < args.min_mem_available_gb:
        print(f"[mem] REFUSING to start: MemAvailable {avail:.1f} GB < {args.min_mem_available_gb} GB")
        return 3

    args.out.mkdir(parents=True, exist_ok=True)
    log_path = args.log_file or (args.out / "train.log")
    log_f = open(log_path, "a", encoding="utf-8")

    def log(msg: str) -> None:
        line = f"{time.strftime('%F %T')} {msg}"
        print(line, flush=True)
        log_f.write(line + "\n")
        log_f.flush()

    torch.manual_seed(args.seed)
    random.seed(args.seed)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    log(f"[env] device={device} torch={torch.__version__}")

    charset = cn_charset()
    idx2char = charset
    char2idx = {c: i for i, c in enumerate(charset)}
    log(f"[charset] C={len(charset)} (index 0 = blank)")

    rec_root = args.data_root / "data" / "processed" / "ccpd_rec"
    tr = build_items(TRAIN_MANIFESTS, rec_root, args.limit)
    va = build_items(VAL_MANIFESTS, rec_root, args.val_limit)
    log(f"[data] train={len(tr[0])} val={len(va[0])}")
    train_ds = RecDataset([], char2idx, train=True).with_corners(tr[2])
    train_ds.items = list(zip(tr[0], tr[1]))
    val_ds = RecDataset([], char2idx, train=False).with_corners(va[2])
    val_ds.items = list(zip(va[0], va[1]))

    nw = min(args.num_workers, 8)
    dl_kwargs = dict(num_workers=nw, batch_size=args.batch_size, collate_fn=collate,
                     pin_memory=True, persistent_workers=False)
    if nw > 0:
        dl_kwargs["prefetch_factor"] = args.prefetch_factor
    train_dl = DataLoader(train_ds, shuffle=True, drop_last=True, **dl_kwargs)
    val_dl = DataLoader(val_ds, shuffle=False, **dl_kwargs)

    model = PlateRecNet(len(charset)).to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    steps_per_epoch = max(len(train_dl), 1)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs * steps_per_epoch)
    ctc = nn.CTCLoss(blank=0, zero_infinity=True)

    start_epoch = 0
    if args.resume and Path(args.resume).exists():
        ck = torch.load(args.resume, map_location=device, weights_only=False)
        model.load_state_dict(ck["model"])
        opt.load_state_dict(ck["optimizer"])
        sched.load_state_dict(ck["scheduler"])
        start_epoch = ck["epoch"] + 1
        best_acc = float(ck.get("best_acc", -1.0))
        main._best = best_acc
        log(f"[resume] from {args.resume} at epoch {start_epoch} (best_acc={best_acc:.4f})")

    log(f"[train] epochs={args.epochs} batch={args.batch_size} steps/epoch={steps_per_epoch} workers={nw}")

    for epoch in range(start_epoch, args.epochs):
        t0 = time.time()
        model.train()
        run_loss = 0.0
        for step, (x, labels, lengths) in enumerate(train_dl):
            x = x.to(device, non_blocking=True)
            logits = model(x).squeeze(2)
            log_probs = logits.log_softmax(1).permute(2, 0, 1)  # (T,N,C) for CTCLoss
            loss = ctc_loss(ctc, log_probs, labels, lengths)
            opt.zero_grad(set_to_none=True)
            loss.backward()
            opt.step()
            sched.step()
            run_loss += float(loss.detach())
            if (step + 1) % args.mem_check_interval == 0:
                r = rss_gb()
                if r > args.max_rss_gb:
                    ckpt = args.out / "last_ckpt.pth"
                    torch.save({"model": model.state_dict(), "optimizer": opt.state_dict(),
                                "scheduler": sched.state_dict(), "epoch": epoch,
                                "best_acc": getattr(main, "_best", -1.0)}, ckpt)
                    log(f"[mem-guard] RSS {r:.1f} GB > max {args.max_rss_gb} GB; "
                        f"checkpoint saved to {ckpt}; exiting (reason=rss-limit)")
                    log_f.close()
                    return 3
        val_acc, val_loss = evaluate(model, val_dl, device, idx2char)
        epoch_s = time.time() - t0
        peak_rss = rss_gb()
        cuda_peak = torch.cuda.max_memory_allocated() / 1048576 if device.type == "cuda" else 0.0
        # 先更新 best，再写 last_ckpt，确保 checkpoint 里的 best_acc 已包含本 epoch。
        if val_acc > getattr(main, "_best", -1.0):
            main._best = val_acc
            torch.save(model.state_dict(), args.out / "best.pth")
            torch.save({"model": model.state_dict(), "epoch": epoch, "val_acc": val_acc},
                       args.out / "best_ckpt.pth")
        torch.save({"model": model.state_dict(), "optimizer": opt.state_dict(),
                    "scheduler": sched.state_dict(), "epoch": epoch,
                    "val_acc": val_acc, "best_acc": getattr(main, "_best", -1.0)},
                   args.out / "last_ckpt.pth")
        log(f"[epoch {epoch}] loss={run_loss / steps_per_epoch:.4f} val_loss={val_loss:.4f} "
            f"val_acc={val_acc:.4f} epoch_time={epoch_s:.1f}s peak_rss={peak_rss:.2f}GB "
            f"cuda_peak_alloc={cuda_peak:.0f}MB mem_available={mem_available_gb():.1f}GB")

    log("[done] training finished")
    log_f.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
