#!/bin/sh
set -eu
VERSION=3.11.17
SOURCE_SHA256=bfb74ad39efae27cda510f134ab408e00f9992c56851cfc0b1cdb5646da11599
SOURCE_URL=https://repo.huaweicloud.com/python/3.11.17/Python-3.11.17.tar.xz
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
OUT=${OUT:-"$ROOT/.build/python-$VERSION"}
SRC_CACHE=${SRC_CACHE:-"$ROOT/.cache"}
SYSROOT=${SYSROOT:-}
SYSROOT_EXTRA=${SYSROOT_EXTRA:-}
CROSS_PREFIX=${CROSS_PREFIX:-}
JOBS=${JOBS:-2}
READELF=${READELF:-/usr/bin/readelf}
usage(){ echo "usage: $0 --sysroot SYSROOT --cross-prefix PREFIX [--source FILE] [--out DIR]" >&2; exit 2; }
SOURCE=
while [ "$#" -gt 0 ]; do
  case "$1" in
    --sysroot) SYSROOT=$2; shift 2;;
    --cross-prefix) CROSS_PREFIX=$2; shift 2;;
    --source) SOURCE=$2; shift 2;;
    --out) OUT=$2; shift 2;;
    --jobs) JOBS=$2; shift 2;;
    *) usage;;
  esac
done
[ -n "$SYSROOT" ] && [ -d "$SYSROOT" ] || { echo "missing --sysroot" >&2; exit 2; }
[ -n "$CROSS_PREFIX" ] && command -v "${CROSS_PREFIX}gcc" >/dev/null 2>&1 || { echo "missing cross compiler" >&2; exit 2; }
mkdir -p "$SRC_CACHE"
[ ! -e "$OUT" ] || { echo "refusing existing output directory: $OUT" >&2; exit 2; }
mkdir "$OUT"
[ -n "$SOURCE" ] || SOURCE="$SRC_CACHE/Python-$VERSION.tar.xz"
if [ ! -f "$SOURCE" ]; then
  command -v curl >/dev/null 2>&1 || { echo "curl is required for the approved mirror" >&2; exit 2; }
  curl --fail --location --retry 2 --output "$SOURCE.part" "$SOURCE_URL"
  mv "$SOURCE.part" "$SOURCE"
fi
actual=$(sha256sum "$SOURCE" | awk '{print $1}')
[ "$actual" = "$SOURCE_SHA256" ] || { echo "Python source SHA mismatch: $actual" >&2; exit 1; }
BUILD="$OUT/build"; STAGE="$OUT/stage"
DEP_ROOT=${SYSROOT_EXTRA:-$SYSROOT}
for dep in usr/include/openssl/ssl.h usr/include/ffi.h usr/include/zlib.h usr/include/expat.h usr/lib/libssl.so usr/lib/libcrypto.so usr/lib/libffi.so usr/lib/libz.so usr/lib/libexpat.so; do
  [ -e "$DEP_ROOT/$dep" ] || { echo "missing target dependency: $DEP_ROOT/$dep" >&2; exit 1; }
done
[ -x "$READELF" ] || { echo "missing host readelf: $READELF" >&2; exit 1; }
mkdir "$BUILD" "$STAGE"
tar -xf "$SOURCE" -C "$BUILD" --strip-components=1
CC="${CROSS_PREFIX}gcc"; CXX="${CROSS_PREFIX}g++"; AR="${CROSS_PREFIX}ar"; RANLIB="${CROSS_PREFIX}ranlib"
export CC CXX AR RANLIB READELF
export CPPFLAGS="--sysroot=$SYSROOT${SYSROOT_EXTRA:+ -I$SYSROOT_EXTRA/usr/include}" LDFLAGS="--sysroot=$SYSROOT${SYSROOT_EXTRA:+ -L$SYSROOT_EXTRA/usr/lib}"
cd "$BUILD"
./configure --host=riscv64-unknown-linux-musl --build="$(./config.guess 2>/dev/null || echo x86_64-pc-linux-gnu)" --prefix=/opt/vb/python --with-build-python="${BUILD_PYTHON:-python3}" --with-ensurepip=no --enable-shared --with-openssl="${SYSROOT_EXTRA:-$SYSROOT}/usr" --with-system-ffi --with-system-expat --with-lto=no --disable-test-modules
make -j"$JOBS"
make DESTDIR="$STAGE" install
test -x "$STAGE/opt/vb/python/bin/python3.11"
test -f "$STAGE/opt/vb/python/lib/libpython3.11.so.1.0"
find "$STAGE/opt/vb/python" -type f -exec sha256sum {} \; | sort > "$OUT/runtime.sha256"
printf '%s\n' "source=$SOURCE" "source_sha256=$actual" "target_prefix=/opt/vb/python" > "$OUT/runtime-build.txt"
echo "runtime staged at $STAGE/opt/vb/python"
