#!/usr/bin/env bash
# 在 GPU x86 工作站上准备车牌检测 YOLOX 训练环境（NRD-3 P5）。幂等，可重复执行。
# 写法沿用 edge-inspection-assembly/training/setup_train_env.sh（NRD-2 §S5 同一训练链）。
set -euo pipefail

ROOT=${ROOT:-$HOME/edge-parking-vision}
cd "$ROOT"

echo "== mirrors =="
echo "HF_ENDPOINT=${HF_ENDPOINT:-<unset>}"
echo "UV_INDEX_URL=${UV_INDEX_URL:-<unset>}"
[[ -n "${UV_INDEX_URL:-}" ]] || { echo "UV_INDEX_URL unset; fix mirrors first" >&2; exit 1; }

# YOLOX 上游最后一次发版在 2022，torch 2.x 下有已知小问题，用固定 tag
YOLOX_REF=${YOLOX_REF:-0.3.0}
if [[ ! -d third_party/YOLOX ]]; then
  mkdir -p third_party
  git clone --depth 1 --branch "$YOLOX_REF" \
    https://github.com/Megvii-BaseDetection/YOLOX.git third_party/YOLOX
fi

echo "== uv venv =="
# 幂等：venv 已存在时 `uv venv` 会直接报错退出，不要重建（重建会丢掉已装的 torch）
[[ -d .venv ]] || uv venv --python 3.11 .venv
# shellcheck disable=SC1091
source .venv/bin/activate

uv pip install --index-url "$UV_INDEX_URL" \
  numpy opencv-python-headless loguru tqdm tabulate psutil tensorboard \
  pycocotools ninja thop onnx onnxruntime onnx-simplifier pyyaml
uv pip install torch torchvision \
  --index-url "${TORCH_INDEX_URL:-https://pypi.tuna.tsinghua.edu.cn/simple}" || \
  uv pip install torch torchvision

python - <<'PY'
import torch
print("torch", torch.__version__, "cuda", torch.version.cuda,
      "available", torch.cuda.is_available())
if torch.cuda.is_available():
    print("device", torch.cuda.get_device_name(0),
          "capability", torch.cuda.get_device_capability(0))
PY

# YOLOX 本体不装，只用 PYTHONPATH（build isolation 下 setup.py 看不到 torch 会断言失败；
# YOLOX 0.3.0 没有 C 扩展，PYTHONPATH 导入够用）
PYTHONPATH="$ROOT/third_party/YOLOX" python -c "import yolox; print('yolox', yolox.__version__)"
echo "setup done"
