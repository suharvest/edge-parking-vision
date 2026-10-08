#!/usr/bin/env bash
# Build thin overlay images: published nrd-parking-{jetson,rk} base + core_parking files from SRC_COMMIT.
#
# Usage: tools/build_overlay_images.sh <src_commit> <new_tag> [workdir]
#   e.g. tools/build_overlay_images.sh <commit> 20261009b
#
# BASE_IMAGE comes from platforms/docker/overlay/Dockerfile.<plat> (pinned by digest);
# PATCH_FROM is the commit whose core_parking files that base image carries.
# Only files changed in PATCH_FROM..SRC_COMMIT go into the overlay, taken from
# `git archive <src_commit>` (never the working tree):
#   REPLACE — the image's copy must equal PATCH_FROM byte for byte (checked); copied as-is.
#   PATCH   — files listed in BASE_DIRTY: the base image carries uncommitted additions
#             (plate preset, /snapshots/ route, ...), so the committed delta
#             PATCH_FROM..SRC_COMMIT is applied on top of the file extracted from the image.
# Build host must be linux/arm64 (spark). No network during build; base images must be local.
#
# History: 20261009  = 20261008 base, PATCH_FROM f55b1ab, src e5f9dec
#          20261009b = 20261009 base, PATCH_FROM f94d37f (core-py identical to e5f9dec)
set -euo pipefail

SRC_COMMIT=${1:?src commit}
NEW_TAG=${2:?new tag}
WORK=${3:-$(mktemp -d)}
REPO=$(cd "$(dirname "$0")/.." && pwd)
REG=sensecraft-missionpack.seeed.cn/solution
PATCH_FROM=${PATCH_FROM:-f94d37f}   # committed core_parking state inside the 20261009 images
# Files that differ from every commit inside the published images (uncommitted additions).
BASE_DIRTY="http_app.py config.py envelope.py transport.py slots/app.py slots/hooks_slots.py"

REPLACE=""; PATCH=""
for f in $(git -C "$REPO" diff --name-only "$PATCH_FROM" "$SRC_COMMIT" -- core-py pyproject.toml uv.lock platforms/docker/Dockerfile); do
  case "$f" in
    core-py/core_parking/*) rel=${f#core-py/core_parking/} ;;
    *) echo "unexpected change outside core_parking: $f" >&2; exit 2 ;;
  esac
  [ -n "$(git -C "$REPO" ls-tree "$SRC_COMMIT" -- "$f")" ] || { echo "file removed in src: $f" >&2; exit 2; }
  case " $BASE_DIRTY " in *" $rel "*) PATCH="$PATCH $rel" ;; *) REPLACE="$REPLACE $rel" ;; esac
done
[ -n "$REPLACE$PATCH" ] || { echo "no core_parking change in $PATCH_FROM..$SRC_COMMIT" >&2; exit 2; }
echo "REPLACE:$REPLACE"; echo "PATCH:$PATCH"

mkdir -p "$WORK/src" "$WORK/from"
git -C "$REPO" archive "$SRC_COMMIT" core-py/core_parking | tar -x -C "$WORK/src"
git -C "$REPO" archive "$PATCH_FROM" core-py/core_parking | tar -x -C "$WORK/from"
: > "$WORK/core.patch"
[ -z "$PATCH" ] || git -C "$REPO" diff "$PATCH_FROM" "$SRC_COMMIT" -- $(for f in $PATCH; do echo "core-py/core_parking/$f"; done) > "$WORK/core.patch"

for plat in jetson rk; do
  base=$(sed -n 's/^ARG BASE_IMAGE=//p' "$REPO/platforms/docker/overlay/Dockerfile.$plat")
  ctx="$WORK/ctx-$plat"
  rm -rf "$ctx"; mkdir -p "$ctx/core_parking" "$ctx/stage/core-py/core_parking"
  cid=$(docker create "$base")
  for f in $REPLACE $PATCH; do
    mkdir -p "$ctx/stage/core-py/core_parking/$(dirname "$f")"
    docker cp "$cid:/opt/edge-parking/core_parking/$f" "$ctx/stage/core-py/core_parking/$f"
  done
  docker rm "$cid" >/dev/null
  for f in $REPLACE; do
    cmp -s "$WORK/from/core-py/core_parking/$f" "$ctx/stage/core-py/core_parking/$f" \
      || { echo "$plat: image copy of $f is not $PATCH_FROM; refusing to replace" >&2; exit 3; }
    mkdir -p "$ctx/core_parking/$(dirname "$f")"
    cp "$WORK/src/core-py/core_parking/$f" "$ctx/core_parking/$f"
  done
  if [ -n "$PATCH" ]; then
    patch -p1 --forward --no-backup-if-mismatch -d "$ctx/stage" < "$WORK/core.patch"
    for f in $PATCH; do
      mkdir -p "$ctx/core_parking/$(dirname "$f")"
      cp "$ctx/stage/core-py/core_parking/$f" "$ctx/core_parking/$f"
    done
  fi
  rm -rf "$ctx/stage"
  cp "$REPO/platforms/docker/overlay/Dockerfile.$plat" "$ctx/Dockerfile"
  (cd "$ctx" && find core_parking -type f | sort | xargs sha256sum) > "$WORK/files-$plat.sha256"
  docker build --pull=false --network=none --build-arg SRC_COMMIT="$(git -C "$REPO" rev-parse "$SRC_COMMIT")" \
    -t "$REG/nrd-parking-$plat:$NEW_TAG" "$ctx"
done
echo "context + file hashes: $WORK"
