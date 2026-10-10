#!/bin/sh
set -eu
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
VERSION=; RETAIL_VISION=; RECAMERA=; RUNTIME_STAGE=; VB_RUNTIME=; OUT=${OUT:-"$ROOT/dist"}
VB_RUNTIME_SHA256=${VB_RUNTIME_SHA256:-8834c5b00d8175fed9673949711186782b62546d99458f7537581fb39c52b286}
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-0}
usage(){ echo "usage: $0 --version V --retail-vision DIR --recamera DIR --python-staging DIR --vb-runtime FILE [--out DIR]" >&2; exit 2; }
while [ "$#" -gt 0 ]; do
  case "$1" in
    --version) VERSION=$2; shift 2;; --retail-vision) RETAIL_VISION=$2; shift 2;; --recamera) RECAMERA=$2; shift 2;; --python-staging) RUNTIME_STAGE=$2; shift 2;; --vb-runtime) VB_RUNTIME=$2; shift 2;; --out) OUT=$2; shift 2;; *) usage;;
  esac
done
[ -n "$VERSION" ] && [ -n "$RETAIL_VISION" ] && [ -n "$RECAMERA" ] && [ -n "$RUNTIME_STAGE" ] && [ -n "$VB_RUNTIME" ] || usage
[ -d "$RETAIL_VISION/core-py/vision_base" ] || { echo "retail-vision vision_base missing" >&2; exit 1; }
[ -d "$ROOT/../../core-py/core_parking" ] || { echo "core_parking source missing" >&2; exit 1; }
[ -x "$VB_RUNTIME" ] || { echo "vb-runtime missing or not executable" >&2; exit 1; }
[ "$(sha256sum "$VB_RUNTIME" | awk '{print $1}')" = "$VB_RUNTIME_SHA256" ] || { echo "vb-runtime SHA mismatch" >&2; exit 1; }
[ -x "$RUNTIME_STAGE/opt/vb/python/bin/python3.11" ] || { echo "Python 3.11 target staging missing" >&2; exit 1; }
ARCH=$(readelf -h "$VB_RUNTIME" | awk -F: '/Machine:/{gsub(/^ +| +$/,"",$2); print $2}')
[ "$ARCH" = "RISC-V" ] || { echo "vb-runtime is not RISC-V: $ARCH" >&2; exit 1; }
PY_ARCH=$(readelf -h "$RUNTIME_STAGE/opt/vb/python/bin/python3.11" | awk -F: '/Machine:/{gsub(/^ +| +$/,"",$2); print $2}')
[ "$PY_ARCH" = "RISC-V" ] || { echo "Python runtime is not RISC-V: $PY_ARCH" >&2; exit 1; }
mkdir -p "$OUT"
PKG="$OUT/edge-parking-plate_$VERSION-riscv64"
[ ! -e "$PKG" ] || { echo "refusing existing package directory: $PKG" >&2; exit 2; }
mkdir "$PKG"
mkdir -p "$PKG/DEBIAN" "$PKG/opt/vb/bin" "$PKG/opt/vb/py/vision_base" "$PKG/opt/vb/python" "$PKG/opt/edge-parking/core_parking" "$PKG/etc/init.d" "$PKG/etc/edge-parking-plate"
cp "$VB_RUNTIME" "$PKG/opt/vb/bin/vb-runtime"; cp -a "$RUNTIME_STAGE/opt/vb/python/." "$PKG/opt/vb/python/"; cp -a "$RETAIL_VISION/core-py/vision_base/." "$PKG/opt/vb/py/vision_base/"; cp -a "$ROOT/../../core-py/core_parking/." "$PKG/opt/edge-parking/core_parking/"
cp "$ROOT/package/K92edge-parking-plate" "$PKG/etc/init.d/K92edge-parking-plate"; cp "$ROOT/package/edge-parking.conf" "$PKG/etc/edge-parking-plate.conf"; sed "s/@VERSION@/$VERSION/g" "$ROOT/package/control" > "$PKG/DEBIAN/control"; cp "$ROOT/package/postinst" "$PKG/DEBIAN/postinst"; cp "$ROOT/package/prerm" "$PKG/DEBIAN/prerm"
cp "$ROOT/presets/plate.json" "$PKG/etc/edge-parking-plate/parking.vb.config.template.json"
find "$PKG" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
find "$PKG" -depth -type d -name __pycache__ -empty -delete
chmod 0755 "$PKG/DEBIAN/postinst" "$PKG/DEBIAN/prerm" "$PKG/etc/init.d/K92edge-parking-plate" "$PKG/opt/vb/bin/vb-runtime" "$PKG/opt/vb/python/bin/python3.11"
printf '%s\n' "vb_runtime_sha256=$(sha256sum "$VB_RUNTIME" | awk '{print $1}')" "python_runtime_sha256=$(sha256sum "$RUNTIME_STAGE/opt/vb/python/bin/python3.11" | awk '{print $1}')" "source_date_epoch=$SOURCE_DATE_EPOCH" > "$PKG/opt/edge-parking/BUILD-METADATA"
DEB="$OUT/edge-parking-plate_${VERSION}_riscv64.deb"
[ ! -e "$DEB" ] || { echo "refusing existing package: $DEB" >&2; exit 2; }
export SOURCE_DATE_EPOCH
find "$PKG" -exec touch -h -d "@$SOURCE_DATE_EPOCH" {} +
dpkg-deb --build --root-owner-group "$PKG" "$DEB"; sha256sum "$DEB"
