#!/usr/bin/env bash
# 导出车牌检测两份 ONNX（640 与 416，同一权重）+ 形状检查（NRD-3 P5）。
set -euo pipefail

ROOT=${ROOT:-$HOME/edge-parking-vision}
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONPATH="$ROOT/third_party/YOLOX:${PYTHONPATH:-}"

CKPT=${CKPT:-$ROOT/training/outputs/yolox_tiny_plate/best_ckpt.pth}
mkdir -p "$ROOT/models"

for S in 640 416; do
  python training/export_plate_det.py \
    -f training/exp/yolox_tiny_plate.py \
    -c "$CKPT" \
    -o "$ROOT/models/plate_det_yolox_tiny_${S}.onnx" \
    --opset 11 --size "$S"
done

python - <<'PY'
import onnxruntime as ort
for s in (640, 416):
    sess = ort.InferenceSession(f"models/plate_det_yolox_tiny_{s}.onnx",
                                providers=["CPUExecutionProvider"])
    i, o = sess.get_inputs()[0], sess.get_outputs()[0]
    print(f"plate_det{s}: input {i.name} {i.shape} -> output {o.name} {o.shape}")
    assert i.shape == [1, 3, s, s], i.shape
    assert o.shape[0] == 1 and o.shape[2] == 6, o.shape  # 1 类: 4 + 1(obj) + 1(cls)
print("onnx shape check OK")
PY
echo "export done"
