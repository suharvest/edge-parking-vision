#!/usr/bin/env python3
"""读取 platforms/base-images.lock.yaml（规格 §S11.2 / §S11.5、§8.1 C4）。

用法：
  uv run python tools/lock_get.py base <platform> [--lock platforms/base-images.lock.yaml]

输出 `<ref 去 tag>@<digest>`（§S11.5 构建脚本直接用作 docker --build-arg BASE）。
digest 缺失或为「需核实」时报错退出 1（X0 完成前的预期状态，见 §8.0 X0 行）。

<platform> 为锁文件 bases 段的键（cpu / jetson / rk / hailo）。
"""

from __future__ import annotations

import argparse
import re
import sys

import yaml

PLACEHOLDER = "需核实"


def base_ref(lock_path: str, platform: str) -> str:
    try:
        with open(lock_path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except OSError as e:
        raise SystemExit(f"lock_get: 无法读取锁文件 {lock_path}: {e}")
    bases = (doc or {}).get("bases")
    if not isinstance(bases, dict) or platform not in bases:
        known = ", ".join(sorted(bases)) if isinstance(bases, dict) else "(无)"
        raise SystemExit(f"lock_get: 锁文件中无 bases.{platform}（已有: {known}）")
    entry = bases[platform]
    ref, digest = entry.get("ref", ""), entry.get("digest", "")
    if not ref or not isinstance(ref, str):
        raise SystemExit(f"lock_get: bases.{platform}.ref 缺失")
    if not digest or digest == PLACEHOLDER:
        raise SystemExit(
            f"lock_get: bases.{platform}.digest 为「{digest or '缺失'}」，"
            "需先完成 X0（从 retail-vision 锁文件填入）"
        )
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise SystemExit(f"lock_get: bases.{platform}.digest 格式非法: {digest!r}")
    ref_no_tag = re.sub(r":[^:@/]+$", "", ref)
    return f"{ref_no_tag}@{digest}"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("what", choices=["base"])
    ap.add_argument("platform")
    ap.add_argument("--lock", default="platforms/base-images.lock.yaml")
    args = ap.parse_args(argv)
    print(base_ref(args.lock, args.platform))
    return 0


if __name__ == "__main__":
    sys.exit(main())
