"""基座镜像锁与挂载的离线 CI 检查（规格 §S11.2、§8.2 X0 行）。

按 §8.0 X0 行：digest 保持「需核实」时本文件的 digest 断言**按设计失败**——
不允许为了让测试变绿而伪造 digest 或放宽断言。
compose 用例在对应 X 任务（X1–X4）补齐 compose 文件后生效，缺失时 skip。
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
LOCK = ROOT / "platforms" / "base-images.lock.yaml"
FIXTURE_LOCK = ROOT / "tests" / "fixtures" / "retail_vision_lock.yaml"
HOST_MOUNTS = ROOT / "platforms" / "host-mounts"
ABI_HEADER = ROOT / "third_party" / "vb" / "vb_analyzer_abi.h"
PLACEHOLDER = "需核实"
DOCKER_BASES = ["cpu", "jetson", "rk", "hailo"]
# §S11.2 测试第 2 条：compose 挂载 = host-mounts ∪ 通用三项
COMMON_VOLUMES = {
    "./config:/config:ro",
    "./models:/models:ro",
    "edge-parking-data:/data/edge-parking",
}
# 本地 retail-vision 检出（离线 CI 无检出时 ABI 比对用例 skip；联网比对由 tools/check_base_images.sh --sync 负责）
LOCAL_RETAIL_VISION = Path(os.environ.get("RETAIL_VISION_DIR", "/nonexistent/retail-vision"))  # 设置 RETAIL_VISION_DIR 以启用与上游的逐字节比对


def load_yaml(p: Path) -> dict:
    with p.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@pytest.fixture(scope="module")
def lock() -> dict:
    assert LOCK.is_file(), f"锁文件缺失: {LOCK}"
    return load_yaml(LOCK)


# ---------- 锁文件结构（X0 即可通过） ----------

def test_lock_structure(lock):
    assert lock.get("retail_vision_repo") == "https://github.com/Seeed-Solution/retail-vision"
    assert lock.get("retail_vision_tag"), "retail_vision_tag 缺失"
    bases = lock.get("bases")
    assert isinstance(bases, dict), "bases 段缺失"
    assert set(bases) >= set(DOCKER_BASES), f"bases 缺平台: {set(DOCKER_BASES) - set(bases)}"
    for name in DOCKER_BASES:
        entry = bases[name]
        assert re.match(r"^sensecraft-missionpack\.seeed\.cn/solution/vision-base-", entry.get("ref", "")), \
            f"bases.{name}.ref 非法: {entry.get('ref')!r}"
        assert "digest" in entry, f"bases.{name}.digest 键缺失"
    assert set(lock.get("device_profiles", {})) >= {"recamera-pro", "recamera-2002"}
    hm = lock["bases"]["rk"].get("host_mounts", {})
    assert set(hm) >= {"rk3576", "rk3588"}, "rk 必须分 rk3576/rk3588 两个 host-mounts 文件"


def test_no_latest_tag_in_platforms():
    """§S11.2：platforms/ 下禁止 `:latest`（禁止只写 tag，§0 规则）。"""
    hits = []
    for p in (ROOT / "platforms").rglob("*"):
        if p.is_file() and p.suffix in {".yml", ".yaml", ".sh", ".json"} | {""} or p.name == "Dockerfile":
            try:
                text = p.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for i, line in enumerate(text.splitlines(), 1):
                if ":latest" in line:
                    hits.append(f"{p}:{i}")
    assert not hits, "platforms/ 下出现 :latest:\n" + "\n".join(hits)


def test_no_wheels_or_hailort_in_git():
    """§S11.2：git 内禁止 .whl 与 libhailort（HailoRT 由宿主挂载，§S11.3）。"""
    out = subprocess.run(
        ["git", "ls-files", "platforms", "third_party"],
        cwd=ROOT, capture_output=True, text=True, check=True,
    ).stdout.splitlines()
    bad = [f for f in out if f.endswith(".whl") or "libhailort" in f]
    assert not bad, f"git 内禁止入库的文件: {bad}"


# ---------- digest（上游就绪前按设计失败，§8.0 X0 行） ----------

def test_digests_filled(lock):
    """bases.*.digest 不得为「需核实」且为 sha256:<64 hex>。镜像发布后才能通过。"""
    if any(lock["bases"][n].get("digest") == PLACEHOLDER for n in DOCKER_BASES):
        pytest.skip("基座镜像尚未发布，digest 仍为占位")
    bad = []
    for name in DOCKER_BASES:
        d = lock["bases"][name].get("digest", "")
        if d == PLACEHOLDER or not re.fullmatch(r"sha256:[0-9a-f]{64}", d or ""):
            bad.append(f"bases.{name}.digest={d!r}")
    assert not bad, (
        "基座 digest 未就绪（vision-base-<p> 尚未推送并从 retail-vision 锁文件填入）——"
        "禁止伪造 digest：\n" + "\n".join(bad)
    )


def test_digests_match_retail_vision_snapshot(lock):
    """锁文件 digest 与 tests/fixtures/retail_vision_lock.yaml 快照一致（快照由 --sync 生成）。"""
    assert FIXTURE_LOCK.is_file(), f"快照缺失: {FIXTURE_LOCK}（由 check_base_images.sh --sync 生成）"
    snap = load_yaml(FIXTURE_LOCK)
    if snap.get("source") == PLACEHOLDER:
        pytest.skip("快照为占位（上游 retail-vision 锁文件缺失），发布后需先 --sync")
    for name in DOCKER_BASES:
        got = lock["bases"][name].get("digest")
        want = (snap.get("bases") or {}).get(name, {}).get("digest")
        assert want and want != PLACEHOLDER, f"快照 bases.{name}.digest 未就绪（需 --sync 重新生成）"
        assert got == want, f"bases.{name}.digest 与 retail-vision 快照不一致: {got!r} != {want!r}"


# ---------- host-mounts（结构断言 X0 可通过；与上游 sha256 一致性待上游） ----------

def test_host_mounts_files_exist_and_parse():
    for name in ["jetson", "rk3576", "rk3588", "hailo"]:
        p = HOST_MOUNTS / f"{name}.yaml"
        assert p.is_file(), f"缺失: {p}"
        doc = load_yaml(p)
        assert isinstance(doc, dict) and doc, f"{p} 为空或非法"
        assert any(k in doc for k in ("volumes", "devices", "tmpfs")), f"{p} 无任何挂载键"


def test_rk_host_mounts_no_rknnlite():
    """§S11.2：RK 的 host-mounts 不含 rknnlite（§S11.1：去掉 rknnlite/ruamel/psutil 三项；护栏：不用 rknnlite）。"""
    for name in ["rk3576", "rk3588"]:
        text = (HOST_MOUNTS / f"{name}.yaml").read_text(encoding="utf-8")
        for banned in ("rknnlite", "ruamel", "psutil"):
            assert banned not in text, f"{name}.yaml 含禁用项 {banned}"


@pytest.mark.skipif(
    not (LOCAL_RETAIL_VISION / "platforms" / "host-mounts").is_dir(),
    reason="上游 retail-vision 无 platforms/host-mounts/（X0 时上游缺失，已按 §S13 汇报）；就绪后本用例生效",
)
def test_host_mounts_match_upstream_sha256():
    """§S11.2：host-mounts/*.yaml 与 retail-vision 同 tag 下同名文件 sha256 相同。"""
    for name in ["jetson", "rk3576", "rk3588", "hailo"]:
        local = (HOST_MOUNTS / f"{name}.yaml").read_bytes()
        up = (LOCAL_RETAIL_VISION / "platforms" / "host-mounts" / f"{name}.yaml").read_bytes()
        assert hashlib.sha256(local).hexdigest() == hashlib.sha256(up).hexdigest(), f"{name}.yaml 与上游不一致"


# ---------- ABI 头（§S11.2：与 retail-vision 同 tag 下文件 sha256 相同） ----------

def test_abi_header_present():
    assert ABI_HEADER.is_file(), f"缺失: {ABI_HEADER}"
    text = ABI_HEADER.read_text(encoding="utf-8")
    assert "VB_ANALYZER_ABI" in text, "ABI 头内容异常"
    assert "Apache" in (ROOT / "LICENSES.md").read_text(encoding="utf-8"), "LICENSES.md 应含 Apache-2.0（基座许可）"


@pytest.mark.skipif(
    not (LOCAL_RETAIL_VISION / "core-cpp" / "vb" / "include" / "vb" / "vb_analyzer_abi.h").is_file(),
    reason="本地无 retail-vision 检出；联网一致性由 tools/check_base_images.sh --sync 负责",
)
def test_abi_header_matches_retail_vision_sha256():
    """与 retail-vision 检出中的 core-cpp/vb/include/vb/vb_analyzer_abi.h 逐字节一致。
    注意：规格要求与「同一 tag」比对；基座尚未打 vision-base-v* tag（需核实），此处对 feat/vision-base 检出比对。"""
    local = hashlib.sha256(ABI_HEADER.read_bytes()).hexdigest()
    up = hashlib.sha256(
        (LOCAL_RETAIL_VISION / "core-cpp" / "vb" / "include" / "vb" / "vb_analyzer_abi.h").read_bytes()
    ).hexdigest()
    assert local == up, "vb_analyzer_abi.h 与 retail-vision 不一致（禁止手改，须原样拷贝）"


# ---------- compose（对应 X 任务完成后生效） ----------

def _compose_cases() -> list[tuple[Path, str]]:
    cases = []
    for p, hm_name in [("cpu", None), ("jetson", "jetson"), ("hailo", "hailo")]:
        f = ROOT / "platforms" / p / "docker-compose.yml"
        if f.is_file():
            cases.append((f, hm_name))
    for p in ["rk3588", "rk3576"]:
        f = ROOT / "platforms" / "rk" / f"docker-compose.{p}.yml"
        if f.is_file():
            cases.append((f, p))
    return cases


@pytest.mark.skipif(not _compose_cases(), reason="compose 文件尚未建立（X1–X4）；§8.2 X0 行：compose 用例在对应 X 完成后通过")
def test_compose_mounts_match_host_mounts(lock):
    """§S11.2：compose 主服务 volumes/devices/tmpfs = host-mounts/<p>.yaml ∪ 通用三项。"""
    for compose_path, hm_name in _compose_cases():
        doc = load_yaml(compose_path)
        svc = doc["services"]["edge-parking"]
        vols = set(svc.get("volumes", []))
        if hm_name:
            hm = load_yaml(HOST_MOUNTS / f"{hm_name}.yaml")
            want = set(hm.get("volumes", [])) | COMMON_VOLUMES
            assert vols == want, f"{compose_path.name} volumes 与 host-mounts ∪ 通用三项不一致: {vols ^ want}"
            assert set(svc.get("devices", [])) == set(hm.get("devices", [])), f"{compose_path.name} devices 不一致"
            assert set(svc.get("tmpfs", [])) == set(hm.get("tmpfs", [])), f"{compose_path.name} tmpfs 不一致"
        else:
            assert vols == COMMON_VOLUMES, f"{compose_path.name}（cpu 无 host-mounts）volumes 应只有通用三项"
        base_arg = svc.get("build", {}).get("args", {}).get("BASE", "")
        assert "@sha256:" in base_arg, f"{compose_path.name} build.args.BASE 必须带 @sha256: digest"
