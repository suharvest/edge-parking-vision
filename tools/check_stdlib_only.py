#!/usr/bin/env python3
"""机器检查：目标目录/文件只允许 import 标准库 + vision_base + core_parking（§S1）。

用 ast 收集每个 .py 文件的顶层（module level）import，顶层模块名必须落在
sys.stdlib_module_names ∪ {vision_base, core_parking} 内；命中其他模块即打印
file:line 并以退出码 1 结束。相对 import（from . import x）总是允许。

用法: uv run python tools/check_stdlib_only.py <path> [<path> ...]
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

EXTRA_ALLOWED = {"vision_base", "core_parking"}


def _allowed_roots() -> set[str]:
    return set(sys.stdlib_module_names) | EXTRA_ALLOWED


def _check_file(path: Path, allowed: set[str]) -> list[tuple[str, int, str]]:
    """返回 [(file, line, module)] 违规清单。只检查顶层 import（函数体内不查，
    与 §S1「收集顶层 import」一致）。"""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as exc:
        return [(str(path), exc.lineno or 0, f"<syntax error: {exc.msg}>")]
    violations: list[tuple[str, int, str]] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in allowed:
                    violations.append((str(path), node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                continue  # 相对 import，包内引用
            root = (node.module or "").split(".")[0]
            if root and root not in allowed:
                violations.append((str(path), node.lineno, node.module or ""))
    return violations


def _iter_py(path: Path):
    if path.is_file() and path.suffix == ".py":
        yield path
    elif path.is_dir():
        yield from sorted(path.rglob("*.py"))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(f"用法: {argv[0]} <path> [<path> ...]", file=sys.stderr)
        return 2
    allowed = _allowed_roots()
    all_violations: list[tuple[str, int, str]] = []
    for arg in argv[1:]:
        p = Path(arg)
        if not p.exists():
            print(f"路径不存在: {arg}", file=sys.stderr)
            return 2
        for py in _iter_py(p):
            all_violations.extend(_check_file(py, allowed))
    if all_violations:
        for f, line, mod in all_violations:
            print(f"{f}:{line}: 非允许的 import: {mod}")
        return 1
    print(f"OK: {len(argv) - 1} 个路径只使用标准库 + {sorted(EXTRA_ALLOWED)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
