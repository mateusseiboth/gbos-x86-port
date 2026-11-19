#!/usr/bin/env bash
# RADV (Mesa's Vulkan driver for AMD GPUs) for Android x86_64: the Radeon's hardware rendering on a
# real PC, under ANGLE (GLES on Vulkan, in /system). The layer installs it as
# /vendor/lib64/hw/vulkan.radv.so (gpu_amd.prop: ro.hardware.vulkan=radv).
#
# Reuses gbos-vm-linux's Mesa tree with the gralloc mapper5 patch and its NDK cross file
# (build_guest_mesa.sh must have run). No LLVM: RADV compiles shaders with ACO.
#
#   vm-amd/graphics/build_radv.sh        -> vm-amd/graphics/out/vulkan.radv.so
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
LINUX_DIR="${GBOS_LINUX_DIR:-/mnt/dados/gbos/gbos-vm-linux}"
NDK="${ANDROID_NDK:-$LINUX_DIR/android-ndk-r30}"
API="${ANDROID_API:-37}"
SRC="$LINUX_DIR/mesa-26.2.4-venus"
CROSS="$LINUX_DIR/mesa-build/cross-x86_64-android.ini"
BUILD="$LINUX_DIR/mesa-build/build-radv"
TB="$NDK/toolchains/llvm/prebuilt/linux-x86_64/bin"

[ -f "$SRC/.gbos-patch" ] && [ -f "$CROSS" ] || { echo "run $LINUX_DIR/build_guest_mesa.sh first" >&2; exit 1; }
if [ ! -f "$BUILD/build.ninja" ]; then
  meson setup "$BUILD" "$SRC" --cross-file "$CROSS" --buildtype=release --wrap-mode=nodownload \
    -Dforce_fallback_for=libdrm -Dallow-fallback-for=libdrm -Dlibdrm:default_library=static \
    -Dlibdrm:amdgpu=enabled -Dexpat=disabled \
    -Dplatforms=android -Dplatform-sdk-version="$API" -Dandroid-stub=true \
    -Dandroid-libbacktrace=disabled -Dllvm=disabled -Dglx=disabled -Dgbm=disabled -Dvideo-codecs= \
    -Dgallium-va=disabled -Dzstd=disabled \
    -Dgallium-drivers= -Dvulkan-drivers=amd -Degl=disabled -Dgles1=disabled -Dgles2=disabled -Dopengl=false
fi
ninja -C "$BUILD" src/amd/vulkan/libvulkan_radeon.so
mkdir -p "$HERE/out"
"$TB/llvm-strip" --strip-unneeded -o "$HERE/out/vulkan.radv.so" "$BUILD/src/amd/vulkan/libvulkan_radeon.so"
ls -l "$HERE/out/vulkan.radv.so"
