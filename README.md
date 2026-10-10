# edge-parking-vision

<p align="center">
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache_2.0-blue.svg" alt="License"></a>
  <img src="https://img.shields.io/badge/Python-3.10+-green.svg" alt="Python">
  <img src="https://img.shields.io/badge/Runtime-Stdlib_Only-success.svg" alt="Runtime">
  <img src="https://img.shields.io/badge/Tests-480+_passing-brightgreen.svg" alt="Tests">
  <img src="https://img.shields.io/badge/Platforms-reComputer_J30/J40_%7C_RK3588_%7C_RK3576_%7C_R2000_%7C_reCamera--2002-orange.svg" alt="Platforms">
  <img src="https://img.shields.io/badge/Presets-Counting_%7C_Slots_%7C_Plate_Recognition-blueviolet.svg" alt="Presets">
</p>

<p align="center">
  <strong>Production-ready Edge AI Parking Vision Suite</strong><br>
  Real-time Vehicle Counting · Parking Slot Occupancy · License Plate Recognition & Gate Control<br>
  <em>100% On-Device · Zero Cloud Dependency · Zero External Python Dependencies in Production</em>
</p>

<p align="center">
  <a href="README.md">English</a> | <a href="README_zh.md">简体中文</a>
</p>

---

## 📖 Overview

**edge-parking-vision** is a lightweight, edge-native vision AI application suite designed for smart parking lots and access control facilities. It delivers real-time computer vision inference directly on embedded edge hardware:

1. **Vehicle Entrance/Exit Counting (`counting`)**: Bidirectional virtual crossing-line vehicle tracking, real-time parking lot occupancy accounting, and rate-limited MQTT updates.
2. **Parking Slot Occupancy Detection (`slots`)**: Multi-space polygon ROI occupancy monitoring, anti-flicker temporal debouncing, and in-browser interactive ROI editor.
3. **License Plate Recognition & Access Control (`plate`)**: High-accuracy two-stage Chinese LPR (YOLOX-Tiny + PlateRecNet CTC), multi-frame voting, plate allowlist management, and automated barrier gate trigger via HTTP/relay/ONVIF.

### Why edge-parking-vision?

- **100% On-Device & Fully Offline**: All video ingestion, NPU neural inference, multi-object tracking, state transitions, and event dispatches run entirely on the local device. No frame or token leaves the local network.
- **Zero Third-Party Python Dependencies in Production**: The application core (`core-py/core_parking`) is engineered with **pure Python 3.10+ standard libraries**. No heavy dependencies, no version conflicts, minimal footprint, and instant cold start on embedded Linux.
- **Cross-Platform Hardware Portability**: Validated and optimized across **NVIDIA Jetson** (TensorRT FP16), **Rockchip NPU** (RK3588 / RK3576 RKNN W8A8), **Raspberry Pi 5 + Hailo-8** (HailoRT INT8), and **Seeed reCamera-2002** (Sophgo SG2002/CV181x RISC-V).
- **Crash-Safe Event Spooling**: Integrated local disk queue with monotonic sequential event IDs (`seq`), ensuring **zero event loss** during MQTT network interruptions with automatic reconnect and playback.
- **Contract-Driven Consistency**: All configuration parameters, MQTT event envelopes, and REST API payloads are strictly validated against versioned JSON Schemas (`contracts/`).
- **Visual In-Browser Calibration**: Built-in HTTP server with live canvas-based web editors to drag-and-drop counting lines (`/counting/editor`) and draw parking slot polygons (`/slots/editor`) over live camera snapshots.

---

## 🏗️ System Architecture

```text
                       ┌────────────────────────┐
                       │   RTSP Camera Stream   │
                       └───────────┬────────────┘
                                   │
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                      Edge Native Runtime (vb-runtime)                   │
│   Hardware Video Decode (NVDEC / MPP / CVI) + Tracking + NPU Inference  │
│   YOLOX-Tiny (416/640) · PlateRecNet CNN-CTC (96x32) · Multi-tracker    │
└──────────────────────────────────┬──────────────────────────────────────┘
                                   │ Frame Detections & Snapshots
                                   ▼
┌─────────────────────────────────────────────────────────────────────────┐
│                 core_parking Application Layer (Python stdlib)          │
│                                                                         │
│  ┌──────────────────────┐ ┌──────────────────────┐ ┌──────────────────┐ │
│  │   Counting Hooks     │ │      Slots Hooks     │ │   Plate & Gate   │ │
│  │  Line cross (in/out) │ │  Polygon ROI overlap │ │  Plate detection │ │
│  │  Capacity & free sp. │ │  Debounce filter     │ │  PlateRecNet CTC │ │
│  │  Rate limit & state  │ │  Per-slot occupancy  │ │  Allowlist/Relay │ │
│  └──────────┬───────────┘ └──────────┬───────────┘ └────────┬─────────┘ │
│             │                        │                      │           │
│             └────────────────┬───────┴──────────────────────┘           │
│                              ▼                                          │
│           ┌─────────────────────────────────────┐                       │
│           │   Envelope Builder (parking.*/1)   │                       │
│           │   Monotonic Seq Allocator           │                       │
│           └──────────────────┬──────────────────┘                       │
│                              │                                          │
│              ┌───────────────┴───────────────┐                          │
│              ▼                               ▼                          │
│     ┌─────────────────┐             ┌─────────────────┐                 │
│     │ Local Disk Spool│             │ MQTT Transport  │                 │
│     │ (Buffer/Replay) │             │ (TLS / Auth)    │                 │
│     └─────────────────┘             └────────┬────────┘                 │
└──────────────────────────────────────────────┼──────────────────────────┘
                                               │
                        ┌──────────────────────┴──────────────────────┐
                        ▼                                             ▼
             ┌─────────────────────┐                       ┌─────────────────────┐
             │ MQTT Broker (Local) │                       │  Built-in Web / HTTP│
             │ Topic: site/parking/│                       │  :8080 Endpoints &  │
             │ <camera_id>/<kind>  │                       │  Visual Web Editors │
             └──────────┬──────────┘                       └─────────────────────┘
                        ▼
       ┌─────────────────────────────────┐
       │ Field Integration / Consumers   │
       │ Barrier Gates · LED Signs ·     │
       │ Grafana Dashboard · Local SCADA │
       └─────────────────────────────────┘
```

---

## 🎯 Presets & Capabilities

The repository unifies three practical parking scenarios via configurable presets in `vb.config/1`:

### 1. Vehicle Counting & Occupancy (`counting`)

- **Objective**: Accurately track vehicle ingress/egress at parking gates and maintain lot occupancy counts.
- **Model**: `yolox_tiny_coco_416` (vehicle classes: car, bus, truck, motorcycle).
- **Core Features**:
  - Configurable virtual tripwire `[a, b]` with directional vectors (`in_side: positive`).
  - Trajectory continuity check (`min_track_frames`, `min_travel`) and rebirth window suppression to prevent duplicate counts during slow rolling or traffic jams.
  - Dynamic lot balance computation: maintains `occupancy`, `capacity`, and `available` spots.
  - Web calibration editor at `http://<ip>:8080/counting/editor`: drag lines and swap in/out directions live on screen.
  - Publishes `parking.crossing/1` and retained `parking.occupancy/1` events.

### 2. Parking Slot Occupancy Monitoring (`slots`)

- **Objective**: Monitor surface or underground parking bays with wide-angle overhead cameras.
- **Model**: `yolox_tiny_coco_640` (high-resolution vehicle detection).
- **Core Features**:
  - Supports multiple custom polygon/bounding ROIs per camera view.
  - Center-point inside polygon and bounding-box overlap ratio calculation.
  - State hysteresis filter: configurable delay (`hold_s: 3.0s` for occupied, `free_hold_s: 5.0s` for free) prevents flickering caused by pedestrians or reversing cars.
  - Interactive web canvas editor at `http://<ip>:8080/slots/editor`: draw arbitrary polygon coordinates directly over camera snapshots and persist changes via `PUT /config/slots` without downtime.
  - Publishes retained `parking.slots/1` state maps.

### 3. License Plate Recognition & Gate Control (`plate`)

- **Objective**: Automated barrier gate opening and license plate audit for gated facilities.
- **Pipeline**: Two-stage hierarchical inference:
  - **Stage 1 (Detection)**: `plate_det_yolox_tiny_640` (or `416` on micro-cameras) outputs bounding coordinates.
  - **Stage 2 (Recognition)**: `plate_rec_cn_96x32` PlateRecNet CNN-CTC network recognizes 66 character classes (Chinese provinces, numbers, letters, and green NEV indicators).
- **Key Innovations & Stability**:
  - **Axis-Context Crop Training (`axisctx`)**: Models are trained on jittered detection crops matching real runtime inference characteristics, boosting whole-plate accuracy to **80.5%** on challenging green NEV plates and achieving **91.7%** publication precision.
  - **Multi-Frame Temporal Voting**: 7-frame sliding window with confidence thresholding eliminates single-frame motion blur errors.
  - **Automated Gate Controller (`gate.py`)**: Checks against memory/file allowlists (`whitelist.py`), triggers gate opening via HTTP, physical relay, or ONVIF action, and stores audit snapshots (`/preview/plate.jpg`).
  - Publishes `parking.plate/1` and `parking.gate/1` events.

---

## ⚡ Supported Hardware Platforms

| Platform / Hardware | Accelerator / Runtime | Model Format | Target Workload | Presets Supported |
|---|---|---|---|---|
| [**Seeed reComputer Industrial J4012 / J3011**](https://www.seeedstudio.com/reComputer-Industrial-J4012-p-5684.html)<br>(NVIDIA Jetson Orin Nano / NX) | TensorRT 10.3 + GStreamer NVDEC | FP16 TensorRT (`.engine`) | High-density multi-stream inference | `counting`, `slots`, `plate` |
| [**Seeed reComputer RK3588**](https://www.seeedstudio.com/reComputer-RK3588-40-p-6818.html)<br>(Rockchip RK3588) | 6 TOPS NPU (RKNN-Toolkit2 2.3.2) | W8A8 INT8 + FP16 out (`.rknn`) | Multi-channel edge box & industrial PC | `counting`, `slots`, `plate` |
| [**Seeed reComputer RK3576**](https://www.seeedstudio.com/reComputer-RK3576-30-p-6815.html)<br>(Rockchip RK3576) | 6 TOPS NPU (RKNN-Toolkit2 2.3.2) | W8A8 INT8 + FP16 out (`.rknn`) | Compact edge gateway / dual-lane gate | `counting`, `slots`, `plate` |
| [**Seeed reComputer Industrial R2000 (R2035-12)**](https://www.seeedstudio.com/reComputer-Industrial-R2035-12-p-6542.html)<br>(Raspberry Pi 5 + Hailo-8) | HailoRT 4.21.0 (26 TOPS NPU) | INT8 HEF (`.hef`) | Fanless industrial edge AI | `counting`, `plate` |
| [**Seeed reCamera-2002**](https://www.seeedstudio.com/reCamera-2002w-8GB-p-6250.html)<br>(Sophgo SG2002 / CV181x RISC-V) | INT8 CVIMODEL (`.cvimodel`) | Standalone smart IP camera (deb package) | `plate`, `counting` |
| [**Seeed reComputer R1100 (R1124-10)**](https://www.seeedstudio.com/reComputer-R1124-10-p-6257.html)<br>(Edge IoT Gateway & Controller) | Mosquitto MQTT + Telegraf | — | Field broker & telemetry aggregation | MQTT broker, Grafana |

### Seeed Studio Solutions & Hardware Integration

`edge-parking-vision` serves as the core computer vision engine powering Seeed Studio's official Smart Parking reference designs and solution pages:

- 🏙️ **Landing Solution**: [Seeed Smart Parking Traffic Vision Scenario](https://www.seeedstudio.com.cn/solutions/parking-traffic-vision) (`parking-traffic-vision`)
- 📦 **Reference Designs**:
  - [Vehicle Entrance/Exit Counting Reference Design (`edge_parking_counting`)](https://www.seeedstudio.com.cn/solutions/reference-designs/edge_parking_counting)
  - [Parking Space Occupancy Monitoring Reference Design (`edge_parking_occupancy`)](https://www.seeedstudio.com.cn/solutions/reference-designs/edge_parking_occupancy)
  - [Edge License Plate Recognition Reference Design (`edge_plate_recognition`)](https://www.seeedstudio.com.cn/solutions/reference-designs/edge_plate_recognition)
- 🛒 **Certified Edge Hardware**:
  - [reCamera 2002](https://www.seeedstudio.com/reCamera-2002w-8GB-p-6250.html): Ultra-compact open source smart AI camera with PoE.
  - [reComputer Industrial J4012](https://www.seeedstudio.com/reComputer-Industrial-J4012-p-5684.html) / [J3011](https://www.seeedstudio.com/reComputer-Industrial-J3011-p-5682.html): Fanless industrial PCs with Jetson Orin for dusty or factory environments.
  - [reComputer Rugged J4012](https://www.seeedstudio.com/reComputer-Rugged-J4012-p-6920.html) / [J3011](https://www.seeedstudio.com/reComputer-Rugged-J3011-p-6921.html): IP66-rated waterproof and dustproof outdoor enclosure.
  - [reComputer RK3588 (R1000)](https://www.seeedstudio.com/reComputer-RK3588-40-p-6818.html): High-compute edge AI box for multi-channel video analysis.
  - [reComputer RK3576 (RK3576-30)](https://www.seeedstudio.com/reComputer-RK3576-30-p-6815.html): Compact edge AI box powered by Rockchip RK3576.
  - [reComputer Industrial R2000 (R2035-12)](https://www.seeedstudio.com/reComputer-Industrial-R2035-12-p-6542.html): Fanless industrial PC powered by Raspberry Pi 5 + Hailo-8 (26 TOPS NPU).
  - [reComputer R1100 (R1124-10)](https://www.seeedstudio.com/reComputer-R1124-10-p-6257.html): Industrial IoT gateway & edge controller for field MQTT broker and device integration.

---

## 📊 Benchmark & Accuracy Evidence

All benchmarks are measured against official CCPD holdout datasets and verified on physical hardware:

### 1. 3,400 Benchmark Images (CCPD Held-out Test Set, ONNX FP32, 7-Frame Voting)

| Evaluation Subset | Images | Detection Recall | Whole-Plate Accuracy | Publication Precision | Correct Publish Rate |
|---|---|---|---|---|---|
| **Green NEV Plates (`green`)** | 1,000 | **99.8%** | **80.5%** | **91.7%** | **81.6%** |
| **Standard Blue Plates (`base`)** | 300 | **100.0%** | **96.0%** | **96.3%** | **96.3%** |
| **Challenging Weather (`weather`)**| 300 | **100.0%** | **91.0%** | **91.2%** | **90.3%** |
| **Rotated Plates (`rotate`)** | 300 | **99.3%** | **81.0%** | **83.3%** | **81.7%** |
| **Tilted Angles (`tilt`)** | 300 | **100.0%** | **75.3%** | **80.3%** | **78.7%** |
| **All Test Sets Combined** | **3,400** | **99.1%** | **80.9%** | **87.8%** | **82.0%** |

### 2. Physical Device Quantization Benchmark (504-Image Benchmark Subset, Single Frame)

| Edge Device | Backend & Precision | Detection Recall | Whole-Plate Acc | Gate Precision | Benchmark Latency (504 imgs) | Effective Throughput |
|---|---|---|---|---|---|---|
| **Seeed reComputer J3011** (Jetson Orin Nano) | TensorRT FP16 | 98.4% | 77.4% | 88.6% | **30.7 s** | ~61.4 FPS |
| **Seeed reComputer RK3588** (Rockchip RK3588) | RKNN INT8 (W8A8) | 98.8% | 78.8% | 88.8% | **38.5 s** | ~76.4 FPS |
| **Seeed reComputer RK3576** (Rockchip RK3576) | RKNN INT8 (W8A8) | 98.8% | 79.0% | 89.0% | **53.4 s** | ~53.4 FPS |
| **Seeed reComputer Industrial R2035** (Pi 5 + Hailo-8) | HailoRT INT8 | 98.6% | 78.4% | 88.5% | — | ~50+ FPS |

*Note: Detailed evaluation logs, raw execution records, and confusion matrices are available under `evaluation/results/`.*

---

## 🚀 Quick Start

### 1. Prerequisites

- Python `>= 3.10`
- [uv](https://github.com/astral-sh/uv) (recommended) or standard Python `venv`

### 2. Environment Setup & Testing

Clone the repository and run the test suite:

```bash
git clone https://github.com/suharvest/edge-parking-vision.git
cd edge-parking-vision

# Install dev dependencies (pytest, jsonschema, pyyaml, onnx)
uv sync

# Run the full test suite (480+ unit and contract tests)
uv run pytest
```

Expected output:
```text
======================= 482 passed, 11 skipped in ~30s =======================
```

### 3. Verify Stdlib-Only Constraint

Verify that the core production modules remain strictly decoupled from third-party dependencies:

```bash
uv run python tools/check_stdlib_only.py core-py/core_parking contracts/validate_payload.py
```

### 4. Running the Telemetry & Dashboard Stack

An optional telemetry stack (Mosquitto + Telegraf + InfluxDB 2 + Grafana) is provided in `backend/`:

```bash
# Start the monitoring stack
MQTT_HOST=broker.local docker compose -f backend/docker-compose.yml up -d
```

- Open `http://localhost:3000` in your browser (Default credentials: `demo` / `demodemo`).
- The pre-provisioned **Parking Occupancy Dashboard** (`parking-occupancy.json`) will visualize incoming occupancy and passage metrics automatically.

---

## 🌐 HTTP Endpoints & Web Tools

The built-in HTTP server (`http_app.py`) runs on port `8080` (configurable via `app.options.http.port`):

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/events?limit=50` | Returns recent parking events in chronological order. |
| `GET` | `/preview` | Live web preview displaying bounding boxes, crossing lines, or parking slot statuses. |
| `GET` | `/counting/editor` | **Tripwire Editor**: Visual interface to drag counting lines and invert in/out directions. |
| `GET` | `/slots/editor` | **Slot ROI Editor**: Interactive canvas tool to draw, name, and calibrate parking polygons. |
| `GET` | `/preview.jpg?stream=<id>` | Latest JPEG camera snapshot (rate-limited to ≤ 1 req/s per stream). |
| `GET` | `/preview.json?stream=<id>` | Latest normalized detection bounding boxes, active lines, and slot/plate metadata. |
| `GET` | `/occupancy` | Direct JSON endpoint for LED signage boards or barrier gate displays. |
| `POST` | `/occupancy` | Manual occupancy override (`{"occupancy": 45}`). |
| `GET/PUT`| `/config/counting` | Read or hot-reload counting line parameters without service restarts. |
| `GET/PUT`| `/config/slots` | Read or hot-reload parking bay polygon geometries live. |
| `GET` | `/snapshot/{stream}.jpg` | High-resolution background snapshot used by the slot editor. |

---

## 📡 MQTT Event Contracts

All events are wrapped in a standard event envelope (`contracts/envelope.schema.json`) published to:
```text
<site_id>/parking/<camera_id>/<kind>
```

### 1. Vehicle Line Crossing (`parking.crossing/1`)
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

### 2. Parking Lot Occupancy (`parking.occupancy/1`, Retained)
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

### 3. License Plate Read (`parking.plate/1`)
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

### 4. Barrier Gate Operation (`parking.gate/1`)
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

### 5. Parking Slot Status Map (`parking.slots/1`, Retained)
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

## 📂 Repository Structure

```text
edge-parking-vision/
├── contracts/             # Source-of-truth JSON Schemas & payload validators
│   ├── app-options.schema.json
│   ├── crossing-event.schema.json
│   ├── envelope.schema.json
│   ├── gate-config.schema.json
│   ├── occupancy.schema.json
│   ├── plate-event.schema.json
│   ├── slots.schema.json
│   └── validate_payload.py
├── core-py/core_parking/  # Pure Python stdlib application runtime
│   ├── config.py          # Option validation & ParkingApp lifecycle assembly
│   ├── envelope.py        # Envelope generation & monotonic seq allocator
│   ├── hooks_counting.py  # Vehicle line crossing & occupancy accounting
│   ├── http_app.py        # REST endpoints & static web editor handler
│   ├── occupancy.py       # Parking state machine & rate limiter
│   ├── spool.py           # Crash-safe disk buffer queue
│   ├── transport.py       # MQTT client with retry and replay logic
│   ├── plate/             # Chinese LPR domain, allowlist & gate controller
│   │   ├── charset.py     # 66 Chinese license plate character definitions
│   │   ├── gate.py        # Barrier gate control & relay/ONVIF trigger
│   │   ├── hooks_plate.py # Plate detection/recognition hooks & voting
│   │   └── whitelist.py   # In-memory and persistent plate allowlists
│   └── slots/             # Multi-space parking bay occupancy monitoring
│       ├── app.py         # SlotsApp lifecycle & endpoints
│       ├── editor.html    # In-browser interactive polygon ROI canvas
│       └── hooks_slots.py # Geometry collision & debounce filter
├── core-cpp/              # C++ implementations for low-power embedded cameras
│   └── plate/             # C++ plate domain normalization & disk spooling
├── platforms/             # Platform deployment configurations & packaging
│   ├── cpu/               # ONNX Runtime CPU presets
│   ├── docker/            # Jetson native Dockerfile and packaging guides
│   ├── jetson/            # NVIDIA Jetson presets & TensorRT conversion
│   ├── rk/                # Rockchip RKNN conversion scripts & docs
│   ├── recamera-2002/     # Seeed reCamera SG2002 RISC-V deb build scripts
│   └── host-mounts/       # Docker volume mounting profiles per device
├── training/              # Model training, dataset synthesis & export tools
│   ├── platerec.py        # PlateRecNet CNN-CTC model architecture
│   ├── train_platerec.py  # Training pipeline with axisctx jitter crops
│   ├── plate_synth.py     # Realistic Chinese license plate image synthesizer
│   ├── export_platerec.py # Static ONNX exporter (opset 11)
│   └── convert_platerec_rknn_fp16out.py # RKNN hybrid quantization converter
├── evaluation/            # End-to-end evaluation & stress benchmarks
│   ├── plate_e2e_eval.py  # Full-pipeline evaluation on CCPD datasets
│   ├── stress_slots.py    # Multi-stream slot FPS & stability stress tester
│   └── results/           # Measured evaluation records across all hardware
├── backend/               # Optional demo visualization stack
│   ├── docker-compose.yml # Telegraf + InfluxDB + Grafana compose
│   └── grafana/           # Pre-configured parking occupancy dashboards
├── tools/                 # Validation & preflight automation scripts
│   ├── check_stdlib_only.py  # Linter enforcing zero third-party dependencies
│   ├── compare_events.py     # Ground-truth event stream parity comparator
│   └── build_overlay_images.sh # Image overlay preflight packager
└── tests/                 # Comprehensive test suite (480+ unit tests)
```

---

## 🛠️ Model Training & Export

### 1. Plate Recognition Model Training (`PlateRecNet`)

PlateRecNet is a specialized LPRNet-inspired CNN-CTC neural network operating on `96×32` inputs across 66 character classes.

To train with **axis-context crops (`axisctx`)** matching runtime detection jitter:

```bash
# 1. Pre-generate aligned axis context crops from CCPD
python training/gen_platerec_axis_crops.py \
  --det models/plate_det_yolox_tiny_640.onnx \
  --exclude-set evaluation/data/plate_e2e/eval_set.jsonl \
  --out data/processed/platerec_axisctx

# 2. Train PlateRecNet with CTC loss and cosine LR annealing
python training/train_platerec.py \
  --crop-mode axisctx \
  --axisctx-dir data/processed/platerec_axisctx \
  --epochs 60 \
  --batch-size 256 \
  --out training/outputs/platerec_cn_axis

# 3. Export to static ONNX
python training/export_platerec.py \
  --ckpt training/outputs/platerec_cn_axis/best.pth \
  --out plate_rec_cn_96x32.onnx
```

### 2. Rockchip RKNN Conversion

To convert the vehicle or license plate detection model to RKNN format:

```bash
python platforms/rk/convert_rknn.py \
  --onnx models/yolox_tiny_coco_416.onnx \
  --calib-list evaluation/data/calib/vehicle_416.txt \
  --size 416 \
  --target rk3588 \
  --out-dir dist/vehicle416-rk3588
```

---

## 📜 Licenses & Attribution

The source code of **edge-parking-vision** is licensed under the [Apache License 2.0](LICENSE).

### Third-Party Components & Datasets

- **CCPD Dataset**: License plate detection and recognition models leverage the [CCPD Dataset](https://github.com/detectRecog/CCPD) (Xu et al., ECCV 2018), released under the [MIT License](licenses/CCPD-MIT.txt).
- **YOLOX**: Object detection architectures and export scripts are derived from [Megvii-BaseDetection/YOLOX](https://github.com/Megvii-BaseDetection/YOLOX), licensed under Apache-2.0.
- **COCO 2017**: Pre-trained vehicle detection weights derive from COCO annotations (CC BY 4.0).
- Detailed licensing, hardware SDK notices, and attribution details are documented in [NOTICE](NOTICE) and [LICENSES.md](LICENSES.md).
