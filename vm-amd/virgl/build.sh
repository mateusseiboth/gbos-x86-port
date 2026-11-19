#!/bin/bash
# virglrenderer 1.3.0 + external-egl-gbm.patch, for QEMU's Venus (Vulkan) path. run_vm.sh loads it
# from out/ (LD_LIBRARY_PATH, QEMU only); the system library is left alone.
#
#   VIRGL_TARBALL=/path/virglrenderer-1.3.0.tar.bz2 vm-amd/virgl/build.sh   (default: download)
#
# Needs meson, ninja, libepoxy-devel, libdrm-devel, mesa-libgbm-devel, vulkan-loader-devel.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION=1.3.0
WORK="$HERE/work"
URL="https://gitlab.freedesktop.org/virgl/virglrenderer/-/archive/$VERSION/virglrenderer-$VERSION.tar.bz2"

rm -rf "$WORK" "$HERE/out"
mkdir -p "$WORK"
TARBALL="${VIRGL_TARBALL:-$WORK/virglrenderer-$VERSION.tar.bz2}"
[ -f "$TARBALL" ] || curl -fsSL -o "$TARBALL" "$URL"
tar -xf "$TARBALL" -C "$WORK"
SRC="$WORK/virglrenderer-$VERSION"
patch -d "$SRC" -p1 < "$HERE/external-egl-gbm.patch"
meson setup "$WORK/build" "$SRC" --prefix="$HERE/out" --libdir=lib --buildtype=release \
  -Dvenus=true -Dvideo=false -Dtests=false >/dev/null
ninja -C "$WORK/build" install >/dev/null
rm -rf "$WORK"
ls -l "$HERE/out/lib/libvirglrenderer.so.1"
