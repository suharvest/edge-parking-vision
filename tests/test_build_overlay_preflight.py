"""tools/build_overlay_images.sh PREFLIGHT_ONLY=1: ADD / REPLACE / PATCH classification (no docker)."""
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "build_overlay_images.sh"
PATCH_FROM = "f94d37f"
SRC = "7fb89e9"


def _has(commit: str) -> bool:
    return subprocess.run(["git", "-C", str(ROOT), "cat-file", "-e", f"{commit}^{{commit}}"],
                          capture_output=True).returncode == 0


@pytest.mark.skipif(not (_has(PATCH_FROM) and _has(SRC)), reason="needs full git history")
def test_preflight_classifies_added_replaced_patched_files():
    env = {**os.environ, "PREFLIGHT_ONLY": "1", "PATCH_FROM": PATCH_FROM}
    r = subprocess.run(["bash", str(SCRIPT), SRC, "t"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    lines = dict(l.split(":", 1) for l in r.stdout.splitlines() if l.split(":", 1)[0] in ("ADD", "REPLACE", "PATCH"))
    assert lines["ADD"].split() == ["plate/gate.py", "plate/hooks_plate.py"]
    assert lines["REPLACE"].split() == ["hooks_counting.py"]
    assert set(lines["PATCH"].split()) == {"config.py", "envelope.py", "http_app.py",
                                           "slots/app.py", "slots/hooks_slots.py", "transport.py"}
    assert "vision_base" not in r.stdout


def test_preflight_rejects_unknown_src_commit():
    env = {**os.environ, "PREFLIGHT_ONLY": "1"}
    r = subprocess.run(["bash", str(SCRIPT), "does-not-exist", "t"], cwd=ROOT, env=env,
                       capture_output=True, text=True)
    assert r.returncode != 0
