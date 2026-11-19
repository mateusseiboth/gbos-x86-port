#!/bin/bash
# Build vm-mouse for the layer with the Android NDK (r30); output: pointer/out/vm-mouse.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NDK="${ANDROID_NDK:?set ANDROID_NDK}/toolchains/llvm/prebuilt/linux-x86_64/bin"
mkdir -p "$HERE/out"
"$NDK/x86_64-linux-android35-clang" -O2 -Wall -Wextra -Werror -fPIE -pie -o "$HERE/out/vm-mouse" "$HERE/vm_mouse.c" -llog
"$NDK/llvm-strip" --strip-unneeded "$HERE/out/vm-mouse"
ls -l "$HERE/out/vm-mouse"
