#!/bin/bash
# Build the layer's small native stand-ins with the Android NDK (r30).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NDK="${ANDROID_NDK:?set ANDROID_NDK}/toolchains/llvm/prebuilt/linux-x86_64/bin"
mkdir -p "$HERE/out"
"$NDK/x86_64-linux-android35-clang" -O2 -Wall -Wextra -Werror -fPIE -pie -o "$HERE/out/futility" "$HERE/futility.c"
"$NDK/llvm-strip" --strip-unneeded "$HERE/out/futility"
ls -l "$HERE/out/futility"
