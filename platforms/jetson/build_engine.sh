#!/usr/bin/env bash
set -euo pipefail

# Build YOLOX vehicle engines on the target Jetson.  The ONNX file remains a
# host input and the engine is intentionally never copied into this repo.
# Usage is compatible with the fall-detection helper while selecting the
# static shape from the registered vehicle model.
if [[ $# -lt 2 ]]; then
  echo "Usage: $0 MODEL.onnx OUTPUT.engine [trtexec args...]" >&2
  echo "       MODEL_KEY=vehicle416|vehicle640 may select the registered shape" >&2
  exit 2
fi

ONNX_PATH=$1
ENGINE_PATH=$2
shift 2
TRTEXEC=${TRTEXEC:-/usr/src/tensorrt/bin/trtexec}

[[ -f "$ONNX_PATH" ]] || { echo "ONNX model not found: $ONNX_PATH" >&2; exit 1; }
[[ -x "$TRTEXEC" ]] || { echo "trtexec not executable: $TRTEXEC" >&2; exit 1; }
mkdir -p "$(dirname "$ENGINE_PATH")"

case "${MODEL_KEY:-$(basename "$ONNX_PATH")}" in
  vehicle416|*yolox_tiny_coco_416.onnx) INPUT_SIZE=416; MODEL_KEY=vehicle416 ;;
  vehicle640|*yolox_tiny_coco_640.onnx) INPUT_SIZE=640; MODEL_KEY=vehicle640 ;;
  *)
    echo "MODEL_KEY must be vehicle416/vehicle640 or a registered vehicle ONNX basename" >&2
    exit 2
    ;;
esac

ONNX_SHA256=$(sha256sum "$ONNX_PATH" | awk '{print $1}')
echo "== TensorRT builder =="
"$TRTEXEC" --version || true  # TensorRT 10.3 may return non-zero here
echo "Model:    $MODEL_KEY ($ONNX_PATH)"
echo "ONNX sha: $ONNX_SHA256"
echo "Engine:   $ENGINE_PATH"
echo "Target:   Jetson Orin SM87 / host TensorRT 10.3"

TRT_INPUT_NAME=${TRT_INPUT_NAME:-images}
TRT_STATIC_SHAPE=${TRT_STATIC_SHAPE:-true}
if [[ "$TRT_STATIC_SHAPE" != true && "$TRT_STATIC_SHAPE" != false ]]; then
  echo "TRT_STATIC_SHAPE must be true or false" >&2
  exit 2
fi

trtexec_args=(
  "--onnx=$ONNX_PATH"
  "--saveEngine=$ENGINE_PATH"
  "--fp16"
  "--skipInference"
  "--builderOptimizationLevel=3"
  "--timingCacheFile=${ENGINE_PATH}.timing.cache"
)
has_explicit_shapes=false
for argument in "$@"; do
  case "$argument" in
    --minShapes=*|--optShapes=*|--maxShapes=*|--shapes=*|--explicitBatch)
      has_explicit_shapes=true
      ;;
  esac
done
if [[ "$TRT_STATIC_SHAPE" == false && "$has_explicit_shapes" == false ]]; then
  trtexec_args+=(
    "--minShapes=${TRT_INPUT_NAME}:1x3x${INPUT_SIZE}x${INPUT_SIZE}"
    "--optShapes=${TRT_INPUT_NAME}:1x3x${INPUT_SIZE}x${INPUT_SIZE}"
    "--maxShapes=${TRT_INPUT_NAME}:1x3x${INPUT_SIZE}x${INPUT_SIZE}"
  )
fi
trtexec_args+=("$@")
"$TRTEXEC" "${trtexec_args[@]}"
test -s "$ENGINE_PATH"

# The manifest is the portable evidence for a later device run.  It records
# the input model and builder context without pretending the engine is portable
# across Jetson/TRT versions.
python3 - "$ENGINE_PATH.manifest.json" "$MODEL_KEY" "$ONNX_PATH" "$ONNX_SHA256" "$TRTEXEC" "${trtexec_args[@]}" <<'PY'
import json, os, platform, subprocess, sys
out, key, onnx, sha, trtexec, *build_args = sys.argv[1:]
try:
    version = subprocess.run([trtexec, "--version"], text=True,
                             capture_output=True, check=False).stdout.strip()
except OSError as exc:
    version = f"unavailable: {exc}"
with open(os.path.abspath(sys.argv[1]).replace(".manifest.json", ""), "rb") as f:
    import hashlib
    engine_sha = hashlib.sha256(f.read()).hexdigest()
data = {
    "schema": "edge-parking.jetson-engine/1",
    "model_key": key,
    "onnx_path": os.path.abspath(onnx),
    "onnx_sha256": sha,
    "engine_sha256": engine_sha,
    "build_argv": [os.path.abspath(trtexec), *build_args],
    "trtexec": os.path.abspath(trtexec),
    "trt_version_probe": version,
    "platform": platform.machine(),
    "sm": "87",
    "precision": "fp16",
    "portable": False,
}
with open(out, "w", encoding="utf-8") as f:
    json.dump(data, f, indent=2, sort_keys=True)
    f.write("\n")
PY
echo "Engine written: $ENGINE_PATH"
echo "Manifest written: $ENGINE_PATH.manifest.json"
