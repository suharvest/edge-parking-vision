#!/usr/bin/env python3
"""Build the axis-crop training store for PlateRecNet (runtime-matched crops).

For every recognizer training/val image this runs the plate detector exactly as
the runtime does (``evaluation/plate_e2e_eval.py``: 640 top-left letterbox,
YOLOX decode, score 0.35, NMS 0.45) and keeps the detection that overlaps the
CCPD ground-truth box best. When no detection reaches IoU 0.5 the GT
axis-aligned box is used instead (recorded as ``src: gt``).

Instead of the full 720x1160 frame, a context region around the chosen box
(box ± ``--margin`` of its width/height, clamped to the frame) is saved
losslessly as PNG, together with the box in region coordinates and the region
offset/frame size. ``train_platerec.py --crop-mode axisctx`` re-applies box
jitter + the runtime 0.08 expansion + the runtime bilinear stage-2 sampling on
that region, which is pixel-identical to sampling the full frame as long as the
jittered/expanded box stays inside the margin (it does for margin 0.4).

The detector ONNX runs on the GPU through onnx2torch (``--device cuda``, max box
difference vs onnxruntime CPU ≈0.03 px on 8 checked frames) or on onnxruntime CPU
(``--device cpu``, ~30 images/s on a GPU x86 workstation). Letterbox, decode and NMS are the
numpy functions of ``plate_e2e_eval.py`` in both cases.

Images listed in ``--exclude-set`` (the held-out evaluation JSONL files) are
dropped from every split.

    .venv/bin/python training/gen_platerec_axis_crops.py \
        --data-root <workspace> \
        --det models/plate_det_yolox_tiny_640.onnx \
        --exclude-set evaluation/data/plate_e2e/eval_set.jsonl \
        --out <workspace>/data/processed/platerec_axisctx --workers 18
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from multiprocessing import Pool
from pathlib import Path

os.environ.setdefault("OMP_NUM_THREADS", "1")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "evaluation"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import plate_e2e_eval as pe  # noqa: E402
from train_platerec import TRAIN_MANIFESTS, VAL_MANIFESTS, image_root_for  # noqa: E402

_SESS = None


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def _init(det_path: str) -> None:
    global _SESS
    import onnxruntime as ort
    so = ort.SessionOptions()
    so.intra_op_num_threads = 1
    so.inter_op_num_threads = 1
    _SESS = ort.InferenceSession(det_path, so, providers=["CPUExecutionProvider"])


def gt_bbox_from_name(name: str) -> list[float]:
    a, b = name.split("-")[2].split("_")
    x0, y0 = (float(v) for v in a.split("&"))
    x1, y1 = (float(v) for v in b.split("&"))
    return [x0, y0, x1, y1]


def _finish(task, bgr, out, scale):
    """Pick the GT-matching detection, cut + save the context region, return the row."""
    idx, split, path, plate, out_rel, out_dir, margin = task
    h, w = bgr.shape[:2]
    gt = gt_bbox_from_name(Path(path).name)
    dets = pe.yolox_decode(out, scale, w, h)
    mi, iou = pe.match_gt(dets, gt)
    if mi is not None:
        box, src, score = dets[mi]["box"], "det", dets[mi]["score"]
    else:
        box, src, score = gt, "gt", None
    # clamp detector box to the frame (runtime normalizes and clamps the same way)
    box = [max(0.0, box[0]), max(0.0, box[1]), min(float(w), box[2]), min(float(h), box[3])]
    bw, bh = box[2] - box[0], box[3] - box[1]
    if bw <= 1 or bh <= 1:
        return {"index": idx, "error": f"degenerate box {box}"}
    rx0 = int(max(0, np.floor(box[0] - bw * margin) - 2))
    ry0 = int(max(0, np.floor(box[1] - bh * margin) - 2))
    rx1 = int(min(w, np.ceil(box[2] + bw * margin) + 2))
    ry1 = int(min(h, np.ceil(box[3] + bh * margin) + 2))
    dst = Path(out_dir) / out_rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(dst), bgr[ry0:ry1, rx0:rx1], [int(cv2.IMWRITE_PNG_COMPRESSION), 1]):
        return {"index": idx, "error": f"write failed {dst}"}
    return {"index": idx, "split": split, "png": out_rel, "plate": plate,
            "src_path": path, "frame_wh": [w, h], "region_xy": [rx0, ry0],
            "box": [round(box[0] - rx0, 3), round(box[1] - ry0, 3),
                    round(box[2] - rx0, 3), round(box[3] - ry0, 3)],
            "src": src, "det_score": None if score is None else round(score, 5),
            "iou": round(iou, 4), "n_det": len(dets)}


def _job(task):
    """CPU path: decode, letterbox, onnxruntime detector, finish."""
    bgr = cv2.imread(task[2], cv2.IMREAD_COLOR)
    if bgr is None:
        return {"index": task[0], "error": f"unreadable {task[2]}"}
    canvas, scale = pe.letterbox(np.ascontiguousarray(bgr[:, :, ::-1]))
    x = np.transpose(canvas[:, :, ::-1], (2, 0, 1))[None].astype(np.float32)
    out = _SESS.run(None, {_SESS.get_inputs()[0].name: np.ascontiguousarray(x)})[0]
    return _finish(task, bgr, out, scale)


def _run_gpu(tasks, det_path: Path, workers: int, batch: int):
    """GPU path: worker threads decode JPEGs; letterbox gather + detector on CUDA."""
    from concurrent.futures import ThreadPoolExecutor
    import torch
    from onnx2torch import convert
    cv2.setNumThreads(1)
    model = convert(str(det_path)).cuda().eval()
    axes_cache: dict = {}

    def letterbox_gpu(bgr_t: "torch.Tensor", h: int, w: int):
        key = (h, w)
        if key not in axes_cache:  # same index/weight math as plate_e2e_eval.letterbox
            scale = min(pe.DET_HW / w, pe.DET_HW / h)
            iw, ih = pe.py_round(w * scale), pe.py_round(h * scale)
            ys = pe._axis(ih, 0.0, 1.0 / scale, h, True)
            xs = pe._axis(iw, 0.0, 1.0 / scale, w, True)
            axes_cache[key] = (scale, ih, iw,
                               [torch.from_numpy(np.asarray(v)).cuda() for v in ys],
                               [torch.from_numpy(np.asarray(v)).cuda() for v in xs])
        scale, ih, iw, (y0, y1, fy), (x0, x1, fx) = axes_cache[key]
        a = bgr_t.flip(-1).float()  # RGB
        rows = a[y0] * (1 - fy)[:, None, None] + a[y1] * fy[:, None, None]
        img = rows[:, x0] * (1 - fx)[None, :, None] + rows[:, x1] * fx[None, :, None]
        canvas = torch.full((pe.DET_HW, pe.DET_HW, 3), 114.0, device="cuda")
        canvas[:ih, :iw] = img
        return canvas.flip(-1).permute(2, 0, 1), scale  # BGR CHW, as the ONNX expects

    def load(task):
        return task, cv2.imread(task[2], cv2.IMREAD_COLOR)

    pool = ThreadPoolExecutor(workers)
    t0 = time.time()
    done = 0
    for b0 in range(0, len(tasks), batch):
        chunk = list(pool.map(load, tasks[b0:b0 + batch]))
        xs, metas = [], []
        for task, bgr in chunk:
            if bgr is None:
                yield {"index": task[0], "error": f"unreadable {task[2]}"}
                continue
            h, w = bgr.shape[:2]
            x, scale = letterbox_gpu(torch.from_numpy(bgr).cuda(), h, w)
            xs.append(x)
            metas.append((task, bgr, scale))
        with torch.no_grad():
            outs = model(torch.stack(xs)).float().cpu().numpy()
        for r in pool.map(lambda a: _finish(a[0][0], a[0][1], a[1][None], a[0][2]),
                          zip(metas, outs)):
            yield r
        done += len(chunk)
        if done // 10000 != (done - len(chunk)) // 10000:
            print(f"  [{done}/{len(tasks)}] {time.time() - t0:.0f}s", flush=True)
    pool.shutdown()


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-root", type=Path, default=Path(os.environ.get("EPV_DATA_ROOT", ".")))
    ap.add_argument("--det", type=Path, required=True)
    ap.add_argument("--exclude-set", type=Path, action="append", default=[])
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--margin", type=float, default=0.4)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--device", choices=("cuda", "cpu"), default="cuda")
    ap.add_argument("--batch", type=int, default=64)
    args = ap.parse_args(argv)

    excluded: set[str] = set()
    for p in args.exclude_set:
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                excluded.add(str(Path(json.loads(line)["path"]).resolve()))
    rec_root = args.data_root / "data" / "processed" / "ccpd_rec"
    tasks, dropped = [], {}
    for split, manifests in (("train", TRAIN_MANIFESTS), ("val", VAL_MANIFESTS)):
        for fname in manifests:
            root = args.data_root / image_root_for(fname)
            n = 0
            for line in (rec_root / fname).read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                rel, plate, _c = line.split("\t")
                path = str((root / rel).resolve())
                if path in excluded:
                    dropped[fname] = dropped.get(fname, 0) + 1
                    continue
                stem = fname.removesuffix(".txt")
                tasks.append((len(tasks), split, path, plate, f"{stem}/{n:06d}.png",
                              str(args.out), args.margin))
                n += 1
                if args.limit and n >= args.limit:
                    break
    print(f"[tasks] {len(tasks)} (excluded {sum(dropped.values())}: {dropped})", flush=True)
    args.out.mkdir(parents=True, exist_ok=True)
    rows, errors = [None] * len(tasks), []
    t0 = time.time()
    if args.device == "cuda":
        it = _run_gpu(tasks, args.det, args.workers, args.batch)
        pool = None
    else:
        pool = Pool(args.workers, initializer=_init, initargs=(str(args.det),))
        it = pool.imap_unordered(_job, tasks, chunksize=64)
    for k, r in enumerate(it):
        if "error" in r:
            errors.append(r)
        else:
            rows[r["index"]] = r
        if pool is not None and (k + 1) % 10000 == 0:
            print(f"  [{k + 1}/{len(tasks)}] {time.time() - t0:.0f}s", flush=True)
    if pool is not None:
        pool.close()
    rows = [r for r in rows if r is not None]
    stats = {}
    for r in rows:
        s = stats.setdefault(r["split"], {"n": 0, "det": 0, "gt": 0})
        s["n"] += 1
        s[r["src"]] += 1
    meta = {"schema": "nrd3.axisctx-crop/1", "detector_model_sha256": _sha(args.det),
            "detector_model": args.det.name, "margin": args.margin, "device": args.device,
            "exclude_sets": {str(p): _sha(p) for p in args.exclude_set},
            "excluded_by_manifest": dropped, "stats": stats, "errors": errors[:50],
            "n_errors": len(errors), "seconds": round(time.time() - t0, 1),
            "detector_config": {"det_hw": pe.DET_HW, "score_th": pe.SCORE_TH, "nms_th": pe.NMS_TH,
                                "match_iou": 0.5}}
    with (args.out / "rows.jsonl").open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    (args.out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1) + "\n",
                                        encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=1))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
