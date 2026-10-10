# plate_rec_cn parity 夹具

本目录包含 200 张车牌裁切图（`crops/`）与标签（`crops.jsonl`，`plate` 字段）。

- 来源：CCPD2020 绿牌 val 集（Xu, Z. et al., ECCV 2018，https://github.com/detectRecog/CCPD ，MIT License，Copyright (c) 2017 CCPD），均匀抽样 200 张。
- 生成：`training/make_platerec_parity.py` 按标注四角点透视矫正到 96×32，无增强。
- 用途：车牌识别模型在各后端（ONNX / RKNN / TensorRT 等）之间的 parity 对照。
- 图片含真实车牌，与 CCPD 公开发布的数据集内容一致；标签为数据集标注。
- CCPD 原图不在本仓库中，本目录只有矫正后的裁切图。
- 许可全文：同目录 `LICENSE-CCPD`；另见仓库根目录 `LICENSES.md`「CCPD 衍生内容」与 `NOTICE`。
