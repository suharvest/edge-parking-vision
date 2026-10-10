# parity 参考帧（vehicle416，CPU 基座）

**占位目录。** 真值数据需在 GPU x86 工作站上由 CPU 基座生成：

```bash
# GPU x86 工作站，基座 CPU parity 模式可用后：
vb-runtime --parity tests/fixtures/parity/vehicle416.cpu/ --config <runtime.json>   # 输入 evaluation/data/parity/vehicle416/（50 帧）
# 比对：
uv run python <retail-vision>/tools/vb_parity_compare.py \
    --ref tests/fixtures/parity/vehicle416.cpu/ --got <out_dir> --iou 0.9 --count-diff 1
```

当前状态：retail-vision 的 `tools/vb_parity_compare.py` 与 `vb-runtime --parity` 尚未提供，
输入形式待确认。数据到位后本目录放逐帧 JSON。
