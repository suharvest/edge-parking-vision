#!/usr/bin/env bash
# YOLOX-Tiny 车牌检测训练（NRD-3 P5）。后台起，nohup + 日志。
set -euo pipefail

ROOT=${ROOT:-$HOME/edge-parking-vision}
cd "$ROOT"
# shellcheck disable=SC1091
source .venv/bin/activate
export PYTHONPATH="$ROOT/third_party/YOLOX:${PYTHONPATH:-}"
export PLATE_DET_DATA_DIR="${PLATE_DET_DATA_DIR:-$ROOT/data/coco/plate_det}"

BATCH=${BATCH:-64}
CKPT=${CKPT:-$ROOT/models/yolox_tiny.pth}
mkdir -p "$ROOT/training/outputs"

# 不加 -o（occupy_mem）：GB10 的 nvidia-smi 不报显存用量（"Not Supported"），
# YOLOX 的 occupy_mem 会在 int('[N/A]') 上崩掉。
ARGS=(-f training/exp/yolox_tiny_plate.py -d 1 -b "$BATCH" --fp16
      -expn yolox_tiny_plate)
if [[ -f "$CKPT" ]]; then
  ARGS+=(-c "$CKPT")
  echo "using COCO-pretrained checkpoint: $CKPT"
else
  echo "WARNING: $CKPT missing, training from scratch" >&2
fi

echo "== start $(date -Is) batch=$BATCH =="
exec python third_party/YOLOX/tools/train.py "${ARGS[@]}"
