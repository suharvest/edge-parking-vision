# RKNN conversion

`convert_rknn.py` builds one fixed-shape vehicle ONNX model for an explicitly
selected `rk3576` or `rk3588` target. It uses RKNN-Toolkit2 2.3.2 and writes
all derived calibration images, datasets, manifests, and RKNN binaries to the
caller-provided output directory. The repository does not contain generated
RKNN binaries.

The source model declares BGR byte pixels, mean `[0,0,0]`, standard deviation
`[1,1,1]`, and top-left letterbox padding 114. The script applies that
preprocessing to the calibration JPEGs before passing lossless PNGs to the
toolkit. PNG files carry standard RGB wire order; the script sets the SDK's
documented `quant_img_RGB2BGR=True` option so the toolkit calibration tensor is
BGR. The 64 images are calibration input, not an accuracy holdout.

Example (use an existing local Toolkit2 environment; do not install packages):

```bash
uv run --no-project --no-sync --python /home/harvest/sv-rknn-2320/.venv/bin/python \
  platforms/rk/convert_rknn.py \
  --onnx models/yolox_tiny_coco_416.onnx \
  --calib-list evaluation/data/calib/vehicle_416.txt \
  --size 416 --target rk3576 \
  --out-dir /home/harvest/nrd-parking-run/artifacts-rknn-20261005/vehicle416-rk3576
```

The converter validates the selected vehicle variant against MODEL-CARD.json,
including its model SHA and input/output shapes, before deriving calibration
files or calling the SDK. The manifest records the installed SDK metadata,
color conversion wiring, model and calibration hashes, target, quantization
settings, and output hash. Native input type, layout, quantization values, and
output fields remain pending until queried with the target runtime; this build
is not device verification.
