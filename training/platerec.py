#!/usr/bin/env python3
"""PlateRecNet 模型定义（NRD-3 §6.5，纯卷积 CTC 识别器，LPRNet 思路自写实现）。

输入 1×3×32×96（RGB，/255），输出 1×C×24（C = len(charset('cn'))，index 0 为 CTC
blank；不含 Softmax/LogSoftmax/Squeeze，导出图到此为止）。

结构（§6.5）：

    Conv3x3(64) BN ReLU → MaxPool(3×3, stride (1,1), padding 1)
    SmallBlock(128) → MaxPool(stride (2,1))
    SmallBlock(256) → SmallBlock(256) → MaxPool(stride (2,1))
    Dropout(0.3) → Conv(1×4, 256) BN ReLU → Dropout(0.3) → Conv(13×1, n_classes) BN ReLU
    global-context: 各层特征用固定 kernel/stride 的 AvgPool2d 池化到 T=24 列后
    concat → Conv1x1(n_classes)

与 §6.5 原文的两处形状适配（原文按 LPRNet 输入 24×94 写作 13×1）：
- 本实现输入高度 32，两次 stride-(2,1) 池化后高度为 8，故高度坍缩卷积取
  Conv(8×1, n_classes)（H×1 逐行卷积，语义与原文 13×1 相同：把特征高度坍缩为 1）；
- Conv(1×4, 256) 取 stride (1,4)、无 padding，使宽度 96 恰好降到 T=24。
global-context 对各层特征用固定 kernel 的 AvgPool2d 池化到 1×24 后 concat，
Conv1x1(n_classes) 得辅助头；两头相加（Add，§6.5 允许的节点类型）作为输出。

导出约束（§6.5）：静态形状、opset 11、无 AdaptiveAvgPool（上面全部是固定
kernel 的 AvgPool2d/MaxPool2d）、无 Softmax/LogSoftmax/Squeeze；BN 由
export_platerec.py 在导出前折叠。ONNX 节点类型只含
Conv/Relu/MaxPool/AveragePool/Concat/Add。
"""
from __future__ import annotations

import sys
from pathlib import Path

import torch
import torch.nn as nn


def _load_charset_cn() -> list[str]:
    """复用 core-py/core_parking/plate/charset.py（只读）。工作区副本优先。"""
    candidates = [
        Path(__file__).resolve().parents[1] / "core-py",
        Path("/home/harvest/project/edge-parking-vision/core-py"),
    ]
    for p in candidates:
        if (p / "core_parking" / "plate" / "charset.py").exists():
            if str(p) not in sys.path:
                sys.path.insert(0, str(p))
            break
    else:
        raise RuntimeError(f"charset.py not found, tried: {candidates}")
    from core_parking.plate.charset import charset

    return charset("cn")


def cn_charset() -> list[str]:
    """CTC 字符表（index 0 = blank），长度即模型类别数 C。"""
    return _load_charset_cn()


class SmallBlock(nn.Module):
    """§6.5 SmallBlock(c): Conv1x1(c/4) → Conv(3×1) → Conv(1×3) → Conv1x1(c)，均 BN+ReLU；带残差 Add。"""

    def __init__(self, in_c: int, c: int, shortcut: bool | None = None) -> None:
        super().__init__()
        if shortcut is None:
            shortcut = in_c == c
        self.shortcut = shortcut
        h = c // 4
        self.conv1 = nn.Conv2d(in_c, h, 1, bias=False)
        self.bn1 = nn.BatchNorm2d(h)
        self.conv2 = nn.Conv2d(h, h, (3, 1), padding=(1, 0), bias=False)
        self.bn2 = nn.BatchNorm2d(h)
        self.conv3 = nn.Conv2d(h, h, (1, 3), padding=(0, 1), bias=False)
        self.bn3 = nn.BatchNorm2d(h)
        self.conv4 = nn.Conv2d(h, c, 1, bias=False)
        self.bn4 = nn.BatchNorm2d(c)
        self.relu = nn.ReLU(inplace=False)

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # noqa: D102
        y = self.relu(self.bn1(self.conv1(x)))
        y = self.relu(self.bn2(self.conv2(y)))
        y = self.relu(self.bn3(self.conv3(y)))
        y = self.relu(self.bn4(self.conv4(y)))
        return y + x if self.shortcut else y  # Add（残差；通道数变化时无 shortcut）


class PlateRecNet(nn.Module):
    """输出 (N, C, 24)，CTC 贪心解码在基座 ctc_greedy（BASE-1 §6.1），本模型不含 Softmax。"""

    def __init__(self, n_classes: int) -> None:
        super().__init__()
        self.n_classes = n_classes
        self.stem = nn.Sequential(
            nn.Conv2d(3, 64, 3, 1, 1, bias=False),
            nn.BatchNorm2d(64),
            nn.ReLU(inplace=False),
            nn.MaxPool2d(3, stride=(1, 1), padding=1),
        )
        self.block1 = SmallBlock(64, 128)    # 通道变化 → 无残差
        self.pool1 = nn.MaxPool2d(kernel_size=(2, 1), stride=(2, 1))  # 32→16
        self.block2 = SmallBlock(128, 256)   # 通道变化 → 无残差
        self.block3 = SmallBlock(256, 256)   # 残差 Add
        self.pool2 = nn.MaxPool2d(kernel_size=(2, 1), stride=(2, 1))  # 16→8
        self.drop1 = nn.Dropout(0.3)
        # Conv(1×4, 256) stride (1,4)：宽 96 → T=24（见模块 docstring 适配说明）
        self.trunk_conv = nn.Conv2d(256, 256, (1, 4), stride=(1, 4), bias=False)
        self.trunk_bn = nn.BatchNorm2d(256)
        self.drop2 = nn.Dropout(0.3)
        # 高度坍缩：8×1 逐行卷积（原文 13×1 的等价适配，见 docstring）
        self.head = nn.Sequential(
            nn.Conv2d(256, n_classes, (8, 1), bias=False),
            nn.BatchNorm2d(n_classes),
        )
        # global-context：各层特征 AvgPool2d 到 1×24 后 concat → Conv1x1(n_classes)
        self.ctx_conv = nn.Conv2d(64 + 128 + 256 + 256 + 256, n_classes, 1, bias=False)
        self.ctx_bn = nn.BatchNorm2d(n_classes)

    def forward(self, x: torch.Tensor) -> torch.Tensor:  # noqa: D102
        f0 = self.stem(x)              # (64, 32, 96)
        f1 = self.block1(f0)           # (128, 32, 96)
        f2 = self.pool1(f1)            # (128, 16, 96)
        f3 = self.block2(f2)           # (256, 16, 96)
        f4 = self.block3(f3)           # (256, 16, 96)
        f5 = self.pool2(f4)            # (256, 8, 96)
        t = self.drop1(f5)
        t = torch.relu(self.trunk_bn(self.trunk_conv(t)))   # (256, 8, 24)
        t = self.drop2(t)
        main = self.head(t)            # (C, 1, 24)
        ctx = torch.cat(
            [
                nn.functional.avg_pool2d(f0, kernel_size=(32, 4)),  # (64, 1, 24)
                nn.functional.avg_pool2d(f1, kernel_size=(32, 4)),  # (128, 1, 24)
                nn.functional.avg_pool2d(f3, kernel_size=(16, 4)),  # (256, 1, 24)
                nn.functional.avg_pool2d(f5, kernel_size=(8, 4)),   # (256, 1, 24)
                nn.functional.avg_pool2d(t, kernel_size=(8, 1)),    # (256, 1, 24)
            ],
            dim=1,
        )
        aux = self.ctx_bn(self.ctx_conv(ctx))  # (C, 1, 24)
        return main + aux  # (N, C, 24)


if __name__ == "__main__":
    chars = cn_charset()
    net = PlateRecNet(len(chars)).eval()
    with torch.no_grad():
        y = net(torch.randn(1, 3, 32, 96))
    print(f"charset C={len(chars)}, output {tuple(y.shape)}")
    # 导出图无 Squeeze/Reshape（§6.5 允许节点类型限制），高度由 (8,1) 卷积坍缩为 1，
    # 即规格 “1×C×24”（去 batch 后 [C, T]，H 维为 1）。
    assert y.shape == (1, len(chars), 1, 24)
