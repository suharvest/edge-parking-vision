"""P1 回归：CTC input_lengths 必须是 T（=24），不能传 target 长度。

背景（Codex 评审 BLOCKER）：训练脚本曾把 target_lengths 误当 input_lengths 传给
nn.CTCLoss，对齐窗口被掐到 target 长度 → 含相邻重复字符的车牌（如 皖AD18887）
不可行，zero_infinity=True 把 loss 记为 0 静默丢弃。本文件固定正确写法 vs 错误
写法的行为差异，防止回归。
"""
from __future__ import annotations

import math
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")
torch.nn = pytest.importorskip("torch.nn")

sys_path = str(Path(__file__).resolve().parents[1] / "training")
import sys  # noqa: E402

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

from platerec import PlateRecNet, cn_charset  # noqa: E402
from train_platerec import ctc_loss  # noqa: E402

T = 24


def _log_probs_for(plate: str, seed: int = 0):
    torch.manual_seed(seed)
    chars = cn_charset()
    char2idx = {c: i for i, c in enumerate(chars)}
    net = PlateRecNet(len(chars)).eval()  # 随机初始化即可
    x = torch.rand(1, 3, 32, 96)
    with torch.no_grad():
        logits = net(x).squeeze(2)  # (N,C,T)
    log_probs = logits.log_softmax(1).permute(2, 0, 1)  # (T,N,C)
    labels = torch.tensor([char2idx[c] for c in plate], dtype=torch.long)
    target_lengths = torch.tensor([len(plate)], dtype=torch.long)
    return log_probs, labels, target_lengths


def test_ctc_loss_finite_positive_with_adjacent_repeats():
    """正确写法（input_lengths=T）：含相邻重复字符的样本 loss 有限且 > 0。"""
    log_probs, labels, tl = _log_probs_for("皖AD18887")  # 相邻重复 "88"
    ctc = torch.nn.CTCLoss(blank=0, zero_infinity=True)
    loss = ctc_loss(ctc, log_probs, labels, tl)
    assert math.isfinite(float(loss)), float(loss)
    assert float(loss) > 0.0, float(loss)


def test_wrong_input_lengths_zero_loss_counterexample():
    """反例固定：input_lengths 误传 target 长度 → 重复样本不可行，loss 记 0。"""
    log_probs, labels, tl = _log_probs_for("皖AD18887")
    ctc = torch.nn.CTCLoss(blank=0, zero_infinity=True)
    bad = ctc(log_probs, labels, tl.clone(), tl)  # 错误写法
    assert float(bad) == 0.0, float(bad)


def test_ctc_loss_rejects_target_longer_than_T():
    log_probs, labels, tl = _log_probs_for("皖AD18887")
    long_tl = torch.tensor([T + 1], dtype=torch.long)
    long_labels = torch.cat([labels, labels[:1]])
    ctc = torch.nn.CTCLoss(blank=0, zero_infinity=True)
    with pytest.raises(ValueError):
        ctc_loss(ctc, log_probs, long_labels, long_tl)
