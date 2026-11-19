#!/bin/bash
# make wrapper for the Googlebook x86_64 kernel (AOSP kernel/common, tag android17-6.18-2026-06_r52).
# Adapted from kernel/kmake.sh to the GBOS_ROOT layout (see vm-amd/README.md) and Fedora's LLVM.
#   KOUT=<dir> picks the build directory (default $GBOS_ROOT/kernel/out).
set -eu
GBOS="${GBOS_ROOT:-/mnt/dados/gbos}"
TC="$HOME/.rustup/toolchains/1.91.1-x86_64-unknown-linux-gnu"
export PATH="$TC/bin:$HOME/.cargo/bin:$PATH"
export KBUILD_GENDWARFKSYMS_STABLE=1
export LOCALVERSION=
# Google signs modules with SHA-1; Fedora's crypto policy rejects SHA-1 signatures unless told otherwise.
export OPENSSL_ENABLE_SHA1_SIGNATURES=1
cd "$GBOS/kernel/common"
exec make O="${KOUT:-$GBOS/kernel/out}" ARCH=x86_64 LLVM=1 \
  RUSTC="$TC/bin/rustc" BINDGEN="$HOME/.cargo/bin/bindgen" RUST_LIB_SRC="$TC/lib/rustlib/src/rust/library" "$@"
