#!/usr/bin/env bash
# Build thin overlay images: published nrd-parking-{jetson,rk} base + core_parking files from SRC_COMMIT.
#
# Usage: tools/build_overlay_images.sh <src_commit> <new_tag> [workdir]
#   e.g. tools/build_overlay_images.sh e5f9dec 20261009
#
# Files are taken from `git archive <src_commit>` (never the working tree):
#   REPLACE  — copied as-is from src_commit (the base image carries an older committed version)
#   PATCH    — the base image carries uncommitted additions in this file, so the committed
#              delta PATCH_FROM..src_commit is applied on top of the file extracted from the base
#              image instead of replacing it (keeps e.g. the /snapshots/ route of the plate preset).
# Build host must be linux/arm64 (spark). No network during build; base images must be local.
set -euo pipefail

SRC_COMMIT=${1:?src commit}
NEW_TAG=${2:?new tag}
WORK=${3:-$(mktemp -d)}
REPO=$(cd "$(dirname "$0")/.." && pwd)
REG=sensecraft-missionpack.seeed.cn/solution
PATCH_FROM=f55b1ab   # committed state of http_app.py inside the 20261008 images

REPLACE="hooks_counting.py slots/editor.html static/preview.html static/counting_editor.html"
PATCH="http_app.py"

# Guard: nothing outside the overlay set (and no dependency file) changed since PATCH_FROM.
changed=$(git -C "$REPO" diff --name-only "$PATCH_FROM" "$SRC_COMMIT" -- core-py pyproject.toml uv.lock platforms/docker/Dockerfile)
for f in $changed; do
  rel=${f#core-py/core_parking/}
  case " $REPLACE $PATCH " in *" $rel "*) ;; *) echo "unexpected change outside overlay set: $f" >&2; exit 2;; esac
done

mkdir -p "$WORK/src"
git -C "$REPO" archive "$SRC_COMMIT" core-py/core_parking | tar -x -C "$WORK/src"
git -C "$REPO" diff "$PATCH_FROM" "$SRC_COMMIT" -- $(for f in $PATCH; do echo "core-py/core_parking/$f"; done) > "$WORK/http.patch"

for plat in jetson rk; do
  base=$(sed -n 's/^ARG BASE_IMAGE=//p' "$REPO/platforms/docker/overlay/Dockerfile.$plat")
  ctx="$WORK/ctx-$plat"
  rm -rf "$ctx"; mkdir -p "$ctx/core_parking"
  for f in $REPLACE; do
    mkdir -p "$ctx/core_parking/$(dirname "$f")"
    cp "$WORK/src/core-py/core_parking/$f" "$ctx/core_parking/$f"
  done
  cid=$(docker create "$base")
  for f in $PATCH; do
    mkdir -p "$ctx/stage/core-py/core_parking/$(dirname "$f")"
    docker cp "$cid:/opt/edge-parking/core_parking/$f" "$ctx/stage/core-py/core_parking/$f"
  done
  docker rm "$cid" >/dev/null
  patch -p1 --forward --no-backup-if-mismatch -d "$ctx/stage" < "$WORK/http.patch"
  for f in $PATCH; do cp "$ctx/stage/core-py/core_parking/$f" "$ctx/core_parking/$f"; done
  rm -rf "$ctx/stage"
  cp "$REPO/platforms/docker/overlay/Dockerfile.$plat" "$ctx/Dockerfile"
  (cd "$ctx" && find core_parking -type f | sort | xargs sha256sum) > "$WORK/files-$plat.sha256"
  docker build --pull=false --network=none --build-arg SRC_COMMIT="$(git -C "$REPO" rev-parse "$SRC_COMMIT")" \
    -t "$REG/nrd-parking-$plat:$NEW_TAG" "$ctx"
done
echo "context + file hashes: $WORK"
