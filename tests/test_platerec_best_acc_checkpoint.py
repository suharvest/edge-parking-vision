"""P6-A 回归：last_ckpt.pth 的 best_acc 必须包含本 epoch 的结果。

背景（Codex 第二轮 P2）：训练脚本先写 last_ckpt.pth、后更新 best。若被打断的
那一轮恰好是最新最好（如 0.80→0.90），checkpoint 里仍是旧 best，resume 后一个
较低分的 epoch 会覆盖真正最好的 best.pth。

本测试用 stub 的 evaluate 让**最后一个 epoch** 才是最高分，直接调用 main()，
断言 last_ckpt.pth['best_acc'] 已更新为本 epoch 的最大值（旧代码会落后一个 epoch）。
"""
from __future__ import annotations

from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

sys_path = str(Path(__file__).resolve().parents[1] / "training")
import sys  # noqa: E402

if sys_path not in sys.path:
    sys.path.insert(0, sys_path)

import train_platerec  # noqa: E402


def test_last_ckpt_best_acc_includes_final_improving_epoch(tmp_path, monkeypatch):
    vals = iter([0.20, 0.50, 0.90])
    monkeypatch.setattr(train_platerec, "evaluate", lambda *a, **k: (next(vals), 0.0))

    out = tmp_path / "out"
    rc = train_platerec.main([
        "--data-root", "/home/harvest/edge-parking-vision",
        "--epochs", "3", "--batch-size", "8", "--num-workers", "0",
        "--limit", "0", "--val-limit", "0", "--out", str(out),
    ])
    assert rc == 0

    ck = torch.load(out / "last_ckpt.pth", map_location="cpu", weights_only=False)
    assert ck["val_acc"] == 0.90
    assert ck["best_acc"] == 0.90, f"best_acc 落后一个 epoch: {ck['best_acc']}"
    assert ck["epoch"] == 2

    best = torch.load(out / "best_ckpt.pth", map_location="cpu", weights_only=False)
    assert best["val_acc"] == 0.90
