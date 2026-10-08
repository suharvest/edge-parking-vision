#!/usr/bin/env python3
"""Build one fixed-shape vehicle ONNX model as a quantized RKNN artifact.

The calibration images are derived outside the repository.  They use the
model-card contract: BGR byte pixels, top-left letterbox, and a fill value of
114.  The RKNN toolkit then reads those lossless PNGs for w8a8 calibration.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import importlib.metadata
from pathlib import Path

import cv2
import numpy as np


def letterbox_bgr(path: Path, size: int) -> np.ndarray:
    image = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if image is None:
        raise RuntimeError(f"cannot read calibration image: {path}")
    height, width = image.shape[:2]
    scale = min(size / width, size / height)
    scaled_w = max(1, round(width * scale))
    scaled_h = max(1, round(height * scale))
    resized = cv2.resize(image, (scaled_w, scaled_h), interpolation=cv2.INTER_LINEAR)
    canvas = np.full((size, size, 3), 114, dtype=np.uint8)
    canvas[:scaled_h, :scaled_w] = resized
    return canvas


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--onnx", type=Path, required=True)
    parser.add_argument("--calib-list", type=Path, required=True)
    parser.add_argument("--size", type=int, choices=(416, 640), required=True)
    parser.add_argument("--target", choices=("rk3576", "rk3588"), required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    card = json.loads((repo_root / "MODEL-CARD.json").read_text())
    variants = {variant["name"]: variant for variant in card["variants"]}
    expected_name = f"vehicle{args.size}"
    variant = variants.get(expected_name)
    if variant is None:
        raise RuntimeError(f"MODEL-CARD has no {expected_name} variant")
    expected_model = (repo_root / variant["file"]).resolve()
    if args.onnx.resolve() != expected_model:
        raise RuntimeError(
            f"--onnx must be MODEL-CARD {expected_name}: {expected_model}"
        )
    actual_sha = sha256(args.onnx)
    if actual_sha != variant["sha256"]:
        raise RuntimeError(
            f"ONNX SHA mismatch for {expected_name}: {actual_sha} != {variant['sha256']}"
        )

    import onnx

    graph = onnx.load(str(args.onnx)).graph
    graph_inputs = [value for value in graph.input if value.name not in {i.name for i in graph.initializer}]
    graph_outputs = list(graph.output)
    actual_input = list(graph_inputs[0].type.tensor_type.shape.dim)
    actual_output = list(graph_outputs[0].type.tensor_type.shape.dim)
    input_shape = [dim.dim_value for dim in actual_input]
    output_shape = [dim.dim_value for dim in actual_output]
    if input_shape != variant["input_shape"] or output_shape != variant["output_shape"]:
        raise RuntimeError(
            f"ONNX shape mismatch: input={input_shape} output={output_shape}; "
            f"expected input={variant['input_shape']} output={variant['output_shape']}"
        )

    args.out_dir.mkdir(parents=True, exist_ok=True)
    derived = args.out_dir / f"calib_{args.size}_sdk_rgb_wire_model_bgr_png"
    derived.mkdir(parents=True, exist_ok=True)
    source_paths = [Path(line.strip()) for line in args.calib_list.read_text().splitlines() if line.strip()]
    if not source_paths:
        raise RuntimeError("calibration list is empty")

    derived_paths: list[Path] = []
    for index, source in enumerate(source_paths):
        image = letterbox_bgr(source, args.size)
        destination = derived / f"{index:04d}.png"
        if not cv2.imwrite(str(destination), image):
            raise RuntimeError(f"cannot write derived calibration image: {destination}")
        derived_paths.append(destination)

    dataset = args.out_dir / f"dataset_vehicle_{args.size}.txt"
    dataset.write_text("".join(f"{path}\n" for path in derived_paths))
    print(f"calibration source count={len(source_paths)} derived count={len(derived_paths)}")
    print(f"calibration source list sha256={sha256(args.calib_list)}")
    print(f"derived dataset={dataset}")

    from rknn.api import RKNN
    import rknn.api as rknn_api

    sdk_version = importlib.metadata.version("rknn-toolkit2")

    output = args.out_dir / f"vehicle_{args.size}.{args.target}.w8a8.rknn"
    rknn = RKNN(verbose=True)
    try:
        ret = rknn.config(
            mean_values=[[0, 0, 0]],
            std_values=[[1, 1, 1]],
            target_platform=args.target,
            quantized_dtype="w8a8",
            optimization_level=3,
            # PNG/JPG loaders expose RGB wire order to the toolkit.  The model
            # contract is BGR, so ask the SDK to swap the loaded calibration
            # image before quantization.
            quant_img_RGB2BGR=True,
        )
        if ret != 0:
            raise RuntimeError(f"rknn.config returned {ret}")
        ret = rknn.load_onnx(model=str(args.onnx))
        if ret != 0:
            raise RuntimeError(f"rknn.load_onnx returned {ret}")
        ret = rknn.build(do_quantization=True, dataset=str(dataset))
        if ret != 0:
            raise RuntimeError(f"rknn.build returned {ret}")
        ret = rknn.export_rknn(str(output))
        if ret != 0:
            raise RuntimeError(f"rknn.export_rknn returned {ret}")
    finally:
        rknn.release()

    manifest = {
        "onnx": str(args.onnx),
        "onnx_sha256": sha256(args.onnx),
        "target": args.target,
        "sdk": {
            "package": "rknn-toolkit2",
            "version": sdk_version,
            "module": str(Path(rknn_api.__file__).resolve()),
        },
        "quantization": "w8a8",
        "input_preprocess": {
            "source_color": "BGR",
            "range": "0-255",
            "mean": [0, 0, 0],
            "std": [1, 1, 1],
            "resize": "INTER_LINEAR aspect-preserving",
            "letterbox": "top_left",
            "pad": 114,
        },
        "calibration_list": str(args.calib_list),
        "calibration_list_sha256": sha256(args.calib_list),
        "calibration_count": len(source_paths),
        "derived_dataset": str(dataset),
        "calibration_source_wiring": {
            "file_wire_order": "PNG standard RGB",
            "decoded_source_array": "BGR before cv2.imwrite",
            "sdk_quant_img_RGB2BGR": True,
            "model_tensor_order": "BGR",
        },
        "derived_sha256": {str(path): sha256(path) for path in derived_paths},
        "output": str(output),
        "output_sha256": sha256(output),
        "output_fields": "unknown until runtime query",
        "native_input_type_layout_quantization": "unknown until device query",
        "device_verified": False,
    }
    manifest_path = output.with_suffix(".manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"output={output}")
    print(f"output_sha256={manifest['output_sha256']}")
    print(f"manifest={manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
