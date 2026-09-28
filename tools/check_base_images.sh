#!/usr/bin/env bash
# 基座镜像锁核对（规格 §S11.2）。联网工具，Mac 或 spark 上运行。
#
#   bash tools/check_base_images.sh          # 对锁文件每个 bases.* 跑 imagetools inspect，digest 与注册表不一致退出 1
#   bash tools/check_base_images.sh --sync   # 从 retail_vision_tag 拉取 retail-vision 锁文件、host-mounts/、
#                                            # vb_analyzer_abi.h 覆盖本仓库副本（规格 §S11.2 升级基座流程第 2 步）
#
# X0 状态：digest 保持「需核实」、retail-vision 尚无 vision-base-v* tag 与 platforms/ 目录。
# 两种模式此时都按设计退出 1（§8.0 X0 行），输出说明缺什么；禁止为了退出 0 而伪造 digest。
set -u
cd "$(dirname "$0")/.."
LOCK=platforms/base-images.lock.yaml
SNAPSHOT=tests/fixtures/retail_vision_lock.yaml
PY="uv run python"

fail() { echo "check_base_images: FAIL: $*" >&2; exit 1; }

[ -f "$LOCK" ] || fail "锁文件缺失: $LOCK"

read_lock() { # read_lock <python-表达式， doc 为锁文件 dict>
  $PY - "$LOCK" <<'PYEOF' "$1"
import sys, yaml
with open(sys.argv[1], encoding="utf-8") as f:
    doc = yaml.safe_load(f)
print(eval(sys.argv[2]))
PYEOF
}

if [ "${1:-}" = "--sync" ]; then
  TAG=$(read_lock "doc.get('retail_vision_tag')")
  REPO=$(read_lock "doc.get('retail_vision_repo')")
  [ "$TAG" != "需核实" ] || fail "retail_vision_tag 为「需核实」：基座尚未打 vision-base-v* tag，无法 --sync（上游缺失，见 §S13 汇报）"
  case "$TAG" in vision-base-v*) ;; *) fail "retail_vision_tag 应为 vision-base-v<semver>，实际: $TAG";; esac
  TMP=$(mktemp -d); trap 'rm -rf "$TMP"' EXIT
  echo "check_base_images: 拉取 $REPO @ $TAG ..."
  git clone --quiet --depth 1 --branch "$TAG" "$REPO" "$TMP/retail-vision" \
    || fail "clone $REPO @ $TAG 失败（tag 不存在或网络不可达）"
  missing=0
  for f in platforms/base-images.lock.yaml core-cpp/vb/include/vb/vb_analyzer_abi.h \
           platforms/host-mounts/jetson.yaml platforms/host-mounts/rk3576.yaml \
           platforms/host-mounts/rk3588.yaml platforms/host-mounts/hailo.yaml; do
    [ -f "$TMP/retail-vision/$f" ] || { echo "check_base_images: 上游缺失 $TAG:$f" >&2; missing=1; }
  done
  [ "$missing" = 0 ] || fail "上游文件不全，未覆盖本仓库任何副本（按 §S13 汇报上游缺失）"
  cp "$TMP/retail-vision/platforms/base-images.lock.yaml" "$SNAPSHOT"
  cp "$TMP/retail-vision/core-cpp/vb/include/vb/vb_analyzer_abi.h" third_party/vb/vb_analyzer_abi.h
  for p in jetson rk3576 rk3588 hailo; do
    cp "$TMP/retail-vision/platforms/host-mounts/$p.yaml" "platforms/host-mounts/$p.yaml"
  done
  # 用上游锁文件快照里的 digest 填回本仓库锁文件（只允许 digest 与 tag 变化，其余结构不动）
  $PY - "$LOCK" "$SNAPSHOT" "$TAG" <<'PYEOF'
import re, sys
lock_p, snap_p, tag = sys.argv[1], sys.argv[2], sys.argv[3]
import yaml
with open(snap_p, encoding="utf-8") as f:
    snap = yaml.safe_load(f)
src = open(lock_p, encoding="utf-8").read()
src = re.sub(r"^retail_vision_tag:.*$", f"retail_vision_tag: {tag}", src, count=1, flags=re.M)
for name, entry in (snap.get("bases") or {}).items():
    d = entry.get("digest")
    if d:
        src = re.sub(rf"({name}:\s*{{[^}}]*?digest:\s*)\"[^\"]*\"", rf"\g<1>\"{d}\"", src)
open(lock_p, "w", encoding="utf-8").write(src)
print("check_base_images: 锁文件 digest 已从快照填回")
PYEOF
  echo "check_base_images: --sync 完成（快照 → $SNAPSHOT；host-mounts/ 与 ABI 头已覆盖）"
  exit 0
fi

# 普通模式：逐 bases.* 用 imagetools inspect 比对 digest
$PY - "$LOCK" <<'PYEOF'
import subprocess, sys, re, yaml
PLACEHOLDER = "需核实"
with open(sys.argv[1], encoding="utf-8") as f:
    doc = yaml.safe_load(f)
bases = doc.get("bases") or {}
rc, pending = 0, []
for name in ["cpu", "jetson", "rk", "hailo"]:
    entry = bases.get(name) or {}
    ref, digest = entry.get("ref", ""), entry.get("digest", "")
    if not digest or digest == PLACEHOLDER or "需核实" in ref:
        pending.append(name)
        print(f"check_base_images: {name}: digest/ref 为「需核实」——vision-base-{name} 尚未推送，按 §8.0 X0 行设计失败")
        rc = 1
        continue
    ref_no_tag = re.sub(r":[^:@/]+$", "", ref)
    print(f"check_base_images: {name}: docker buildx imagetools inspect {ref} ...")
    r = subprocess.run(["docker", "buildx", "imagetools", "inspect", ref],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"check_base_images: {name}: inspect 失败: {r.stderr.strip()}", file=sys.stderr)
        rc = 1
        continue
    m = re.search(r"^Digest:\s*(\S+)", r.stdout, re.M)
    got = m.group(1) if m else None
    want = f"{ref_no_tag}@{digest}".split("@", 1)[1]
    if got != digest:
        print(f"check_base_images: {name}: digest 不一致 registry={got} lock={digest}", file=sys.stderr)
        rc = 1
    else:
        print(f"check_base_images: {name}: OK {digest}")
if rc:
    if pending:
        print("check_base_images: 待就绪平台: " + ", ".join(pending) +
              "（镜像推送并写入 retail-vision 锁文件后跑 --sync）", file=sys.stderr)
    sys.exit(1)
print("check_base_images: 全部 bases digest 与注册表一致")
PYEOF
