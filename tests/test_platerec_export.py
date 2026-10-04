"""PlateRecNet 导出 ONNX 检查（NRD-3 §6.5 / §10，P6）。

- 节点类型只含 Conv/Relu/MaxPool/AveragePool/Concat/Add，无
  Softmax/LogSoftmax/Squeeze/AdaptiveAvgPool；
- 形状 1×3×32×96 → 1×C×24；
- PyTorch vs ONNXRuntime 同输入最大差 < 1e-3。

ONNX 不存在或缺 torch/onnxruntime 时 skip，保证仓库默认 pytest 仍绿。
"""
from __future__ import annotations

from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
ort = pytest.importorskip("onnxruntime")

sys_path = str(Path(__file__).resolve().parents[1] / "training")
import sys  # noqa: E402

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

ONNX = Path(__file__).resolve().parents[1] / "models" / "plate_rec_cn_96x32.onnx"

# ONNX 被 gitignore（新克隆不存在）：模块级 skip，保证仓库默认 pytest 仍绿
pytestmark = pytest.mark.skipif(not ONNX.exists(), reason="plate_rec_cn_96x32.onnx 未导出")

ALLOWED = {"Conv", "Relu", "MaxPool", "AveragePool", "Concat", "Add"}
FORBIDDEN = {"Softmax", "LogSoftmax", "Squeeze", "AdaptiveAvgPool"}


def _load_onnx():
    import onnx

    return onnx.load(str(ONNX))


def test_onnx_exists():
    assert ONNX.exists(), f"missing {ONNX}"


def test_node_types():
    m = _load_onnx()
    types = {n.op_type for n in m.graph.node}
    unexpected = types - ALLOWED
    assert not unexpected, f"unexpected node types: {sorted(unexpected)}"
    bad = types & FORBIDDEN
    assert not bad, f"forbidden node types: {sorted(bad)}"


def test_io_shapes():
    from platerec import cn_charset

    c = len(cn_charset())
    sess = ort.InferenceSession(str(ONNX), providers=["CPUExecutionProvider"])
    assert sess.get_inputs()[0].shape == [1, 3, 32, 96]
    # 规格 “1×C×24”：图内无 Squeeze/Reshape，H 维为 1（(8,1) 卷积坍缩）
    assert sess.get_outputs()[0].shape == [1, c, 1, 24]


def test_pytorch_onnx_parity():
    from export_platerec import fold_model
    from platerec import PlateRecNet, cn_charset

    state_path = ONNX.with_suffix(".state.pt")  # 导出时保存的折 BN 后权重
    if not state_path.exists():
        pytest.skip(f"missing {state_path}")
    net = fold_model(PlateRecNet(len(cn_charset())).eval())  # 与导出一致的折 BN 结构
    net.load_state_dict(torch.load(state_path, map_location="cpu", weights_only=True))
    x = torch.rand(1, 3, 32, 96)
    with torch.no_grad():
        y_pt = net(x).numpy()
    sess = ort.InferenceSession(str(ONNX), providers=["CPUExecutionProvider"])
    y_ort = sess.run(None, {"input": x.numpy()})[0]
    assert y_ort.shape == (1, len(cn_charset()), 1, 24)
    assert abs(y_pt - y_ort).max() < 1e-3, abs(y_pt - y_ort).max()
