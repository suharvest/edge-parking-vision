#!/usr/bin/env bash
# 导出 PlateRecNet CN ONNX + 形状检查（NRD-3 §6.5 / P6）。DE 本期不做（§0.1 / D2）。
set -euo pipefail

ROOT=${ROOT:-$HOME/edge-parking-vision}
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True

CKPT=${CKPT:-$ROOT/training/outputs/platerec_cn/best.pth}
mkdir -p "$ROOT/models"

python training/export_platerec.py \
  --ckpt "$CKPT" \
  --out "$ROOT/models/plate_rec_cn_96x32.onnx"

echo "export done (plate_rec_cn_96x32.onnx)"
