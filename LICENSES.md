# LICENSES.md

许可按 BASE-1 §7.1 的三层分列：**代码许可 / 权重许可 / 训练与标注数据许可**分别核实、分别记录，「可商用」不能从仓库代码许可单独推出。本文件为 NRD-2 §S7 初稿的实现，随交付逐项更新。

## 三层分列（模型，对应 `MODEL-CARD.json` 的 `licenses_three_layer`）

| 层 | 组件 | 许可 | 原文 | 商用 | 署名 |
|---|---|---|---|---|---|
| 代码 | YOLOX 代码（导出用，0.3.0） | Apache-2.0 | https://github.com/Megvii-BaseDetection/YOLOX/blob/main/LICENSE | 是 | 保留 NOTICE |
| 权重 | `yolox_tiny.pth`（release 0.1.1rc0，sha256 `9de513de…aee8f0`，不入库） | 随 release 附 Apache-2.0 LICENSE 发布；授权声明独立于代码许可（BASE-1 §7.1） | 同上 | 按用户 2026-09-26 决策继续用于测试与基准；不分发 .pth；不据此写「权重可商用」 | NOTICE 记录 release tag 与 sha256 |
| 训练数据 | COCO 2017 | 标注 CC BY 4.0；图片版权归各 Flickr 作者 | https://cocodataset.org/#termsofuse（原文见 URD-2 §7.3） | 不写「COCO 可商用」；不分发 COCO 图片 | 署名 COCO Consortium |
| 训练数据 | CCPD（CCPD2019 蓝牌 + CCPD2020 绿牌，车牌检测/识别训练） | MIT | https://github.com/detectRecog/CCPD/blob/master/LICENSE（"MIT License, Copyright (c) 2017 CCPD"；README："This dataset is open-source under MIT license."，2026-09-25 核实，见 NRD-3 §7） | 是 | 图片含真实车牌，只用于训练，不进 gallery、不随交付分发；parity/校准帧不取 CCPD（用 dev 集） |

用户 2026-09-26 决策：COCO 权重在 A 类（测试与基准用）继续使用；后续自训权重按 Apache-2.0 发布（NRD-3 车牌模型），自训不自动消除初始化权重与训练数据的许可，替换后仍逐文件列清单。

## 本仓库与第三方组件

| 组件 | 版本 / 地址 | 许可 | 原文 | 商用 | 署名 |
|---|---|---|---|---|---|
| edge-parking-vision 自身代码 | 本仓库 | Apache-2.0 | 仓库 `LICENSE` | 是 | NOTICE |
| retail-vision（`vision_base` Python 包 + `vb-runtime` + 基座镜像 `vision-base-<p>`） | tag `vision-base-v<semver>`；镜像 digest 见 §S11.2 锁文件 | Apache-2.0（仓库 `LICENSE` 首行，BASE-1 §1） | 仓库 LICENSE | 是 | NOTICE 写明 tag 与各镜像 digest；镜像内第三方组件见 retail-vision `NOTICE` |
| retail-vision 拷入文件（`vb_analyzer_abi.h`、`host-mounts/*.yaml`） | 同一 tag | Apache-2.0 | 仓库 LICENSE | 是 | NOTICE |
| fall-detection 拷入文件（`build_engine.sh`） | `origin/main` `5af128d` | Apache-2.0（2026-09-26 读仓库 `LICENSE` 首行） | 仓库 LICENSE | 是 | NOTICE |
| edge-inspection-assembly（转换脚本写法参考、MODEL-CARD/SHA256SUMS 格式） | commit `5da8dde` | 需核实该仓库 LICENSE 文件 | — | — | 未核实前不整文件拷贝（本任务仅参考文件格式，未拷贝文件内容） |
| edge-security-kit | HEAD `761edad` | 无 LICENSE 文件 | — | 不拷代码，只借设计 | 不入 NOTICE |
| rknn-toolkit2（仅转换机）/ librknnrt（宿主挂载，基座 C++ 直调） | 2.3.2（Pro）；RK 板按宿主版本 | Rockchip 许可 | 需核实 | 运行库不随镜像分发 | — |
| TensorRT / CUDA（宿主挂载，不分发） | JetPack 6 | NVIDIA 许可 | 需核实 | 不随镜像分发 | — |
| HailoRT（`libhailort` 宿主挂载）/ Dataflow Compiler（仅转换机） | 4.21.0 | Hailo 许可（Developer Zone click-through 分发） | 需核实 | 不随镜像、不随上游包分发；HEF 为我方产物 | — |
| TPU-MLIR（转换机容器 sophgo/tpuc_dev:v3.1） | — | 需核实（上游标 BSD-2? 未打开原文） | — | — | — |
| Grafana（演示看板示例） | — | AGPL-3.0 | https://github.com/grafana/grafana/blob/main/LICENSE | 作为独立未修改服务运行，不链接、不分发修改；写明「用户自行部署」 | — |
| Eclipse Mosquitto（R1124 broker，系统包；开发用 `eclipse-mosquitto:2` 镜像） | ≥2.0 | EPL-2.0 OR EDL-1.0（SPDX `EPL-2.0 OR BSD-3-Clause`） | https://github.com/eclipse-mosquitto/mosquitto/blob/master/LICENSE.txt（2026-09-26 原文核实） | 是（独立服务，不链接） | — |

## 测试素材（§S6）

| 素材 | 许可 | 原文/来源 | 商用 | 处理 |
|---|---|---|---|---|
| dev 集（Wikimedia Commons 车辆视频） | 逐文件记录于 `evaluation/data/SOURCES.csv`（仅收 CC0/PD、CC BY 4.0、CC BY-SA 4.0） | Commons 文件页 | 按 §S6 | 逐文件写 SOURCES.csv；CC BY-SA 素材不进 gallery；原始视频不入库（sha256 记录于 `evaluation/data/video.sha256`） |
| test 集（自拍出数视频） | 自拍 | — | 是 | 不外发（`evaluation/data/private/`，gitignore）；发布前车牌打码 |
| AI City Challenge / UA-DETRAC 等数据集 | — | — | 禁止（许可不明或仅研究用途；UA-DETRAC 未核实前不用） | 不使用 |
