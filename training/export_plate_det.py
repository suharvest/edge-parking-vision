#!/usr/bin/env python3
"""导出车牌检测 YOLOX ONNX（NRD-3 §6.6 / P5）。

静态 batch-1、opset 11、输入名 images、decode_in_inference=False
（解码与 NMS 由基座/postprocess 统一做，同 NRD-2 §S5）。
同一权重导出两份：`--size 640` → plate_det_yolox_tiny_640.onnx，
`--size 416` → plate_det_yolox_tiny_416.onnx（SG2002 用）。

不用 YOLOX 自带 tools/export_onnx.py：torch.onnx._export 在 torch 2.x 已移除；
显式 dynamo=False 走 TorchScript 导出器（opset 11 需要）。
"""
from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

import torch

INPUT_NAME = "images"
OUTPUT_NAME = "output"


def load_exp(exp_file: Path):
    spec = importlib.util.spec_from_file_location("plate_exp", exp_file)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.Exp()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    here = Path(__file__).resolve().parent
    parser.add_argument("-f", "--exp-file", type=Path,
                        default=here / "exp" / "yolox_tiny_plate.py")
    parser.add_argument("-c", "--ckpt", type=Path, required=True)
    parser.add_argument("-o", "--output", type=Path, required=True)
    parser.add_argument("--opset", type=int, default=11)
    parser.add_argument("--size", type=int, default=0,
                        help="导出输入边长（覆盖 exp.test_size；0=用 exp 值）")
    args = parser.parse_args()

    exp = load_exp(args.exp_file)
    model = exp.get_model()
    # 训练产物由本机产出，来源可信；torch>=2.6 默认 weights_only=True 会拒绝
    # YOLOX checkpoint 里的 numpy scalar。
    ckpt = torch.load(args.ckpt, map_location="cpu", weights_only=False)
    model.load_state_dict(ckpt["model"])
    model.eval()
    model.head.decode_in_inference = False

    height, width = exp.test_size
    if args.size:
        height = width = args.size
    dummy = torch.zeros(1, 3, height, width)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    torch.onnx.export(
        model, dummy, str(args.output),
        input_names=[INPUT_NAME], output_names=[OUTPUT_NAME],
        opset_version=args.opset, dynamic_axes=None, dynamo=False,
    )

    import onnx
    graph = onnx.load(str(args.output))
    onnx.checker.check_model(graph)
    inputs = graph.graph.input
    assert len(inputs) == 1, f"expected 1 input, got {len(inputs)}"
    assert inputs[0].name == INPUT_NAME, inputs[0].name
    shape = [d.dim_value for d in inputs[0].type.tensor_type.shape.dim]
    assert shape == [1, 3, height, width], shape
    out_shape = [d.dim_value for d in
                 graph.graph.output[0].type.tensor_type.shape.dim]
    print(f"exported {args.output}")
    print(f"  input  : {inputs[0].name} {shape}")
    print(f"  output : {graph.graph.output[0].name} {out_shape}")
    print(f"  opset  : {graph.opset_import[0].version}")
    print(f"  classes: {exp.num_classes}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
