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
import hashlib
import json
import math
import os
import random
import re
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
    def __init__(self, items: list[tuple[str, str]], char2idx: dict[str, int], train: bool,
                 crop_mode: str = "quad", axis_boxes: list[list[float]] | None = None,
                 axis_expand: float = 0.08) -> None:
        self.items = items
        self.char2idx = char2idx
        self.train = train
        if crop_mode not in {"quad", "axis", "axisctx"}:
            raise ValueError(f"unknown crop mode: {crop_mode}")
        self.crop_mode = crop_mode
        self.axis_boxes = axis_boxes or []
        self.axis_expand = axis_expand
        self._corners: list[np.ndarray] = []
        self.ctx_rows: list[dict] = []
        self.ctx_dir: Path | None = None
        self.jitter = (0.0, 0.0, 0.0)  # (prob, max center shift / box size, max log-scale)

    def __len__(self) -> int:  # noqa: D105
        return len(self.items)

    def __getitem__(self, i: int):
        if self.crop_mode == "axisctx":
            return self._getitem_ctx(i)
        path, plate = self.items[i]
        img = cv2.imread(path)  # BGR，逐张读取，不预载
        if img is None:
            raise FileNotFoundError(path)
        name = Path(path).name
        # 四角点在文件名第 3 段（与标签第 3 列一致，直接从文件名解析可避免再存一份）；
        # 这里用标签文件的四角点列 —— 存在 self.items? 我们存 corners 更直接：
        if self.crop_mode == "quad":
            corners = self._corners[i]
            crop = warp_crop(img, corners)
            if self.train:
                crop = augment(crop)  # preserve the historical BGR augmentation order
            crop_rgb = cv2.cvtColor(crop, cv2.COLOR_BGR2RGB)
        else:
            if i >= len(self.axis_boxes):
                raise ValueError(f"axis crop metadata missing row {i}")
            crop_rgb = axis_crop_native_contract(img, self.axis_boxes[i], self.axis_expand)
            if self.train:
                crop_rgb = augment(np.clip(crop_rgb, 0, 255).astype(np.uint8)).astype(np.float32)
        x = crop_rgb.astype(np.float32) / 255.0
        x = torch.from_numpy(x.transpose(2, 0, 1))
        label = torch.tensor([self.char2idx[c] for c in plate], dtype=torch.long)
        return x, label

    def _getitem_ctx(self, i: int):
        r = self.ctx_rows[i]
        bgr = cv2.imread(str(self.ctx_dir / r["png"]), cv2.IMREAD_COLOR)
        if bgr is None:
            raise FileNotFoundError(r["png"])
        rgb = bgr[:, :, ::-1]
        box = [float(v) for v in r["box"]]
        if self.train and self.jitter[0] > 0 and RNG.random() < self.jitter[0]:
            box = jitter_box(box, self.jitter[1], self.jitter[2])
        crop_rgb = axisctx_crop(rgb, box, r["region_xy"], r["frame_wh"], self.axis_expand)
        if self.train:
            crop_rgb = augment(np.clip(np.floor(crop_rgb + 0.5), 0, 255).astype(np.uint8))
        x = torch.from_numpy((crop_rgb.astype(np.float32) / 255.0).transpose(2, 0, 1).copy())
        label = torch.tensor([self.char2idx[c] for c in r["plate"]], dtype=torch.long)
        return x, label

    def with_corners(self, corners_list: list[np.ndarray]) -> "RecDataset":
        self._corners = corners_list
        return self


def jitter_box(box: list[float], shift: float, log_scale: float) -> list[float]:
    """Perturb a region-space box: centre shift U(-shift, shift)·size, size × exp(U(-ls, ls))."""
    cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
    bw, bh = box[2] - box[0], box[3] - box[1]
    cx += float(RNG.uniform(-shift, shift)) * bw
    cy += float(RNG.uniform(-shift, shift)) * bh
    bw *= math.exp(float(RNG.uniform(-log_scale, log_scale)))
    bh *= math.exp(float(RNG.uniform(-log_scale, log_scale)))
    return [cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2]


def axisctx_crop(region_rgb: np.ndarray, box: list[float], region_xy, frame_wh,
                 expand: float) -> np.ndarray:
    """Runtime stage-2 crop (plate_e2e_eval.expand_box + crop_rec) computed on a saved
    context region. Box is in region pixels; expansion/clamping use the full frame."""
    fw, fh = float(frame_wh[0]), float(frame_wh[1])
    ox, oy = float(region_xy[0]), float(region_xy[1])
    b = [box[0] + ox, box[1] + oy, box[2] + ox, box[3] + oy]
    b = [max(0.0, b[0]), max(0.0, b[1]), min(fw, b[2]), min(fh, b[3])]
    cx, cy = (b[0] + b[2]) / 2 / fw, (b[1] + b[3]) / 2 / fh
    bw, bh = (b[2] - b[0]) / fw, (b[3] - b[1]) / fh
    x0, y0 = max(0.0, cx - bw * (0.5 + expand)), max(0.0, cy - bh * (0.5 + expand))
    x1, y1 = min(1.0, cx + bw * (0.5 + expand)), min(1.0, cy + bh * (0.5 + expand))
    x0, x1 = x0 * fw - ox, x1 * fw - ox
    y0, y1 = y0 * fh - oy, y1 * fh - oy
    rh, rw = region_rgb.shape[:2]
    ys = _stage2_axis(32, y0, (y1 - y0) / 32, rh)
    xs = _stage2_axis(96, x0, (x1 - x0) / 96, rw)
    a = region_rgb.astype(np.float32)
    rows = a[ys[0]] * (1 - ys[2])[:, None, None] + a[ys[1]] * ys[2][:, None, None]
    return rows[:, xs[0]] * (1 - xs[2])[None, :, None] + rows[:, xs[1]] * xs[2][None, :, None]


def _stage2_axis(n_out: int, start: float, step: float, n_src: int):
    """cpu_stage2 sampling: half-pixel centres, clamp coordinate, then floor."""
    s = start + (np.arange(n_out, dtype=np.float32) + 0.5) * step - 0.5
    s = np.clip(s, 0, n_src - 1)
    i0 = np.floor(s).astype(np.int64)
    f = (s - i0).astype(np.float32)
    return i0, np.minimum(i0 + 1, n_src - 1), f


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


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def axis_crop_native_contract(img: np.ndarray, bbox: list[float], expand: float) -> np.ndarray:
    """Make the RGB crop used by native stage2's normalized axis rectangle."""
    if img is None or img.ndim != 3 or img.shape[2] != 3:
        raise ValueError("axis crop source must be a decoded 3-channel image")
    if len(bbox) != 4 or not all(math.isfinite(float(x)) for x in bbox):
        raise ValueError("axis crop bbox must contain four finite numbers")
    x0, y0, x1, y1 = map(float, bbox)
    h, w = img.shape[:2]
    if not (0 <= x0 < x1 <= w and 0 <= y0 < y1 <= h):
        raise ValueError("axis crop bbox is outside source image")
    if not (math.isfinite(expand) and 0 <= expand <= 0.5):
        raise ValueError("axis crop expand is invalid")
    cx, cy = (x0 + x1) * 0.5, (y0 + y1) * 0.5
    bw, bh = x1 - x0, y1 - y0
    x0 = max(0.0, cx - bw * (0.5 + expand)); y0 = max(0.0, cy - bh * (0.5 + expand))
    x1 = min(float(w), cx + bw * (0.5 + expand)); y1 = min(float(h), cy + bh * (0.5 + expand))
    if not (x0 < x1 and y0 < y1):
        raise ValueError("axis crop is empty after expand/clamp")
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    out = np.empty((32, 96, 3), dtype=np.float32)
    for oy in range(32):
        sy = y0 + (oy + 0.5) * (y1 - y0) / 32.0 - 0.5
        ay0 = math.floor(sy); fy = sy - ay0; ay = max(0, min(h - 1, ay0)); by = max(0, min(h - 1, ay0 + 1))
        for ox in range(96):
            sx = x0 + (ox + 0.5) * (x1 - x0) / 96.0 - 0.5
            ax0 = math.floor(sx); fx = sx - ax0; ax = max(0, min(w - 1, ax0)); bx = max(0, min(w - 1, ax0 + 1))
            top = rgb[ay, ax] * (1 - fx) + rgb[ay, bx] * fx
            bot = rgb[by, ax] * (1 - fx) + rgb[by, bx] * fx
            out[oy, ox] = top * (1 - fy) + bot * fy
    return np.clip(out, 0, 255)


def build_axis_items(manifest_path: Path, split: str, data_root: Path,
                     expected_expand: float | None = None, limit: int | None = None):
    """Load strict external detector crop records; never fall back to GT crops."""
    raw = json.loads(manifest_path.read_text(encoding="utf-8"))
    if raw.get("schema") != "nrd3.axis-crop/1" or not isinstance(raw.get("rows"), list):
        raise ValueError("axis manifest schema must be nrd3.axis-crop/1 with rows")
    model_sha = raw.get("detector_model_sha256")
    if not isinstance(model_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", model_sha):
        raise ValueError("axis manifest detector_model_sha256 must be a SHA256")
    if raw.get("bbox_space") != "source_pixels":
        raise ValueError("axis manifest bbox_space must be source_pixels")
    model_path_value = raw.get("detector_model_path")
    if not isinstance(model_path_value, str) or not model_path_value:
        raise ValueError("axis manifest detector_model_path is required")
    model_path = Path(model_path_value)
    model_path = model_path if model_path.is_absolute() else (data_root / model_path).resolve()
    if not model_path.is_file() or _sha256_file(model_path).lower() != model_sha.lower():
        raise ValueError(f"axis manifest detector model path/SHA mismatch: {model_path}")
    expand = raw.get("expand", 0.08)
    if (isinstance(expand, bool) or not isinstance(expand, (int, float)) or
            not math.isfinite(float(expand)) or not 0 <= float(expand) <= 0.5):
        raise ValueError("axis manifest expand is invalid")
    if expected_expand is not None and (isinstance(expected_expand, bool) or
            not math.isfinite(float(expected_expand)) or abs(float(expand) - float(expected_expand)) > 1e-9):
        raise ValueError("axis manifest expand differs from requested value")
    if any(not isinstance(r, dict) for r in raw["rows"]):
        raise ValueError("axis manifest rows must be objects")
    rows = [r for r in raw["rows"] if r.get("split") == split]
    if limit is not None:
        rows = rows[:limit]
    if not rows:
        raise ValueError(f"axis manifest has no rows for split={split!r}")
    paths: list[str] = []; labels: list[str] = []; boxes: list[list[float]] = []; seen_indices: set[int] = set()
    for n, r in enumerate(rows):
        index = r.get("index")
        if isinstance(index, bool) or not isinstance(index, int) or index < 0 or index in seen_indices:
            raise ValueError(f"axis row {n} index is missing, invalid, or duplicated")
        seen_indices.add(index)
        if r.get("selection") != "highest_score":
            raise ValueError(f"axis row {n} selection must be highest_score")
        p = Path(r.get("source_path", "")); p = p if p.is_absolute() else (data_root / p).resolve()
        expected_sha = r.get("source_sha256")
        if not p.is_file() or not isinstance(expected_sha, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha):
            raise ValueError(f"axis row {n} source path/SHA is invalid")
        if _sha256_file(p).lower() != expected_sha.lower():
            raise ValueError(f"axis row {n} source SHA mismatch: {p}")
        img = cv2.imread(str(p), cv2.IMREAD_COLOR)
        if img is None or img.ndim != 3 or img.shape[1] <= 0 or img.shape[0] <= 0:
            raise ValueError(f"axis row {n} source image cannot be decoded")
        dw, dh = r.get("source_width"), r.get("source_height")
        if (isinstance(dw, bool) or not isinstance(dw, int) or dw <= 0 or
                isinstance(dh, bool) or not isinstance(dh, int) or dh <= 0 or
                dw != img.shape[1] or dh != img.shape[0]):
            raise ValueError(f"axis row {n} source geometry is missing or does not match decoded image")
        box = r.get("det_bbox_xyxy")
        if not isinstance(box, list) or len(box) != 4 or not all(isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(float(x)) for x in box):
            raise ValueError(f"axis row {n} bbox is invalid")
        x0, y0, x1, y1 = map(float, box)
        if not (0 <= x0 < x1 <= img.shape[1] and 0 <= y0 < y1 <= img.shape[0]):
            raise ValueError(f"axis row {n} bbox is outside source geometry")
        score = r.get("det_score")
        if not isinstance(score, (int, float)) or isinstance(score, bool) or not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
            raise ValueError(f"axis row {n} detector score is invalid")
        label = r.get("label")
        if not isinstance(label, str) or not label:
            raise ValueError(f"axis row {n} label is invalid")
        paths.append(str(p)); labels.append(label); boxes.append([x0, y0, x1, y1])
    return paths, labels, boxes, float(expand), model_sha


def _charset_sha256(charset: list[str]) -> str:
    payload = json.dumps(charset, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _checkpoint_meta(crop_mode: str, axis_manifest_sha: str | None,
                     detector_sha: str | None, axis_expand: float | None,
                     charset_sha: str) -> dict:
    return {"crop_mode": crop_mode, "axis_manifest_sha256": axis_manifest_sha,
            "detector_model_sha256": detector_sha, "axis_expand": axis_expand,
            "charset_sha256": charset_sha}


def _validate_resume_meta(ck: dict, current: dict) -> None:
    saved = ck.get("crop_meta")
    if saved is None:
        if current["crop_mode"] == "axis":
            raise ValueError("axis resume requires checkpoint crop_meta; legacy checkpoint is quad-only")
        return
    if saved != current:
        raise ValueError(f"checkpoint crop_meta mismatch: saved={saved!r} current={current!r}")


def _load_init_weights(model: nn.Module, path: Path) -> None:
    raw = torch.load(path, map_location="cpu", weights_only=False)
    state = raw.get("model") if isinstance(raw, dict) and isinstance(raw.get("model"), dict) else raw
    if not isinstance(state, dict):
        raise ValueError("--init-weights must contain a model state dict")
    try:
        model.load_state_dict(state, strict=True)
    except RuntimeError as exc:
        raise ValueError(f"--init-weights incompatible with current model: {exc}") from exc


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
    ap.add_argument("--init-weights", type=Path, default=None,
                    help="load model weights only; starts fresh optimizer/scheduler")
    ap.add_argument("--crop-mode", choices=("quad", "axis", "axisctx"), default="quad")
    ap.add_argument("--axisctx-dir", type=Path, default=None,
                    help="store from gen_platerec_axis_crops.py (rows.jsonl + meta.json + PNGs)")
    ap.add_argument("--box-jitter-prob", type=float, default=0.8)
    ap.add_argument("--box-jitter-shift", type=float, default=0.05)
    ap.add_argument("--box-jitter-log-scale", type=float, default=0.12)
    ap.add_argument("--axis-crop-manifest", type=Path, default=None)
    ap.add_argument("--axis-expand", type=float, default=None)
    ap.add_argument("--max-rss-gb", type=float, default=60.0)
    ap.add_argument("--min-mem-available-gb", type=float, default=30.0)
    ap.add_argument("--mem-check-interval", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args(argv)
    if args.resume is not None and args.init_weights is not None:
        raise SystemExit("--resume and --init-weights are mutually exclusive")

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
    axis_manifest_sha = None
    detector_model_sha = None
    axis_expand = None
    if args.crop_mode == "axisctx":
        if args.axisctx_dir is None:
            raise SystemExit("--axisctx-dir is required with --crop-mode axisctx")
        ctx_meta = json.loads((args.axisctx_dir / "meta.json").read_text(encoding="utf-8"))
        all_rows = [json.loads(l) for l in (args.axisctx_dir / "rows.jsonl").read_text(
            encoding="utf-8").splitlines() if l.strip()]
        rows_tr = [r for r in all_rows if r["split"] == "train"][: args.limit]
        rows_va = [r for r in all_rows if r["split"] == "val"][: args.val_limit]
        axis_expand = 0.08 if args.axis_expand is None else float(args.axis_expand)
        axis_manifest_sha = _sha256_file(args.axisctx_dir / "rows.jsonl")
        detector_model_sha = ctx_meta["detector_model_sha256"]
        train_ds = RecDataset([], char2idx, train=True, crop_mode="axisctx", axis_expand=axis_expand)
        val_ds = RecDataset([], char2idx, train=False, crop_mode="axisctx", axis_expand=axis_expand)
        for ds, rows in ((train_ds, rows_tr), (val_ds, rows_va)):
            ds.ctx_rows, ds.ctx_dir = rows, args.axisctx_dir
            ds.items = [(r["png"], r["plate"]) for r in rows]
        train_ds.jitter = (args.box_jitter_prob, args.box_jitter_shift, args.box_jitter_log_scale)
        tr, va = (rows_tr,), (rows_va,)
        log(f"[axisctx] dir={args.axisctx_dir} rows_sha256={axis_manifest_sha} "
            f"detector_sha256={detector_model_sha} expand={axis_expand} "
            f"jitter={train_ds.jitter} stats={ctx_meta.get('stats')} "
            f"excluded={ctx_meta.get('excluded_by_manifest')}")
    elif args.crop_mode == "axis":
        if args.axis_crop_manifest is None:
            raise SystemExit("--axis-crop-manifest is required with --crop-mode axis")
        tr = build_axis_items(args.axis_crop_manifest, "train", args.data_root, args.axis_expand, args.limit)
        va = build_axis_items(args.axis_crop_manifest, "val", args.data_root, args.axis_expand, args.val_limit)
        if abs(tr[3] - va[3]) > 1e-9 or tr[4] != va[4]:
            raise SystemExit("axis train/val crop contracts differ")
        axis_manifest_sha = _sha256_file(args.axis_crop_manifest)
        detector_model_sha = tr[4]
        axis_expand = tr[3]
        train_ds = RecDataset([], char2idx, train=True, crop_mode="axis", axis_boxes=tr[2], axis_expand=tr[3])
        train_ds.items = list(zip(tr[0], tr[1]))
        val_ds = RecDataset([], char2idx, train=False, crop_mode="axis", axis_boxes=va[2], axis_expand=va[3])
        val_ds.items = list(zip(va[0], va[1]))
    else:
        if args.axis_crop_manifest is not None:
            raise SystemExit("--axis-crop-manifest requires --crop-mode axis")
        tr = build_items(TRAIN_MANIFESTS, rec_root, args.limit)
        va = build_items(VAL_MANIFESTS, rec_root, args.val_limit)
        train_ds = RecDataset([], char2idx, train=True).with_corners(tr[2])
        train_ds.items = list(zip(tr[0], tr[1]))
        val_ds = RecDataset([], char2idx, train=False).with_corners(va[2])
        val_ds.items = list(zip(va[0], va[1]))
    log(f"[data] train={len(tr[0])} val={len(va[0])}")

    nw = min(args.num_workers, 8)
    dl_kwargs = dict(num_workers=nw, batch_size=args.batch_size, collate_fn=collate,
                     pin_memory=True, persistent_workers=False)
    if nw > 0:
        dl_kwargs["prefetch_factor"] = args.prefetch_factor
    train_dl = DataLoader(train_ds, shuffle=True, drop_last=True, **dl_kwargs)
    val_dl = DataLoader(val_ds, shuffle=False, **dl_kwargs)

    model = PlateRecNet(len(charset)).to(device)
    crop_meta = _checkpoint_meta(args.crop_mode, axis_manifest_sha, detector_model_sha,
                                 axis_expand, _charset_sha256(charset))
    opt = torch.optim.AdamW(model.parameters(), lr=args.lr)
    steps_per_epoch = max(len(train_dl), 1)
    sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=args.epochs * steps_per_epoch)
    ctc = nn.CTCLoss(blank=0, zero_infinity=True)

    start_epoch = 0
    if args.resume and Path(args.resume).exists():
        ck = torch.load(args.resume, map_location=device, weights_only=False)
        if not isinstance(ck, dict) or not isinstance(ck.get("model"), dict):
            raise SystemExit("--resume requires a structured checkpoint with model and crop_meta")
        try:
            _validate_resume_meta(ck, crop_meta)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        model.load_state_dict(ck["model"])
        opt.load_state_dict(ck["optimizer"])
        sched.load_state_dict(ck["scheduler"])
        start_epoch = ck["epoch"] + 1
        best_acc = float(ck.get("best_acc", -1.0))
        main._best = best_acc
        log(f"[resume] from {args.resume} at epoch {start_epoch} (best_acc={best_acc:.4f})")
    elif args.init_weights:
        try:
            _load_init_weights(model, args.init_weights)
        except ValueError as exc:
            raise SystemExit(str(exc)) from exc
        log(f"[init-weights] loaded model only from {args.init_weights}; optimizer/scheduler/best_acc reset")

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
                                "best_acc": getattr(main, "_best", -1.0),
                                "crop_meta": crop_meta}, ckpt)
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
            torch.save({"model": model.state_dict(), "epoch": epoch, "val_acc": val_acc,
                        "crop_meta": crop_meta},
                       args.out / "best_ckpt.pth")
        torch.save({"model": model.state_dict(), "optimizer": opt.state_dict(),
                    "scheduler": sched.state_dict(), "epoch": epoch,
                    "val_acc": val_acc, "best_acc": getattr(main, "_best", -1.0),
                    "crop_meta": crop_meta},
                   args.out / "last_ckpt.pth")
        log(f"[epoch {epoch}] loss={run_loss / steps_per_epoch:.4f} val_loss={val_loss:.4f} "
            f"val_acc={val_acc:.4f} epoch_time={epoch_s:.1f}s peak_rss={peak_rss:.2f}GB "
            f"cuda_peak_alloc={cuda_peak:.0f}MB mem_available={mem_available_gb():.1f}GB")

    log("[done] training finished")
    log_f.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
