# edge-parking-vision

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.10+-green.svg" alt="Python">
  <img src="https://img.shields.io/badge/Runtime-Stdlib_Only-success.svg" alt="Runtime">
  <img src="https://img.shields.io/badge/Tests-480+_passing-brightgreen.svg" alt="Tests">
  <img src="https://img.shields.io/badge/Platforms-Jetson_%7C_RK3588_%7C_RK3576_%7C_Hailo--8_%7C_SG2002_%7C_CPU-orange.svg" alt="Platforms">
  <img src="https://img.shields.io/badge/Presets-Counting_%7C_Slots_%7C_Plate_Recognition-blueviolet.svg" alt="Presets">
</p>

<p align="center">
  <strong>生产级边缘端智能停车与车牌识别视觉套件</strong><br>
  出入口通行计数 · 远景车位占用检测 · 车牌识别与道闸门禁联动<br>
  <em>100% 边缘端离线推理 · 零云端依赖 · 生产运行时标准库零第三方 Python 依赖</em>
</p>

<p align="center">
  <a href="README.md">English</a> | <a href="README_zh.md">简体中文</a>
</p>

---

## 📖 项目简介

**edge-parking-vision** 是一套面向智能停车场、出入口道闸与智慧园区的轻量级边缘端视觉 AI 应用套件。系统针对异构边缘算力芯片深度优化，支持三类即开即用的业务预设（Presets）：

1. **出入口通行计数 (`counting`)**：双向虚拟警戒线车辆跟踪与进出计数，维护车场动态余位，按频控发布 MQTT 信封。
2. **远景车位占用检测 (`slots`)**：多路/单路广角相机车位多边形 ROI 占用状态监测，带时间防抖滤波与浏览器可视化多边形标定器。
3. **车牌识别与道闸门禁联动 (`plate`)**：端侧两阶段高精度中式车牌识别（YOLOX-Tiny + PlateRecNet CTC），多帧滑窗置信度投票，本地白名单放行与 HTTP/继电器/ONVIF 道闸控制。

### 核心设计原则

- **100% 边缘就地计算，离线无网可用**：视频解码、目标检测跟踪、识别推理、状态机与事件组装全程在边缘设备内部完成，无公网依赖，无视频上云延迟与带宽开销。
- **生产核心仅依赖 Python 标准库**：应用业务层（`core-py/core_parking`）**零第三方 pip 依赖**，仅使用 Python 3.10+ 标准库实现，冷启动速度极快，彻底杜绝包版本冲突，极易打包嵌入受限嵌入式环境。
- **异构硬件算力适配**：原生支持 **NVIDIA Jetson**（TensorRT FP16）、**Rockchip NPU**（RK3588 / RK3576 RKNN W8A8）、**树莓派 5 + Hailo-8**（HailoRT INT8）、**Seeed reCamera-2002**（Sophgo CV181x/SG2002 RISC-V）以及通用 x86/ARM CPU。
- **掉线零丢失本地 Spool 队列**：单调递增持久化序列号（`seq`）与本地磁盘缓冲，网络或 MQTT Broker 中断时安全落盘，重连后按序重放。
- **契约驱动（Contract-Driven）**：所有配置项、MQTT 信封与 REST 接口均由严格版本化的 JSON Schema 规范定义与校验（`contracts/`）。
- **浏览器实时可视化标定**：内置 HTTP 服务直接提供免安装的 Web 编辑器，在实时相机画面上拖拽警戒线（`/counting/editor`）或绘制车位多边形（`/slots/editor`），实时下发热重载。

---

## 🏗️ 整体架构

```text
                       ┌────────────────────────┐
                       │   RTSP Camera Stream   │
                       └───────────┬────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Edge Native Runtime (vb-runtime)                   │
│   硬件视频解码 (NVDEC / MPP / CVI) + 目标追踪器 + NPU 硬件加速推理        │
│   YOLOX-Tiny (416/640) · PlateRecNet CNN-CTC (96x32) · Multi-tracker    │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ Frame Detections & Snapshots
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                 core_parking Application Layer (Python 标准库实现)      │
│                                                                         │
│  ┌──────────────────────┐ ┌──────────────────────┐ ┌──────────────────┐ │
│  │   Counting Hooks     │ │      Slots Hooks     │ │   Plate & Gate   │ │
│  │  警戒线穿越 (in/out) │ │  多边形车位 ROI 判定 │ │  车牌检测与透视  │ │
│  │  动态容量与空位更新  │ │  时间滞后防抖滤波    │ │  PlateRecNet CTC │ │
│  │  状态流控与回放      │ │  逐车位占用率计算    │ │  白名单与继电器  │ │
│  └──────────┬───────────┘ └──────────┬───────────┘ └────────┬─────────┘ │
│             │                        │                      │           │
│             └────────────────┬───────┴──────────────────────┘           │
│                              ▼                                          │
│           ┌─────────────────────────────────────┐                       │
│           │   Envelope Builder (parking.*/1)   │                       │
│           │   单调递增持久化 Seq 分配器         │                       │
│           └──────────────────┬──────────────────┘                       │
│                              │                                          │
│              ┌───────────────┴───────────────┐                          │
│              ▼                               ▼                          │
│     ┌─────────────────┐             ┌─────────────────┐                 │
│     │ Local Disk Spool│             │ MQTT Transport  │                 │
│     │ 掉电缓冲与重放  │             │ (TLS / 鉴权)    │                 │
│     └─────────────────┘             └────────┬────────┘                 │
└──────────────────────────────────────────────┼──────────────────────────┘
                                               │
                        ┌──────────────────────┴──────────────────────┐
                        ▼                                             ▼
             ┌─────────────────────┐                       ┌─────────────────────┐
             │ MQTT Broker (现场)  │                       │  内置 Web / HTTP    │
             │ Topic: site/parking/│                       │  :8080 端点与       │
             │ <camera_id>/<kind>  │                       │  浏览器可视化编辑器 │
             └──────────┬──────────┘                       └─────────────────────┘
                        ▼
       ┌─────────────────────────────────┐
       │ 现场系统 / 终端集成             │
       │ 道闸控制 · 余位屏 ·             │
       │ Grafana 看板 · 上级业务系统     │
       └─────────────────────────────────┘
```

---

## 🎯 三大业务预设能力

在部署配置 `vb.config/1` 中通过 `app.name` 与配置项选择对应的业务能力：

### 1. 出入口通行计数 (`counting`)

- **业务场景**：停车场进出口、园区出入门禁通道、单向/双向车道流量统计。
- **推荐模型**：`yolox_tiny_coco_416`（检测类别：小汽车、大客车、货车、摩托车）。
- **功能特性**：
  - 支持配置空间虚拟线段 `[a, b]` 与朝向判定规则（`in_side: positive`）。
  - 轨迹完整度约束（`min_track_frames`、`min_travel`）与防二次重复触发窗口（`reborn_window_s`），有效过滤拥堵蠕行时的误计。
  - 动态余位状态机：自动维护 `occupancy`（当前停放数）、`capacity`（总车位数）与 `available`（剩余空位数）。
  - 内置可视化调线工具 `http://<ip>:8080/counting/editor`：在真实视频帧上直接拖拽调整警戒线与进出朝向，一键保存即时生效。
  - 发布 `parking.crossing/1` 通行事件与带保留标记（retained）的 `parking.occupancy/1` 余位信封。

### 2. 远景车位占用检测 (`slots`)

- **业务场景**：地面露天停车场、地下地库大视角俯拍监控覆盖。
- **推荐模型**：`yolox_tiny_coco_640`（640² 高清目标检测）。
- **功能特性**：
  - 单路画面支持配置数十个自定义四边形或多边形车位 ROI。
  - 结合中心点落区与边界框重叠率（`occupied_ratio` / `free_ratio`）判定空间归属。
  - 状态时间迟滞滤波（Hysteresis Filter）：支持设定占位延迟（`hold_s: 3.0s`）与释放延迟（`free_hold_s: 5.0s`），消除车辆入库倒车打方向与行人遮挡造成的状态抖动。
  - 交互式 Web 多边形画布 `http://<ip>:8080/slots/editor`：基于 HTML5 Canvas 直接在相机底图上点击绘制、重命名多边形，通过 `PUT /config/slots` 无缝更新配置。
  - 发布包含各车位详尽状态的 `parking.slots/1`（retained）快照。

### 3. 车牌识别与道闸门禁联动 (`plate`)

- **业务场景**：通道无人值守道闸、内部车辆白名单放行、停车审计。
- **两阶段端侧架构**：
  - **第一阶段（检测）**：`plate_det_yolox_tiny_640`（低算力端如 SG2002 采用 416²）。
  - **第二阶段（识别）**：`plate_rec_cn_96x32` PlateRecNet 纯卷积 CTC 网络，识别 66 种汉字（省份简称）、英文字母、数字及新能源绿牌专属标识。
- **核心算法亮点**：
  - **轴对齐上下文裁切重训（`axisctx`）**：针对边缘端运行时检测框抖动特性注入扰动训练，使新能源绿牌实测整牌识别率高达 **80.5%**，发布置信度达 **91.7%**。
  - **多帧滑窗投票机制**：7 帧滑窗结合置信度门控（Gate），彻底规避运动模糊与反光造成的偶发识别错误。
  - **道闸自动控制逻辑（`gate.py`）**：内置基于内存与文件的白名单比对（`whitelist.py`），支持 HTTP 回调、硬件继电器信号或 ONVIF 联动开闸，并自动保存抓拍图（`/preview/plate.jpg`）。
  - 发布 `parking.plate/1` 识别信封与 `parking.gate/1` 道闸动作信封。

---

## ⚡ 硬件平台支持矩阵

| 硬件平台 / 产品设备 | 加速引擎与运行时 | 模型格式 | 典型应用与算力规格 | 支持的预设 |
|---|---|---|---|---|
| [**Seeed reComputer Industrial J4012 / J3011**](https://www.seeedstudio.com/reComputer-Industrial-J4012-p-5684.html)<br>(NVIDIA Jetson Orin Nano / NX) | TensorRT 10.3 + GStreamer NVDEC | FP16 TensorRT Engine (`.engine`) | 多路高帧率并发视觉分析 | `counting`, `slots`, `plate` |
| [**Seeed reComputer RK3588**](https://www.seeedstudio.com/reComputer-RK3588-40-p-6818.html)<br>(Rockchip RK3588) | 6 TOPS NPU (RKNN-Toolkit2 2.3.2) | W8A8 (INT8) + FP16 输出 (`.rknn`) | 工业级边缘微服务器 / 多路智能盒子 | `counting`, `slots`, `plate` |
| [**Seeed reComputer Industrial R2235**](https://www.seeedstudio.com/reComputer-Industrial-R2235-12-p-6654.html)<br>(Rockchip RK3576) | 6 TOPS NPU (RKNN-Toolkit2 2.3.2) | W8A8 (INT8) + FP16 输出 (`.rknn`) | 紧凑型边缘网关 / 单机双车道闸口 | `counting`, `slots`, `plate` |
| **树莓派 5 + Hailo-8** | HailoRT 4.21.0 (26 TOPS NPU) | INT8 HEF (`.hef`) | 低功耗模块化边缘工控机 | `counting`, `plate` |
| [**Seeed reCamera-2002**](https://www.seeedstudio.com/reCamera-2002w-8GB-p-6250.html)<br>(算能 SG2002 / CV181x RISC-V) | INT8 CVIMODEL (`.cvimodel`) | 一体化全内置智能摄像机（deb 包部署） | `plate`, `counting` |
| **x86 / ARM CPU** | ONNX Runtime CPU | FP32 ONNX (`.onnx`) | 开发调试、基准对照、低频通道 | `counting`, `slots`, `plate` |
| **R1124 网关** | Mosquitto MQTT + Telegraf | — | 现场边缘通信中枢与遥测聚合 | 消息代理、Grafana 看板 |

### Seeed Studio 方案与硬件深度集成

`edge-parking-vision` 是驱动 Seeed Studio 官方智慧停车行业解决方案与系列可部署方案的核心视觉引擎：

- 🏙️ **官方行业场景落地页**：[Seeed 智慧停车视觉方案](https://www.seeedstudio.com.cn/solutions/parking-traffic-vision)（`parking-traffic-vision`）
- 📦 **配套可部署方案（Reference Designs）**：
  - [停车场车辆进出计数可部署方案 (`edge_parking_counting`)](https://www.seeedstudio.com.cn/solutions/reference-designs/edge_parking_counting)
  - [多车位占用检测可部署方案 (`edge_parking_occupancy`)](https://www.seeedstudio.com.cn/solutions/reference-designs/edge_parking_occupancy)
  - [边缘车牌识别与道闸门禁可部署方案 (`edge_plate_recognition`)](https://www.seeedstudio.com.cn/solutions/reference-designs/edge_plate_recognition)
- 🛒 **认证边缘算力硬件**：
  - [reCamera 2002](https://www.seeedstudio.com/reCamera-2002w-8GB-p-6250.html)：超紧凑开源 RISC-V 智能 AI 相机，自带 PoE，单机就地覆盖 4–6 个车位。
  - [reComputer Industrial J4012](https://www.seeedstudio.com/reComputer-Industrial-J4012-p-5684.html) / [J3011](https://www.seeedstudio.com/reComputer-Industrial-J3011-p-5682.html)：无风扇工业级 Jetson Orin 边缘主机，适合多路车道进出高并发分析。
  - [reComputer Rugged J4012](https://www.seeedstudio.com/reComputer-Rugged-J4012-p-6920.html) / [J3011](https://www.seeedstudio.com/reComputer-Rugged-J3011-p-6921.html)：IP66 级防水防尘全密封户外加固型边缘计算机。
  - [reComputer RK3588 (R1000)](https://www.seeedstudio.com/reComputer-RK3588-40-p-6818.html)：高性能瑞芯微 6 TOPS NPU 工业工控机。
  - [reComputer Industrial R2235 (RK3576)](https://www.seeedstudio.com/reComputer-Industrial-R2235-12-p-6654.html)：导轨式工业网关，具备丰富物理 IO 接口直驱道闸继电器。

---

## 📊 权威实测基准数据

以下测试均在标准 CCPD 留出测试集与对应物理硬件上真实采集（详见 `evaluation/results/`）：

### 1. CCPD 留出集端到端精度评测（3,400 张图像，ONNX FP32，7 帧投票）

| 评测子集 | 图像量 | 检测召回率 | 整牌准确率 | 字符准确率 | 发布置信度 | 正确发布率 |
|---|---|---|---|---|---|---|
| **新能源绿牌 (`green`)** | 1,000 张 | **99.8%** | **80.5%** | **93.6%** | **91.7%** | **81.6%** |
| **标准蓝牌 (`base`)** | 300 张 | **100.0%** | **96.0%** | **99.4%** | **96.3%** | **96.3%** |
| **恶劣天气 (`weather`)** | 300 张 | **100.0%** | **91.0%** | **98.7%** | **91.2%** | **90.3%** |
| **旋转角度 (`rotate`)** | 300 张 | **99.3%** | **81.0%** | **96.6%** | **83.3%** | **81.7%** |
| **倾斜角度 (`tilt`)** | 300 张 | **100.0%** | **75.3%** | **96.3%** | **80.3%** | **78.7%** |
| **全集综合评测** | **3,400 张** | **99.1%** | **80.9%** | **95.2%** | **87.8%** | **82.0%** |

### 2. 真实物理设备量化精度与性能基准（504 张设备子集，单帧推理）

| 边缘硬件设备 | 加速后端与精度 | 检测召回率 | 整牌准确率 | 门控精度 | 总计耗时 (504 张) | 折合吞吐率 |
|---|---|---|---|---|---|---|
| **NVIDIA Jetson Orin Nano** | TensorRT FP16 | 98.4% | 77.4% | 88.6% | **30.7 s** | ~61.4 FPS |
| **Rockchip RK3588** | RKNN INT8 (W8A8) | 98.8% | 78.8% | 88.8% | **38.5 s** | ~76.4 FPS |
| **Rockchip RK3576** | RKNN INT8 (W8A8) | 98.8% | 79.0% | 89.0% | **53.4 s** | ~53.4 FPS |
| **树莓派 5 + Hailo-8** | HailoRT INT8 | 98.6% | 78.4% | 88.5% | — | ~50+ FPS |

---

## 🚀 快速上手

### 1. 环境依赖

- Python `>= 3.10`
- 推荐使用 [uv](https://github.com/astral-sh/uv)，或直接使用标准库 `python3 -m venv`

### 2. 安装与测试

克隆代码仓库并执行完整回归测试：

```bash
git clone https://github.com/suharvest/edge-parking-vision.git
cd edge-parking-vision

# 安装开发测试依赖（pytest, jsonschema, pyyaml, onnx）
uv sync

# 运行全量单元测试（包含全部 480+ 测试用例）
uv run pytest
```

预期输出：
```text
======================= 482 passed, 11 skipped in ~30s =======================
```

### 3. 校验生产标准库零依赖约束

执行静态扫描，确保核心业务代码绝对不含第三方包引用：

```bash
uv run python tools/check_stdlib_only.py core-py/core_parking contracts/validate_payload.py
```

### 4. 启动配套遥测看板（可选）

项目在 `backend/` 目录下提供了基于 Docker Compose 的演示遥测栈（Telegraf + InfluxDB 2 + Grafana）：

```bash
# 启动遥测套件
MQTT_HOST=broker.local docker compose -f backend/docker-compose.yml up -d
```

- 浏览器访问 `http://localhost:3000`（默认用户名：`demo`，密码：`demodemo`）。
- 自动载入预设的 **Parking Occupancy Dashboard**，实时呈现进出流量曲线、车场剩余车位及历史事件。

---

## 🌐 HTTP 端点与可视化工具

内置 HTTP 服务由标准库 `ThreadingHTTPServer` 承载，默认监听 `8080` 端口：

| 请求方法 | 路由路径 | 功能说明 |
|---|---|---|
| `GET` | `/events?limit=50` | 获取最近生成的停车事件列表（内存环形缓冲区）。 |
| `GET` | `/preview` | 实时预览页面，浏览器端渲染检测框、警戒线及车位标注。 |
| `GET` | `/counting/editor` | **警戒线可视化编辑器**：拖拽交互调整计数警戒线，交换进出方向。 |
| `GET` | `/slots/editor` | **车位多边形编辑器**：在实时快照上直接绘制并标注多边形车位区域。 |
| `GET` | `/preview.jpg?stream=<id>` | 获取指定摄像机路数的最近高质量 JPEG 快照（内置 1s/次 流控）。 |
| `GET` | `/preview.json?stream=<id>` | 获取源归一化检测框坐标、警戒线参数及当前识别数据。 |
| `GET` | `/occupancy` | 余位数据直接输出，供现场 LED 余位屏或第三方硬件轮询。 |
| `POST` | `/occupancy` | 手动校准当前停放车辆数（例如 `{"occupancy": 50}`）。 |
| `GET/PUT`| `/config/counting` | 查询或热更新出入口警戒线配置，即时持久化到覆盖文件。 |
| `GET/PUT`| `/config/slots` | 查询或热更新车位多边形几何列表，无需重启服务。 |
| `GET` | `/snapshot/{stream}.jpg` | 获取高分辨率背景抓拍，供车位编辑器标定使用。 |

---

## 📡 MQTT 事件契约规范

所有向外发布的事件均封装于公共信封（`contracts/envelope.schema.json`）中，主题格式统一为：
```text
<site_id>/parking/<camera_id>/<kind>
```

### 1. 车辆穿越警戒线事件 (`parking.crossing/1`)
```json
{
  "schema": "parking.crossing/1",
  "event_id": "c1f77d54-8e3d-4c32-a548-c923368ea5b1",
  "site_id": "demo-lot",
  "camera_id": "gate-a",
  "ts": 1728564000123,
  "seq": 1042,
  "replayed": false,
  "track_id": 18,
  "class_id": 2,
  "direction": "in",
  "speed_mps": null
}
```

### 2. 车场余位状态快照 (`parking.occupancy/1`，Retained)
```json
{
  "schema": "parking.occupancy/1",
  "event_id": "b3e945c2-12aa-4251-87ab-fd08914b1092",
  "site_id": "demo-lot",
  "camera_id": "gate-a",
  "ts": 1728564000125,
  "seq": 1043,
  "replayed": false,
  "occupancy": 84,
  "capacity": 120,
  "available": 36,
  "source": "crossing"
}
```

### 3. 车牌识别事件 (`parking.plate/1`)
```json
{
  "schema": "parking.plate/1",
  "event_id": "0d8a1c93-547e-41bc-91e8-7ba6a9e1e328",
  "site_id": "demo-lot",
  "camera_id": "gate-a",
  "ts": 1728564005432,
  "seq": 1044,
  "replayed": false,
  "plate_text": "粤B12345D",
  "plate_type": "cn_green",
  "confidence": 0.942,
  "track_id": 19,
  "vote_count": 5,
  "snapshot_url": "/snapshot/gate-a/20261010-143005-粤B12345D.jpg"
}
```

### 4. 道闸动作事件 (`parking.gate/1`)
```json
{
  "schema": "parking.gate/1",
  "event_id": "e45a2789-f53e-4d56-a149-8e2b83c18b10",
  "site_id": "demo-lot",
  "camera_id": "gate-a",
  "ts": 1728564005450,
  "seq": 1045,
  "replayed": false,
  "action": "open",
  "trigger": "whitelist",
  "plate_text": "粤B12345D",
  "status": "success"
}
```

### 5. 广角车位占用映射 (`parking.slots/1`，Retained)
```json
{
  "schema": "parking.slots/1",
  "event_id": "84d7a88e-6701-4475-9b76-a2128e4e9b98",
  "site_id": "demo-lot",
  "camera_id": "lot-cam-01",
  "ts": 1728564010000,
  "seq": 204,
  "replayed": false,
  "total": 4,
  "occupied": 3,
  "free": 1,
  "slots": [
    {"id": "B1-023", "status": "occupied", "occupied_ratio": 0.85},
    {"id": "B1-024", "status": "free", "occupied_ratio": 0.02},
    {"id": "B1-025", "status": "occupied", "occupied_ratio": 0.78},
    {"id": "B1-026", "status": "occupied", "occupied_ratio": 0.91}
  ]
}
```

---

## 📂 代码目录导览

```text
edge-parking-vision/
├── contracts/             # 核心契约：JSON Schema 规范与纯标准库验证器
│   ├── app-options.schema.json
│   ├── crossing-event.schema.json
│   ├── envelope.schema.json
│   ├── gate-config.schema.json
│   ├── occupancy.schema.json
│   ├── plate-event.schema.json
│   ├── slots.schema.json
│   └── validate_payload.py
├── core-py/core_parking/  # 纯 Python 标准库应用核心
│   ├── config.py          # 启动配置解析与 ParkingApp 装配
│   ├── envelope.py        # 信封组装与单调持久化 seq 分配器
│   ├── hooks_counting.py  # 通行警戒线穿越与余位计数状态机
│   ├── http_app.py        # HTTP 服务端与静态可视化编辑器路由
│   ├── occupancy.py       # 车场容量状态机与频控
│   ├── spool.py           # 本地持久化故障缓冲队列
│   ├── transport.py       # MQTT 传输客户端与断线重发机制
│   ├── plate/             # 车牌业务域、字符集、白名单与道闸控制器
│   │   ├── charset.py     # 66 种中文字符与车牌编码映射
│   │   ├── gate.py        # 道闸状态机、HTTP/继电器/ONVIF 触发
│   │   ├── hooks_plate.py # 车牌推理钩子与滑窗置信度投票
│   │   └── whitelist.py   # 本地白名单管理与模糊匹配
│   └── slots/             # 广角车位占用业务域
│       ├── app.py         # SlotsApp 状态管理与端点
│       ├── editor.html    # 浏览器 Canvas 多边形车位标定器
│       └── hooks_slots.py # 几何碰撞检测与时间滞后滤波
├── core-cpp/              # C++ 嵌入式轻量级业务域实现
│   └── plate/             # 车牌标准化与嵌入式本地缓冲
├── platforms/             # 异构硬件部署预设与打包脚本
│   ├── cpu/               # ONNX Runtime CPU 部署预设
│   ├── docker/            # Jetson 原生构建与打包说明
│   ├── jetson/            # NVIDIA Jetson 预设与 TensorRT 构建
│   ├── rk/                # Rockchip RKNN 转换脚本与部署文档
│   ├── recamera-2002/     # Seeed reCamera SG2002 deb 安装包制作
│   └── host-mounts/       # 各硬件主机的 Docker 挂载规范
├── training/              # 模型训练、数据合成与转换导出
│   ├── platerec.py        # PlateRecNet CNN-CTC 架构定义
│   ├── train_platerec.py  # axisctx 抖动裁剪模式训练主脚本
│   ├── plate_synth.py     # 真实中式车牌图像合成生成器
│   ├── export_platerec.py # 静态 ONNX 导出脚本 (opset 11)
│   └── convert_platerec_rknn_fp16out.py # RKNN 混合量化转换
├── evaluation/            # 端到端评测与稳定性压力测试
│   ├── plate_e2e_eval.py  # CCPD 全流程精度评测工具
│   ├── stress_slots.py    # 多路长窗口 FPS 与稳定性压测
│   └── results/           # 真实设备评测记录与指标数据
├── backend/               # 演示遥测与可视化看板
│   ├── docker-compose.yml # Telegraf + InfluxDB + Grafana 编排
│   └── grafana/           # 开箱即用的 Grafana 仪表盘配置
├── tools/                 # 工程规范检查与辅助脚本
│   ├── check_stdlib_only.py  # 标准库零依赖合规门禁扫描
│   ├── compare_events.py     # 事件流与真值比对工具
│   └── build_overlay_images.sh # 镜像打包工具
└── tests/                 # 单元测试与契约测试集（480+ 用例）
```

---

## 🛠️ 模型训练与导出指南

### 1. 车牌识别模型训练 (`PlateRecNet`)

PlateRecNet 是针对边缘 NPU 优化的纯卷积 CTC 架构，输入分辨率为 `96×32`，分类头涵盖 66 个字符。

使用 **运行时轴对齐上下文裁剪（`axisctx`）** 模式训练：

```bash
# 1. 基于检测模型与真实标注预生成轴对齐裁剪图缓存
python training/gen_platerec_axis_crops.py \
  --det models/plate_det_yolox_tiny_640.onnx \
  --exclude-set evaluation/data/plate_e2e/eval_set.jsonl \
  --out data/processed/platerec_axisctx

# 2. 启动 PlateRecNet 训练（采用 CTC Loss 与余弦退火学习率）
python training/train_platerec.py \
  --crop-mode axisctx \
  --axisctx-dir data/processed/platerec_axisctx \
  --epochs 60 \
  --batch-size 256 \
  --out training/outputs/platerec_cn_axis

# 3. 导出为静态 ONNX 权重
python training/export_platerec.py \
  --ckpt training/outputs/platerec_cn_axis/best.pth \
  --out plate_rec_cn_96x32.onnx
```

### 2. 瑞芯微 RKNN 量化转换

将检测或识别 ONNX 模型转换为支持 RK3588/RK3576 的 NPU 模型：

```bash
python platforms/rk/convert_rknn.py \
  --onnx models/yolox_tiny_coco_416.onnx \
  --calib-list evaluation/data/calib/vehicle_416.txt \
  --size 416 \
  --target rk3588 \
  --out-dir dist/vehicle416-rk3588
```

---

## 📜 许可证与致谢

本项目采用 [Apache License 2.0](LICENSE) 开源许可证。

### 第三方组件与数据集声明

- **CCPD 数据集**：车牌检测与识别模型训练参考了 [CCPD Dataset](https://github.com/detectRecog/CCPD)（Xu et al., ECCV 2018），遵循 [MIT 许可](licenses/CCPD-MIT.txt)。
- **YOLOX**：目标检测网络与导出实现衍生自 [Megvii-BaseDetection/YOLOX](https://github.com/Megvii-BaseDetection/YOLOX)，遵循 Apache-2.0 许可。
- **COCO 2017**：基础车辆检测预训练权重衍生自 COCO 标注（CC BY 4.0）。
- 详尽的许可证合规说明、硬件厂商 SDK 声明及署名见 [NOTICE](NOTICE) 与 [LICENSES.md](LICENSES.md)。
