#!/usr/bin/env python3
"""Compare CPU ``RuntimeClient.infer_image`` with an independent ONNX CTC run.

The input set is the 200 PNG crop fixture under ``evaluation/data/parity``.
Each PNG is decoded and re-encoded to JPEG, and both RuntimeClient and the
independent reference consume those exact JPEG bytes. The reference then
uses RGB order, 96x32 resize and 1/255 scale before independent CTC decode.
No constant or synthetic model is used.
"""
from __future__ import annotations

import argparse
import hashlib
from io import BytesIO
import json
import os
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "core-py"))
from core_parking.plate.charset import charset  # noqa: E402
from vision_base.runtime_client import RuntimeClient  # noqa: E402


def _decode(logits: np.ndarray, symbols: list[str]) -> str:
    # Supported model contract is [1,66,1,24]; accept equivalent singleton
    # dimensions so the checker reports shape drift instead of guessing.
    a = np.asarray(logits)
    if a.ndim == 4 and a.shape[0] == a.shape[2] == 1:
        a = a[:, :, 0, :]
    if a.ndim != 3 or a.shape[0] != 1:
        raise ValueError(f"expected [1,C,T] or [1,C,1,T], got {a.shape}")
    ids = np.argmax(a[0], axis=0).tolist()
    out: list[str] = []
    prev = -1
    for idx in ids:
        if idx != 0 and idx != prev:
            out.append(symbols[idx])
        prev = idx
    return "".join(out)


def _reference(jpeg: bytes, session, input_name: str, symbols: list[str]) -> str:
    image = Image.open(BytesIO(jpeg)).convert("RGB").resize((96, 32), Image.Resampling.BILINEAR)
    tensor = np.transpose(np.asarray(image, dtype=np.float32) / 255.0, (2, 0, 1))[None]
    outputs = session.run(None, {input_name: tensor})
    if len(outputs) != 1:
        raise ValueError(f"recognizer returned {len(outputs)} outputs")
    if tuple(outputs[0].shape) != (1, 66, 1, 24):
        raise ValueError(f"recognizer output must be [1,66,1,24], got {outputs[0].shape}")
    return _decode(outputs[0], symbols)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runtime", required=True, help="CPU vb-runtime binary")
    ap.add_argument("--runtime-config", required=True)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--crops", type=Path, default=ROOT / "evaluation/data/parity/plate_rec_cn/crops")
    ap.add_argument("--manifest", type=Path, default=ROOT / "evaluation/data/parity/plate_rec_cn/crops.jsonl")
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args(argv)
    args.out.parent.mkdir(parents=True, exist_ok=True)

    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise SystemExit("onnxruntime Python package is required for the independent reference") from exc
    session = ort.InferenceSession(str(args.model), providers=["CPUExecutionProvider"])
    input_name = session.get_inputs()[0].name
    symbols = charset("cn")
    rows = [json.loads(line) for line in args.manifest.read_text(encoding="utf-8").splitlines() if line.strip()]

    config = json.loads(Path(args.runtime_config).read_text(encoding="utf-8"))
    config_path = args.out.with_suffix(".runtime.json")
    config_path.write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")
    client = RuntimeClient([str(args.runtime), "--ipc-fd", "{fd}"], str(config_path),
                           on_frame=lambda *a: None, on_event=lambda *a: None,
                           on_stats=lambda *a: None, on_state=lambda *a: None,
                           on_exit=lambda *a: None)
    results = []
    try:
        hello = client.start()
        for row in rows:
            path = args.crops / Path(row["image"]).name
            buf = BytesIO()
            Image.open(path).convert("RGB").save(buf, format="JPEG", quality=100)
            jpeg = buf.getvalue()
            native = client.infer_image(jpeg, timeout_s=10)
            reference = _reference(jpeg, session, input_name, symbols)
            results.append({"index": row["index"], "image": row["image"],
                            "expected": row.get("plate"), "native": native.get("text"),
                            "reference": reference,
                            "match": native.get("text") == reference,
                            "source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                            "jpeg_sha256": hashlib.sha256(jpeg).hexdigest()})
    finally:
        client.stop()
    matched = sum(bool(r["match"]) for r in results)
    summary = {"count": len(results), "matched": matched,
               "agreement": matched / len(results) if results else 0.0,
               "runtime": str(args.runtime), "model": str(args.model),
               "hello": {"runtime_version": hello.runtime_version,
                         "stage2_ready": hello.stage2_ready}}
    args.out.write_text(json.dumps({"summary": summary, "rows": results},
                                   ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["agreement"] >= 0.98 else 1


if __name__ == "__main__":
    raise SystemExit(main())
