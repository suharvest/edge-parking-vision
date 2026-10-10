# LICENSES.md

许可按三层分列：**代码许可 / 权重许可 / 训练与标注数据许可**分别核实、分别记录；代码许可不能代替权重与数据许可。

## 三层分列（模型，对应 `MODEL-CARD.json` 的 `licenses_three_layer`）

| 层 | 组件 | 许可 | 原文 | 商用 | 署名 |
|---|---|---|---|---|---|
| 代码 | YOLOX 代码（导出用，0.3.0） | Apache-2.0 | https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE | 是 | 保留 NOTICE |
| 权重 | `yolox_tiny.pth`（release 0.1.1rc0，sha256 `9de513de…aee8f0`，不入库） | 随 release 附 Apache-2.0 LICENSE 发布；授权声明独立于代码许可 | 同上 | 按用户 2026-09-26 决策继续用于测试与基准；不分发 .pth；不据此写「权重可商用」 | NOTICE 记录 release tag 与 sha256 |
| 训练数据 | COCO 2017 | 标注 CC BY 4.0；图片版权归各 Flickr 作者 | https://cocodataset.org/#termsofuse | 不写「COCO 可商用」；不分发 COCO 图片 | 署名 COCO Consortium |
| 训练与评测数据 | CCPD（Xu, Z. et al., ECCV 2018；CCPD2019 蓝牌 + CCPD2020 绿牌，车牌检测/识别训练与评测） | MIT，"Copyright (c) 2017 CCPD" | https://github.com/detectRecog/CCPD/blob/master/LICENSE | 是 | 见下文「CCPD 衍生内容」 |

用户 2026-09-26 决策：COCO 权重在 A 类（测试与基准用）继续使用；后续自训权重按 Apache-2.0 发布（车牌模型），自训不自动消除初始化权重与训练数据的许可，替换后仍逐文件列清单。

## CCPD 衍生内容（本仓库包含的部分）

来源：CCPD（Xu, Z. et al., "Towards End-to-End License Plate Detection and Recognition: A Large Dataset and Baseline", ECCV 2018），https://github.com/detectRecog/CCPD ，MIT License，Copyright (c) 2017 CCPD。

本仓库包含以下由 CCPD 派生的内容，用于模型 parity 对照与精度评测：

| 路径 | 内容 | 数量 |
|---|---|---|
| `evaluation/data/parity/plate_rec_cn/crops/` | 从 CCPD2020 绿牌 val 按标注四角点透视矫正到 96×32 的车牌裁切图（无增强） | 200 张 PNG |
| `evaluation/data/parity/plate_rec_cn/crops.jsonl` | 上述裁切图的车牌号标签 | 200 行 |
| `evaluation/data/plate_e2e/eval_set.jsonl`、`device_subset.jsonl` | 端到端评测清单：CCPD 原图文件名（相对 CCPD 根目录的路径）、车牌号标签、检测框 | 3400 / 504 行 |

说明：
- 裁切图含真实车牌，内容与 CCPD 公开发布的数据集一致，车牌号为公开数据集中的标注。
- CCPD 完整原图不在本仓库中；评测清单只引用路径，运行评测需自行从 CCPD 官方仓库获取并通过 `--ccpd-root`（或环境变量 `CCPD_ROOT`）指定。
- 再分发时保留 CCPD 的 MIT 许可声明。许可全文：`licenses/CCPD-MIT.txt`（同文本副本在 `evaluation/data/parity/plate_rec_cn/LICENSE-CCPD`），归属说明见 `NOTICE`。

## 本仓库与第三方组件

| 组件 | 版本 / 地址 | 许可 | 原文 | 商用 | 署名 |
|---|---|---|---|---|---|
| edge-parking-vision 自身代码 | 本仓库 | Apache-2.0 | 仓库 `LICENSE` | 是 | NOTICE |
| retail-vision（`vision_base` Python 包已内置于 `third_party/vision_base/`，commit `2af1ee5b`；`vb-runtime` 与基座镜像 `vision-base-<p>` 不在本仓库） | tag `vision-base-v<semver>`；镜像 digest 见 `platforms/base-images.lock.yaml` | Apache-2.0（仓库 `LICENSE` 首行） | 仓库 LICENSE | 是 | NOTICE 写明 tag 与各镜像 digest；镜像内第三方组件见 retail-vision `NOTICE` |
| retail-vision 拷入文件（`vb_analyzer_abi.h`、`third_party/vision_base/`） | 见 NOTICE | Apache-2.0 | 仓库 LICENSE | 是 | NOTICE |
| 内部原型脚本（无公开来源） | — | — | — | 仅作 `platforms/jetson/build_engine.sh` 参数约定、`platforms/host-mounts/*.yaml` 挂载项与训练环境脚本写法的设计参考；本仓库文件为自行编写，未拷入其文件 | — |
| rknn-toolkit2（仅转换机）/ librknnrt（宿主挂载） | 2.3.2；RK 板按宿主版本 | RKNN SDK License（Rockchip；https://github.com/airockchip/rknn-toolkit2/blob/master/LICENSE，2026-10-10 核对仓库 LICENSE 首行） | 同左 | 不随本仓库分发；用户自行从 Rockchip 获取 | — |
| TensorRT / CUDA（宿主挂载） | JetPack 6 | NVIDIA 许可（随 JetPack 安装） | 随 JetPack | 不随本仓库、不随镜像分发 | — |
| HailoRT（`libhailort` 宿主挂载）/ Dataflow Compiler（仅转换机） | 4.21.0 | HailoRT：libhailort、pyhailort、hailortcli 为 MIT，hailonet GStreamer 插件为 LGPL-2.1-or-later（https://github.com/hailo-ai/hailort README，2026-10-10 核对）；Dataflow Compiler 为 Hailo Developer Zone 许可 | 同左 | 不随本仓库分发；HEF 为本项目产物 | — |
| Grafana（演示看板示例） | — | AGPL-3.0 | https://github.com/grafana/grafana/blob/main/LICENSE | 作为独立未修改服务运行，不链接、不分发修改；写明「用户自行部署」 | — |
| Eclipse Mosquitto（R1124 broker，系统包；开发用 `eclipse-mosquitto:2` 镜像） | ≥2.0 | EPL-2.0 OR EDL-1.0（SPDX `EPL-2.0 OR BSD-3-Clause`） | https://github.com/eclipse-mosquitto/mosquitto/blob/master/LICENSE.txt（2026-09-26 原文核实） | 是（独立服务，不链接） | — |

## 测试素材

| 素材 | 许可 | 原文/来源 | 商用 | 处理 |
|---|---|---|---|---|
| dev 集（Wikimedia Commons 车辆视频） | 逐文件记录于 `evaluation/data/SOURCES.csv`（仅收 CC0/PD、CC BY 4.0、CC BY-SA 4.0） | Commons 文件页 | 按 SOURCES.csv 逐文件许可 | 逐文件写 SOURCES.csv；CC BY-SA 素材不进 gallery；原始视频不入库（sha256 记录于 `evaluation/data/video.sha256`） |
| test 集（自拍出数视频） | 自拍 | — | 是 | 不外发（`evaluation/data/private/`，gitignore）；发布前车牌打码 |
| AI City Challenge / UA-DETRAC 等数据集 | — | — | 禁止（许可不明或仅研究用途；UA-DETRAC 未核实前不用） | 不使用 |
