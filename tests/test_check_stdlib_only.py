"""tools/check_stdlib_only.py 的自检（§8.1 C0 / §12 第 6 条）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools" / "check_stdlib_only.py"


def _run(path: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(TOOL), str(path)],
        capture_output=True,
        text=True,
    )


def test_rejects_third_party_import(tmp_path: Path) -> None:
    bad = tmp_path / "bad.py"
    bad.write_text("import numpy\n", encoding="utf-8")
    proc = _run(tmp_path)
    assert proc.returncode == 1
    assert "bad.py:1" in proc.stdout
    assert "numpy" in proc.stdout


def test_accepts_stdlib_and_vision_base(tmp_path: Path) -> None:
    ok = tmp_path / "ok.py"
    ok.write_text(
        "import json\nimport sys\nfrom pathlib import Path\n"
        "import vision_base\nimport core_parking\nfrom . import sibling\n",
        encoding="utf-8",
    )
    proc = _run(tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr


def test_rejects_from_import_third_party(tmp_path: Path) -> None:
    bad = tmp_path / "bad2.py"
    bad.write_text("from paho.mqtt import client\n", encoding="utf-8")
    proc = _run(tmp_path)
    assert proc.returncode == 1
    assert "paho.mqtt" in proc.stdout


def test_core_parking_and_validate_payload_pass() -> None:
    proc = subprocess.run(
        [sys.executable, str(TOOL), str(ROOT / "core-py" / "core_parking"),
         str(ROOT / "contracts" / "validate_payload.py")],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
