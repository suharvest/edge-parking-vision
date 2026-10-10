#!/usr/bin/env python3
"""End-to-end plate accuracy: detector + recognizer + runtime gate/vote.

Re-implements the vb-runtime CPU path used by the ``plate`` preset
(platforms/cpu/presets/plate.json) in numpy so the same code runs on spark
(ONNX FP32) and on devices (RKNN INT8 via rknnlite, TensorRT FP16):

* detector: top-left letterbox to 640x640, pad 114, half-pixel bilinear
  (backends/cpu/cpu_backend.cpp preprocess), BGR, 0-255; YOLOX raw-head grid
  decode with strides 8/16/32, score = obj*cls > 0.35, greedy NMS 0.45
  (src/post/decoder.cpp, yolox_decode.cpp, nms.cpp).
* stage2: at most 4 crops per frame by score (max_crops_per_frame default),
  box expanded by 0.08 per side, half-pixel bilinear sample of the source
  frame to 96x32 RGB, x/255 (src/stage2.cpp, backends/cpu/cpu_stage2.cpp).
* CTC greedy with per-step softmax; mean_conf / min_char_conf as in
  src/post/ctc_greedy.cpp.
* text_vote gate: read counts only when min_char_conf >= 0.6 and the text
  matches the CN blue/green pattern; publish when one text reaches 3 votes
  within the 7-read window, or 2 votes at track end (flush_min_agree)
  (src/analyzers/text_vote.cpp, preset vote block).

The preset ROI polygon is site configuration and is NOT applied here.
Images are single stills, so the vote is simulated with N jittered copies of
each image (``--vote-frames``); see the results markdown for the caveat.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
import zlib
from pathlib import Path

import numpy as np

PROVINCES = "京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤青藏川宁琼"
LETTERS = "ABCDEFGHJKLMNPQRSTUVWXYZ"
DIGITS = "0123456789"
CHARSET = [""] + list(PROVINCES) + list(LETTERS) + list(DIGITS)  # 66, index 0 = blank
BLUE_RE = re.compile(r"^[%s][A-HJ-NP-Z][A-HJ-NP-Z0-9]{5}$" % PROVINCES)
GREEN_RE = re.compile(r"^[%s][A-HJ-NP-Z][A-HJ-NP-Z0-9]{6}$" % PROVINCES)

DET_HW = 640
SCORE_TH = 0.35
NMS_TH = 0.45
STRIDES = (8, 16, 32)
EXPAND = 0.08
MAX_CROPS = 4
REC_H, REC_W = 32, 96
MIN_CONF = 0.6
WINDOW, MIN_AGREE, FLUSH_MIN_AGREE = 7, 3, 2


# ---------------------------------------------------------------- image io
def load_rgb(path: str) -> np.ndarray:
    """RGB uint8 HxWx3. ``.rgbz`` = zlib(raw) with an 8-byte h,w header."""
    if path.endswith(".rgbz"):
        raw = Path(path).read_bytes()
        h, w = int.from_bytes(raw[:4], "little"), int.from_bytes(raw[4:8], "little")
        return np.frombuffer(zlib.decompress(raw[8:]), np.uint8).reshape(h, w, 3)
    import cv2  # spark only
    bgr = cv2.imread(path, cv2.IMREAD_COLOR)
    if bgr is None:
        raise FileNotFoundError(path)
    return np.ascontiguousarray(bgr[:, :, ::-1])


def save_rgbz(img: np.ndarray, path: Path) -> None:
    h, w = img.shape[:2]
    path.write_bytes(h.to_bytes(4, "little") + w.to_bytes(4, "little")
                     + zlib.compress(np.ascontiguousarray(img).tobytes(), 6))


# ---------------------------------------------------------------- geometry
def py_round(v: float) -> int:
    f = math.floor(v)
    r = v - f
    if r > 0.5:
        return int(f) + 1
    if r < 0.5:
        return int(f)
    return int(f) if int(f) % 2 == 0 else int(f) + 1


def _axis(n_out: int, start: float, step: float, n_src: int, clamp_fy_zero: bool):
    s = start + (np.arange(n_out, dtype=np.float32) + 0.5) * step - 0.5
    if clamp_fy_zero:  # cpu_backend letterbox: clamp index and zero the weight
        i0 = np.floor(s).astype(np.int64)
        f = (s - i0).astype(np.float32)
        lo, hi = i0 < 0, i0 > n_src - 1
        i0 = np.clip(i0, 0, n_src - 1)
        f[lo | hi] = 0
    else:  # cpu_stage2: clamp the coordinate first
        s = np.clip(s, 0, n_src - 1)
        i0 = np.floor(s).astype(np.int64)
        f = (s - i0).astype(np.float32)
    i1 = np.minimum(i0 + 1, n_src - 1)
    return i0, i1, f


def _bilinear(img: np.ndarray, y, x) -> np.ndarray:
    y0, y1, fy = y
    x0, x1, fx = x
    a = img.astype(np.float32)
    rows = a[y0] * (1 - fy)[:, None, None] + a[y1] * fy[:, None, None]
    return rows[:, x0] * (1 - fx)[None, :, None] + rows[:, x1] * fx[None, :, None]


def letterbox(img: np.ndarray):
    """Returns float32 HxWx3 *RGB* canvas (0-255, pad 114) and scale."""
    h, w = img.shape[:2]
    scale = min(DET_HW / w, DET_HW / h)
    iw, ih = py_round(w * scale), py_round(h * scale)
    canvas = np.full((DET_HW, DET_HW, 3), 114.0, np.float32)
    ys = _axis(ih, 0.0, 1.0 / scale, h, True)
    xs = _axis(iw, 0.0, 1.0 / scale, w, True)
    canvas[:ih, :iw] = _bilinear(img, ys, xs)
    return canvas, scale


def crop_rec(img: np.ndarray, box) -> np.ndarray:
    """box = normalized (x0,y0,x1,y1) of the source frame -> float32 32x96x3 RGB 0-255."""
    h, w = img.shape[:2]
    x0, y0, x1, y1 = box[0] * w, box[1] * h, box[2] * w, box[3] * h
    ys = _axis(REC_H, y0, (y1 - y0) / REC_H, h, False)
    xs = _axis(REC_W, x0, (x1 - x0) / REC_W, w, False)
    return _bilinear(img, ys, xs)


def yolox_decode(out: np.ndarray, scale: float, src_w: int, src_h: int):
    a = np.asarray(out, np.float32).reshape(-1, out.shape[-1])
    gx, gy, gs = [], [], []
    for s in STRIDES:
        n = DET_HW // s
        yy, xx = np.meshgrid(np.arange(n), np.arange(n), indexing="ij")
        gx.append(xx.ravel()); gy.append(yy.ravel()); gs.append(np.full(n * n, s))
    gx, gy, gs = (np.concatenate(v).astype(np.float32) for v in (gx, gy, gs))
    assert a.shape[0] == gx.size, a.shape
    cx = (a[:, 0] + gx) * gs
    cy = (a[:, 1] + gy) * gs
    bw = np.exp(a[:, 2]) * gs
    bh = np.exp(a[:, 3]) * gs
    score = a[:, 4] * a[:, 5:].max(axis=1)
    keep = np.isfinite(score) & (score > SCORE_TH)
    cx, cy, bw, bh, score = cx[keep], cy[keep], bw[keep], bh[keep], score[keep]
    order = np.argsort(-score, kind="stable")
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], 1)[order]
    score = score[order]
    kept = []
    sup = np.zeros(len(score), bool)
    for i in range(len(score)):
        if sup[i]:
            continue
        kept.append(i)
        iou = box_iou(boxes[i], boxes)
        sup |= iou > NMS_TH
    dets = []
    for i in kept:  # model canvas px -> source px (top-left letterbox)
        b = boxes[i] / scale
        dets.append({"box": [float(v) for v in b], "score": float(score[i])})
    return dets


def box_iou(b, boxes):
    boxes = np.asarray(boxes, np.float32).reshape(-1, 4)
    ix = np.clip(np.minimum(b[2], boxes[:, 2]) - np.maximum(b[0], boxes[:, 0]), 0, None)
    iy = np.clip(np.minimum(b[3], boxes[:, 3]) - np.maximum(b[1], boxes[:, 1]), 0, None)
    inter = ix * iy
    uni = (b[2] - b[0]) * (b[3] - b[1]) + (boxes[:, 2] - boxes[:, 0]) * (boxes[:, 3] - boxes[:, 1]) - inter
    return np.where(uni > 0, inter / np.maximum(uni, 1e-9), 0)


def expand_box(box, w, h):
    cx, cy = (box[0] + box[2]) / 2 / w, (box[1] + box[3]) / 2 / h
    bw, bh = (box[2] - box[0]) / w, (box[3] - box[1]) / h
    x0, y0 = max(0.0, cx - bw * (0.5 + EXPAND)), max(0.0, cy - bh * (0.5 + EXPAND))
    x1, y1 = min(1.0, cx + bw * (0.5 + EXPAND)), min(1.0, cy + bh * (0.5 + EXPAND))
    return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None


def ctc_greedy(logits: np.ndarray):
    a = np.asarray(logits, np.float32)
    a = a.reshape(a.shape[-3] if a.ndim == 4 else a.shape[-2], -1)  # [C,T]
    assert a.shape[0] == len(CHARSET), a.shape
    m = a.max(axis=0, keepdims=True)
    p = np.exp(a - m)
    p /= p.sum(axis=0, keepdims=True)
    best = p.argmax(axis=0)
    bp = p.max(axis=0)
    out, conf, prev = [], [], -1
    for t in range(a.shape[1]):
        b = int(best[t])
        if b != 0 and b != prev:
            out.append(CHARSET[b]); conf.append(float(bp[t]))
        elif b != 0 and b == prev and conf:
            conf[-1] = max(conf[-1], float(bp[t]))
        prev = b
    if not conf:
        return "", 0.0, 0.0
    return "".join(out), sum(conf) / len(conf), min(conf)


def gate_ok(text: str, minc: float) -> bool:
    return bool(text) and minc >= MIN_CONF and bool(BLUE_RE.match(text) or GREEN_RE.match(text))


def vote(reads):
    """reads: list of (text, minc) for one track in frame order -> published text or None."""
    q, emitted = [], None
    for text, minc in reads:
        if not gate_ok(text, minc):
            continue
        q.append(text)
        q = q[-WINDOW:]
        counts = {}
        top, n = None, 0
        for v in q:
            counts[v] = counts.get(v, 0) + 1
            if counts[v] > n:
                n, top = counts[v], v
        if n >= MIN_AGREE:
            return top, False
    counts, top, n = {}, None, 0
    for v in q:
        counts[v] = counts.get(v, 0) + 1
        if counts[v] > n:
            n, top = counts[v], v
    if n >= FLUSH_MIN_AGREE:
        return top, True
    return None, False


def levenshtein(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def jitter(img: np.ndarray, rng: random.Random) -> np.ndarray:
    """Small affine + gain change standing in for consecutive video frames; returns (img, M)."""
    import cv2  # spark only
    h, w = img.shape[:2]
    s = rng.uniform(0.95, 1.05)
    ang = rng.uniform(-2.0, 2.0)
    tx, ty = rng.uniform(-0.02, 0.02) * w, rng.uniform(-0.02, 0.02) * h
    m = cv2.getRotationMatrix2D((w / 2, h / 2), ang, s)
    m[0, 2] += tx; m[1, 2] += ty
    out = cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    g = rng.uniform(0.85, 1.15)
    return np.clip(out.astype(np.float32) * g, 0, 255).astype(np.uint8), m


def warp_box(box, m):
    pts = np.array([[box[0], box[1], 1], [box[2], box[1], 1], [box[0], box[3], 1], [box[2], box[3], 1]], np.float32)
    q = pts @ np.asarray(m, np.float32).T
    return [float(q[:, 0].min()), float(q[:, 1].min()), float(q[:, 0].max()), float(q[:, 1].max())]


# ---------------------------------------------------------------- backends
class OnnxBackend:
    def __init__(self, det, rec):
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = int(os.environ.get("ORT_THREADS", "8"))
        self.det = ort.InferenceSession(det, so, providers=["CPUExecutionProvider"])
        self.rec = ort.InferenceSession(rec, so, providers=["CPUExecutionProvider"])
        self.di, self.ri = self.det.get_inputs()[0].name, self.rec.get_inputs()[0].name

    def run_det(self, canvas_rgb):
        x = np.transpose(canvas_rgb[:, :, ::-1], (2, 0, 1))[None].astype(np.float32)
        return self.det.run(None, {self.di: np.ascontiguousarray(x)})[0]

    def run_rec(self, crop_rgb):
        x = np.transpose(crop_rgb / 255.0, (2, 0, 1))[None].astype(np.float32)
        return self.rec.run(None, {self.ri: np.ascontiguousarray(x)})[0]


class RknnBackend:
    """rknnlite; models carry mean/std, so inputs are uint8 NHWC (det BGR, rec RGB)."""

    def __init__(self, det, rec):
        from rknnlite.api import RKNNLite
        self.det, self.rec = RKNNLite(), RKNNLite()
        for r, p in ((self.det, det), (self.rec, rec)):
            assert r.load_rknn(p) == 0, p
            assert r.init_runtime() == 0, p

    def run_det(self, canvas_rgb):
        x = np.clip(np.floor(canvas_rgb[:, :, ::-1] + 0.5), 0, 255).astype(np.uint8)[None]
        return np.asarray(self.det.inference(inputs=[x], data_format="nhwc")[0], np.float32)

    def run_rec(self, crop_rgb):
        x = np.clip(np.floor(crop_rgb + 0.5), 0, 255).astype(np.uint8)[None]
        return np.asarray(self.rec.inference(inputs=[x], data_format="nhwc")[0], np.float32)


class TrtBackend:
    def __init__(self, det, rec):
        import tensorrt as trt
        from cuda import cudart
        self.cudart = cudart
        self.logger = trt.Logger(trt.Logger.WARNING)
        self.rt = trt.Runtime(self.logger)
        self.m = [self._load(det, trt), self._load(rec, trt)]

    def _ck(self, r):
        err = r[0] if isinstance(r, tuple) else r
        assert int(err) == 0, err
        return r[1] if isinstance(r, tuple) and len(r) > 1 else None

    def _load(self, path, trt):
        eng = self.rt.deserialize_cuda_engine(Path(path).read_bytes())
        ctx = eng.create_execution_context()
        io = {}
        for i in range(eng.num_io_tensors):
            n = eng.get_tensor_name(i)
            shape = tuple(eng.get_tensor_shape(n))
            dt = np.dtype(trt.nptype(eng.get_tensor_dtype(n)))
            nbytes = int(np.prod(shape)) * dt.itemsize
            ptr = self._ck(self.cudart.cudaMalloc(nbytes))
            ctx.set_tensor_address(n, ptr)
            io[n] = (eng.get_tensor_mode(n) == trt.TensorIOMode.INPUT, shape, dt, ptr, nbytes)
        stream = self._ck(self.cudart.cudaStreamCreate())
        return eng, ctx, io, stream

    def _infer(self, k, x):
        eng, ctx, io, stream = self.m[k]
        out = None
        for n, (is_in, shape, dt, ptr, nbytes) in io.items():
            if is_in:
                a = np.ascontiguousarray(x.astype(dt).reshape(shape))
                self._ck(self.cudart.cudaMemcpy(ptr, a.ctypes.data, nbytes,
                                                self.cudart.cudaMemcpyKind.cudaMemcpyHostToDevice))
        assert ctx.execute_async_v3(stream)
        self._ck(self.cudart.cudaStreamSynchronize(stream))
        for n, (is_in, shape, dt, ptr, nbytes) in io.items():
            if not is_in:
                out = np.empty(shape, dt)
                self._ck(self.cudart.cudaMemcpy(out.ctypes.data, ptr, nbytes,
                                                self.cudart.cudaMemcpyKind.cudaMemcpyDeviceToHost))
        return out.astype(np.float32)

    def run_det(self, canvas_rgb):
        return self._infer(0, np.transpose(canvas_rgb[:, :, ::-1], (2, 0, 1))[None])

    def run_rec(self, crop_rgb):
        return self._infer(1, np.transpose(crop_rgb / 255.0, (2, 0, 1))[None])


# ---------------------------------------------------------------- pipeline
def run_frame(be, img):
    h, w = img.shape[:2]
    canvas, scale = letterbox(img)
    dets = yolox_decode(be.run_det(canvas), scale, w, h)
    for i, d in enumerate(dets):  # dets are score-sorted; stage2 takes the top 4
        d["read"] = None
        if i >= MAX_CROPS:
            continue
        box = expand_box(d["box"], w, h)
        if box is None:
            continue
        text, mean, minc = ctc_greedy(be.run_rec(crop_rec(img, box)))
        d["read"] = {"text": text, "mean_conf": round(mean, 5), "min_char_conf": round(minc, 5)}
    return dets


def match_gt(dets, gt):
    if not dets:
        return None, 0.0
    ious = box_iou(np.asarray(gt, np.float32), [d["box"] for d in dets])
    i = int(np.argmax(ious))
    return (i, float(ious[i])) if ious[i] >= 0.5 else (None, float(ious[i]))


def evaluate(rows, be, vote_frames: int, seed: int, log_every: int = 200):
    out = []
    t0 = time.time()
    for k, r in enumerate(rows):
        img = load_rgb(r["path"])
        dets = run_frame(be, img)
        mi, iou = match_gt(dets, r["bbox"])
        read = dets[mi]["read"] if mi is not None else None
        text = read["text"] if read else ""
        rec = {"id": r["id"], "subset": r["subset"], "tier": r["tier"], "gt": r["plate"],
               "detected": mi is not None, "iou": round(iou, 4), "n_det": len(dets),
               "pred": text, "min_char_conf": read["min_char_conf"] if read else None,
               "mean_conf": read["mean_conf"] if read else None,
               "exact": text == r["plate"],
               "char_acc": max(0.0, 1 - levenshtein(text, r["plate"]) / len(r["plate"])),
               "gated": bool(read) and gate_ok(text, read["min_char_conf"]),
               "fp_dets": len(dets) - (1 if mi is not None else 0),
               "fp_gated_reads": sum(1 for j, d in enumerate(dets) if j != mi and d["read"]
                                     and gate_ok(d["read"]["text"], d["read"]["min_char_conf"]))}
        if vote_frames > 1:
            rng = random.Random(f"{seed}:{r['id']}")
            seq = [(text, read["min_char_conf"] if read else 0.0)]
            for _ in range(vote_frames - 1):
                jimg, m = jitter(img, rng)
                jd = run_frame(be, jimg)
                # follow the plate through the known warp (stand-in for the tracker)
                ji, _ = match_gt(jd, warp_box(r["bbox"], m)) if jd else (None, 0)
                jr = jd[ji]["read"] if ji is not None else None
                seq.append((jr["text"], jr["min_char_conf"]) if jr else ("", 0.0))
            pub, flushed = vote(seq)
            rec.update({"vote_reads": [s[0] for s in seq], "published": pub,
                        "published_flush": flushed,
                        "published_correct": pub == r["plate"] if pub else None})
        out.append(rec)
        if (k + 1) % log_every == 0:
            print(f"  [{k + 1}/{len(rows)}] {time.time() - t0:.0f}s", flush=True)
    return out


def summarize(results):
    groups = {}
    for r in results:
        for key in (r["subset"], r["tier"], "all"):
            groups.setdefault(key, []).append(r)
    summ = {}
    for key, rs in groups.items():
        n = len(rs)
        det = sum(r["detected"] for r in rs)
        gated = [r for r in rs if r["gated"]]
        s = {"n": n, "det_recall": det / n,
             "exact": sum(r["exact"] for r in rs) / n,
             "exact_given_det": (sum(r["exact"] for r in rs if r["detected"]) / det) if det else None,
             "char_acc": sum(r["char_acc"] for r in rs) / n,
             "gated_rate": len(gated) / n,
             "gated_precision": (sum(r["exact"] for r in gated) / len(gated)) if gated else None,
             "fp_dets_per_img": sum(r["fp_dets"] for r in rs) / n,
             "fp_gated_reads": sum(r["fp_gated_reads"] for r in rs)}
        if "published" in rs[0]:
            pub = [r for r in rs if r["published"]]
            s.update({"publish_rate": len(pub) / n,
                      "publish_precision": (sum(r["published_correct"] for r in pub) / len(pub)) if pub else None,
                      "published_correct_rate": sum(bool(r["published_correct"]) for r in pub) / n,
                      "published_via_flush": sum(r["published_flush"] for r in pub)})
        summ[key] = {k: (round(v, 4) if isinstance(v, float) else v) for k, v in s.items()}
    return summ


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", type=Path, required=True, help="JSONL from plate_e2e_build_set.py")
    ap.add_argument("--backend", choices=("onnx", "rknn", "trt"), default="onnx")
    ap.add_argument("--det", required=True)
    ap.add_argument("--rec", required=True)
    ap.add_argument("--path-prefix", default=None,
                    help="rewrite each row path to <prefix>/<id>.rgbz (device runs)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--vote-frames", type=int, default=0)
    ap.add_argument("--seed", type=int, default=20261009)
    ap.add_argument("--export-rgbz", type=Path, default=None,
                    help="decode the set and write <dir>/<id>.rgbz, then exit")
    ap.add_argument("--out", type=Path, required=False)
    args = ap.parse_args(argv)

    rows = [json.loads(l) for l in args.set.read_text(encoding="utf-8").splitlines() if l.strip()]
    if args.limit:
        rows = rows[: args.limit]
    if args.export_rgbz:
        args.export_rgbz.mkdir(parents=True, exist_ok=True)
        for r in rows:
            save_rgbz(load_rgb(r["path"]), args.export_rgbz / f"{r['id']}.rgbz")
        print(f"exported {len(rows)} -> {args.export_rgbz}")
        return 0
    if args.path_prefix:
        for r in rows:
            r["path"] = f"{args.path_prefix}/{r['id']}.rgbz"
    be = {"onnx": OnnxBackend, "rknn": RknnBackend, "trt": TrtBackend}[args.backend](args.det, args.rec)
    t0 = time.time()
    res = evaluate(rows, be, args.vote_frames, args.seed)
    summ = summarize(res)
    meta = {"backend": args.backend, "det": args.det, "rec": args.rec, "set": str(args.set),
            "n": len(rows), "vote_frames": args.vote_frames, "seconds": round(time.time() - t0, 1),
            "config": {"det_hw": DET_HW, "score_th": SCORE_TH, "nms_th": NMS_TH, "expand": EXPAND,
                       "max_crops": MAX_CROPS, "min_conf": MIN_CONF, "window": WINDOW,
                       "min_agree": MIN_AGREE, "flush_min_agree": FLUSH_MIN_AGREE, "roi": None}}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({"meta": meta, "summary": summ, "rows": res},
                                   ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"meta": meta, "summary": summ}, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
