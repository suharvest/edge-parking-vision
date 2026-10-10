#!/usr/bin/env python3
"""导出 PlateRecNet CN ONNX（NRD-3 §6.5 / §8 P6）：models/plate_rec_cn_96x32.onnx。

- opset 11、静态 batch 1、导出前折叠全部 BN（Conv+BN → Conv）；
- 形状检查 1×3×32×96 → 1×C×24；不含 Softmax/LogSoftmax/Squeeze/AdaptiveAvgPool
  （节点类型检查由 tests/test_platerec_export.py 负责）。

用法（GPU x86 工作站工作区）：.venv/bin/python training/export_platerec.py \
    --ckpt training/outputs/platerec_cn/best.pth --out models/plate_rec_cn_96x32.onnx
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import torch
import torch.nn as nn

sys.path.insert(0, str(Path(__file__).resolve().parent))
from platerec import PlateRecNet, cn_charset  # noqa: E402


def fold_bn(conv: nn.Conv2d, bn: nn.BatchNorm2d) -> nn.Conv2d:
    """Conv+BN 折叠为单个 Conv（推理等价：y = conv(x) * s + b）。"""
    w = conv.weight.data
    b = conv.bias.data if conv.bias is not None else torch.zeros(conv.out_channels)
    s = bn.weight.data / torch.sqrt(bn.running_var + bn.eps)
    conv2 = nn.Conv2d(conv.in_channels, conv.out_channels, conv.kernel_size,
                      conv.stride, conv.padding, bias=True)
    conv2.weight.data = w * s.view(-1, 1, 1, 1)
    conv2.bias.data = b - bn.running_mean * s + bn.bias.data
    return conv2.eval()


def fold_model(model: PlateRecNet) -> PlateRecNet:
    m = model.eval()
    m.stem[0] = fold_bn(m.stem[0], m.stem[1])
    m.stem[1] = nn.Identity()
    for blk_name in ("block1", "block2", "block3"):
        blk = getattr(m, blk_name)
        blk.conv1, blk.bn1 = fold_bn(blk.conv1, blk.bn1), nn.Identity()
        blk.conv2, blk.bn2 = fold_bn(blk.conv2, blk.bn2), nn.Identity()
        blk.conv3, blk.bn3 = fold_bn(blk.conv3, blk.bn3), nn.Identity()
        blk.conv4, blk.bn4 = fold_bn(blk.conv4, blk.bn4), nn.Identity()
    m.trunk_conv, m.trunk_bn = fold_bn(m.trunk_conv, m.trunk_bn), nn.Identity()
    m.head[0], m.head[1] = fold_bn(m.head[0], m.head[1]), nn.Identity()
    m.ctx_conv, m.ctx_bn = fold_bn(m.ctx_conv, m.ctx_bn), nn.Identity()
    return m


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="export PlateRecNet CN ONNX (opset 11)")
    ap.add_argument("--ckpt", type=Path, default=Path("training/outputs/platerec_cn/best.pth"))
    ap.add_argument("--out", type=Path, default=Path("models/plate_rec_cn_96x32.onnx"))
    args = ap.parse_args(argv)

    charset = cn_charset()
    model = PlateRecNet(len(charset))
    ck = torch.load(args.ckpt, map_location="cpu", weights_only=False)
    sd = ck.get("model", ck)
    model.load_state_dict(sd)
    model = fold_model(model)

    dummy = torch.randn(1, 3, 32, 96)
    with torch.no_grad():
        ref = model(dummy)
    # 规格 “1×C×24”：图内无 Squeeze/Reshape，H 维由 (8,1) 卷积坍缩为 1
    assert ref.shape == (1, len(charset), 1, 24), ref.shape

    args.out.parent.mkdir(parents=True, exist_ok=True)
    state_out = args.out.with_suffix(".state.pt")
    torch.save(model.state_dict(), state_out)  # 折 BN 后权重，供一致性测试同权重比对
    torch.onnx.export(model, dummy, str(args.out), opset_version=11,
                      input_names=["input"], output_names=["output"],
                      do_constant_folding=True, dynamic_axes=None,
                      dynamo=False)  # 传统 TorchScript 导出器（opset 11，无需 onnxscript）
    print(f"exported {args.out}")

    # 形状检查
    import onnxruntime as ort
    sess = ort.InferenceSession(str(args.out), providers=["CPUExecutionProvider"])
    i, o = sess.get_inputs()[0], sess.get_outputs()[0]
    print(f"onnx: input {i.name} {i.shape} -> output {o.name} {o.shape}")
    assert i.shape == [1, 3, 32, 96], i.shape
    assert o.shape == [1, len(charset), 1, 24], o.shape
    x = torch.rand(1, 3, 32, 96)
    with torch.no_grad():
        y_pt = model(x).numpy()
    y_ort = sess.run(None, {"input": x.numpy()})[0]
    diff = float(abs(y_pt - y_ort).max())
    print(f"torch vs ort max abs diff = {diff:.2e}")
    assert diff < 1e-3, diff
    print("export check OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
