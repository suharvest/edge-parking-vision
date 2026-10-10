# parity 参考帧（vehicle416，CPU 基座）

**占位目录。** 真值数据需在 GPU x86 工作站上由 CPU 基座生成（规格 §8.1 C4 行、§S9 parity 行）：

```bash
# GPU x86 工作站，基座 M2.1 完成后：
vb-runtime --parity tests/fixtures/parity/vehicle416.cpu/ --config <runtime.json>   # 输入 evaluation/data/parity/vehicle416/（50 帧）
# 比对：
uv run python ~/project/retail-vision/tools/vb_parity_compare.py \
    --ref tests/fixtures/parity/vehicle416.cpu/ --got <out_dir> --iou 0.9 --count-diff 1
```

阻塞原因（2026-09-27，C4 执行时）：retail-vision `tools/vb_parity_compare.py`
尚不存在（BASE-1 M2.1 离线部分未完成），`vb-runtime --parity` 亦未交付；
输入形式在规格中标注「需核实」。数据到位后本目录落逐帧 JSON。
