# 车牌识别端到端精度（2026-10-09）

检测 + 识别 + 运行时投票门控，CCPD 留出集。原始逐张结果在本目录 `*.json`。

## 被测模型

| 角色 | 源 ONNX | sha256 |
|---|---|---|
| 检测 | `models/plate_det_yolox_tiny_640.onnx` | `40be2053…e428` |
| 识别 | `models/plate_rec_cn_96x32.onnx`（`training/outputs/platerec_cn_full`） | `8a1e632f…a419` |

设备上测的是 2026-10-08 发布包里的文件（包 sha256 与 `published-20261008.json` 一致，manifest 中 `source_onnx_sha256` 与上表一致）：

| 包 | 检测 | 识别 |
|---|---|---|
| `plate_rk3588` (`1ea1864826a2`) | `plate_det_yolox_tiny_640_w8a8.rk3588.rknn` | `plate_rec_cn_96x32.rk3588.int8.rknn` |
| `plate_rk3576` (`dbd43c252024`) | `plate_det_yolox_tiny_640.rk3576.int8.rknn` | `plate_rec_cn_96x32.rk3576.int8.rknn` |
| `plate_jetson_orin_nano` (`a5e0e456c945`) | `plate_det_yolox_tiny_640_fp16.engine` | `plate_rec_cn_96x32_fp16.engine` |

## 评测集

`evaluation/data/plate_e2e/eval_set.jsonl`（3400 张，sha256 `e3e1fd71…2060`），由 `plate_e2e_build_set.py` 生成，seed 20261009。

训练数据（来自训练代码与 GPU x86 工作站上的训练产物）：

- 检测：CCPD2019 ccpd_base 随机 50 000 + 2 500（`data/coco/plate_det/{train,val}2017/b`）+ CCPD2020 green train/val。
- 识别：CCPD2019 除 np 外**全部子集**（`train_platerec.py` `TRAIN_MANIFESTS`，训练日志 `train=357701`）+ CCPD2020 green train；val = green val。

因此只有 CCPD2020 green test 对两个模型都未见过。评测集分两层：

| 层 | 子集 | 张数 | 说明 |
|---|---|---|---|
| `held_out_both` | CCPD2020 green test | 1000 | 检测、识别都未见过 |
| `held_out_det_rec_seen` | CCPD2019 base / db / fn / rotate / tilt / weather / challenge / blur | 各 300，共 2400 | 检测未见过（base 已剔除检测训练/验证用的 52 494 张）；**识别训练见过这些图** |

设备子集 `evaluation/data/plate_e2e/device_subset.jsonl`：从上表按顺序取 green 200 + 每个 CCPD2019 子集 38，共 504 张。图片在 GPU x86 工作站上用 OpenCV 解码一次，以无损 zlib 原始 RGB（`.rgbz`）分发到各设备，四个后端的输入像素一致。

## 流水线（与运行时一致的部分）

`plate_e2e_eval.py` 用 numpy 复刻 vb-runtime CPU 路径与 `platforms/cpu/presets/plate.json`：

- 检测：640 左上对齐 letterbox，pad 114，半像素双线性，BGR 0–255；YOLOX 网格解码 strides 8/16/32，score = obj×cls > 0.35，NMS 0.45。
- 识别：每帧按 score 取前 4 个框，框四边各扩 0.08，原图双线性采样到 96×32 RGB，÷255；CTC greedy，逐步 softmax，`min_char_conf` 与 `mean_conf` 按 `ctc_greedy.cpp` 定义。
- 门控：`min_char_conf ≥ 0.6` 且匹配蓝/绿牌正则才算一票；同一文本在 7 票窗口内达到 3 票即发布，轨迹结束时达到 2 票按 flush 发布。

不包含：预设 ROI 多边形（场地配置，未启用）；RKNN/TRT 设备上的裁切是本脚本的 numpy 实现，不是设备运行时的 RGA/CUDA 裁切。

指标：

- 检测召回：任一检测框与 GT 框 IoU ≥ 0.5。
- 整牌准确率：与 GT 匹配的检测框读出的文本与标签整串相同；漏检计错。
- 字符准确率：1 − 编辑距离 / 标签长度（下限 0），漏检计 0。
- 门控读数 / 门控精度：单帧读数通过 `min_conf + 正则` 的比例，以及其中整串正确的比例。
- 发布 / 发布精度：投票模拟。静态图没有连续帧，每张图生成 7 帧（原图 + 6 帧随机仿射：缩放 0.95–1.05、旋转 ±2°、平移 ±2%、亮度 ×0.85–1.15，GT 框随仿射变换后用于跟随），按 7 帧顺序投票。这是对视频连续帧的近似，真实视频中相邻帧差异与此不同。

## 结果一：ONNX FP32，GPU x86 工作站，3400 张

| 子集 | 张数 | 检测召回 | 整牌准确率 | 字符准确率 | 门控读数 | 门控精度 | 发布 | 发布精度 | 正确发布 |
|---|---|---|---|---|---|---|---|---|---|
| **green test（双留出）** | 1000 | 99.8% | **58.1%** | 83.2% | 59.6% | 87.2% | 67.5% | **86.1%** | 58.1% |
| base | 300 | 100.0% | 89.7% | 97.8% | 81.7% | 96.3% | 91.0% | 95.2% | 86.7% |
| db | 300 | 96.0% | 49.0% | 81.4% | 53.0% | 78.6% | 60.0% | 80.0% | 48.0% |
| fn | 300 | 97.3% | 41.3% | 73.3% | 43.7% | 77.9% | 51.3% | 79.9% | 41.0% |
| rotate | 300 | 99.3% | 25.7% | 64.4% | 20.3% | 82.0% | 27.0% | 80.2% | 21.7% |
| tilt | 300 | 100.0% | 14.0% | 61.8% | 12.7% | 60.5% | 17.3% | 67.3% | 11.7% |
| weather | 300 | 100.0% | 85.0% | 97.1% | 83.0% | 92.8% | 89.0% | 92.1% | 82.0% |
| challenge | 300 | 99.3% | 62.3% | 89.5% | 66.7% | 84.0% | 72.7% | 83.5% | 60.7% |
| blur | 300 | 98.0% | 58.3% | 89.2% | 66.3% | 78.4% | 76.3% | 79.5% | 60.7% |
| CCPD2019 合计（识别见过） | 2400 | 98.8% | 53.2% | 81.8% | 53.4% | 85.1% | 60.6% | 85.1% | 51.5% |
| 全部 | 3400 | 99.1% | 54.6% | 82.2% | 55.2% | 85.8% | 62.6% | 85.4% | 53.5% |

- 发布列中经 flush（2 票）发布的：green 51 张，全部 146 张。
- 非 GT 检测框：平均 0.008 个/张；其中单帧通过门控的读数共 9 条（3400 张）。

原始数据：`onnx_fp32_x86_full.json`。

## 结果二：裁切方式对识别的影响（ONNX FP32，只跑识别）

同一批 3400 张，用 GT 框代替检测框：

| 子集 | 四角点透视矫正（训练时的裁切） | GT 轴对齐框 + 0.08 扩边（运行时的裁切） |
|---|---|---|
| green test | 89.5% | 58.6% |
| base | 100.0% | 87.3% |
| db | 81.0% | 37.7% |
| fn | 90.7% | 43.0% |
| rotate | 95.3% | 17.7% |
| tilt | 95.3% | 8.3% |
| weather | 98.7% | 82.0% |
| challenge | 87.0% | 60.0% |
| blur | 86.0% | 56.0% |
| 全部 | 91.1% | 51.8% |

已发布的识别模型用四角点矫正图训练，运行时只有检测轴对齐框、没有角点。green test 上两种裁切相差 30.9 个百分点；端到端 58.1% 与「GT 轴对齐框」58.6% 接近，检测带来的损失约 0.5 个百分点。原始数据：`rec_crop_diag_onnx_fp32.json`。

## 结果三：设备量化模型，504 张设备子集（单帧，不含投票模拟）

| 后端 | 设备 | 张数 | 检测召回 | 整牌准确率 | 字符准确率 | 门控读数 | 门控精度 | 与 ONNX 读出文本一致 |
|---|---|---|---|---|---|---|---|---|
| ONNX FP32（参照） | GPU x86 工作站| 504 | 98.4% | 52.8% | 79.8% | 52.8% | 86.5% | — |
| RKNN INT8 | RK3588 | 504 | 98.8% | 51.2% | 79.6% | 52.6% | 84.2% | 297/504 |
| RKNN INT8 | RK3576 | 504 | 98.8% | 51.2% | 79.6% | 52.6% | 84.2% | 297/504 |
| TensorRT FP16 | Jetson Orin Nano | 504 | 98.4% | 53.2% | 80.0% | 52.8% | 86.8% | 465/504 |

其中 green test 200 张（双留出）：

| 后端 | 整牌准确率 | 字符准确率 | 门控读数 | 门控精度 |
|---|---|---|---|---|
| ONNX FP32 | 53.5% | 78.9% | 51.0% | 91.2% |
| RK3588 RKNN INT8 | 51.0% | 79.0% | 51.5% | 86.4% |
| RK3576 RKNN INT8 | 51.0% | 79.0% | 51.5% | 86.4% |
| Jetson TRT FP16 | 54.5% | 79.1% | 52.5% | 90.5% |

- RK3588 与 RK3576 两个包用同一量化配方与校准集，504 张读出文本逐张相同，`min_char_conf` 差值中位数 4e-5、最大 0.025。
- RKNN INT8 与 ONNX 的读出文本只有 59% 一致，但整牌准确率差 1.6 个百分点：量化改变的多为本来就读错的样本。
- 设备运行时间（504 张，含 numpy 预处理）：RK3588 41.9 s，RK3576 54.3 s，Jetson Orin Nano 30.5 s（分 4 批，每批 126 张）。

原始数据：`onnx_fp32_x86_device_subset.json`、`rknn_int8_rk3588.json`、`rknn_int8_rk3576.json`、`trt_fp16_orin_nano.json`。

## 复现

GPU x86 工作站（依赖：onnxruntime、numpy、opencv-python-headless）：

```bash
python3 evaluation/plate_e2e_build_set.py --out evaluation/data/plate_e2e/eval_set.jsonl
python  evaluation/plate_e2e_eval.py --set evaluation/data/plate_e2e/eval_set.jsonl \
  --det models/plate_det_yolox_tiny_640.onnx --rec models/plate_rec_cn_96x32.onnx \
  --vote-frames 7 --out evaluation/results/plate_e2e_20261009/onnx_fp32_x86_full.json
python  evaluation/plate_rec_crop_diag.py --set evaluation/data/plate_e2e/eval_set.jsonl \
  --det models/plate_det_yolox_tiny_640.onnx --rec models/plate_rec_cn_96x32.onnx \
  --out evaluation/results/plate_e2e_20261009/rec_crop_diag_onnx_fp32.json
python  evaluation/plate_e2e_eval.py --set evaluation/data/plate_e2e/device_subset.jsonl \
  --export-rgbz <dir> --det x --rec x
```

设备（宿主机 python3；RK 用 rknn-toolkit-lite2 2.3.0，Jetson 用 TensorRT 10.3 + cuda-python）：

```bash
python3 plate_e2e_eval.py --backend rknn --set device_subset.jsonl --path-prefix <rgbz dir> \
  --det plate_det_yolox_tiny_640_w8a8.rk3588.rknn --rec plate_rec_cn_96x32.rk3588.int8.rknn --out rk3588.json
python3 plate_e2e_eval.py --backend trt --set device_subset.jsonl --path-prefix <rgbz dir> \
  --det plate_det_yolox_tiny_640_fp16.engine --rec plate_rec_cn_96x32_fp16.engine --out trt.json
```
