#!/bin/bash
# Build the layer's small native stand-ins with the Android NDK (r30).
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
NDK="${ANDROID_NDK:?set ANDROID_NDK}/toolchains/llvm/prebuilt/linux-x86_64/bin"
mkdir -p "$HERE/out"
for stub in futility gscutil; do
  "$NDK/x86_64-linux-android35-clang" -O2 -Wall -Wextra -Werror -fPIE -pie -o "$HERE/out/$stub" "$HERE/$stub.c"
  "$NDK/llvm-strip" --strip-unneeded "$HERE/out/$stub"
  ls -l "$HERE/out/$stub"
done
