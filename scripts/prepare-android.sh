#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
if [[ $(uname -s) != Linux || $(uname -m) != x86_64 ]]; then
    echo "Run this script on x86_64 Ubuntu/WSL" >&2
    exit 1
fi
for tool in curl unzip tar sha256sum; do command -v "$tool" >/dev/null; done
tools="$root/tmp/android-tools"
mkdir -p "$tools"
download() {
    local url=$1 file=$2 digest=$3
    if [[ -f "$file" ]] && echo "$digest  $file" | sha256sum --status -c -; then return; fi
    curl -fL --retry 3 --connect-timeout 15 --max-time 900 -C - "$url" -o "$file.part"
    echo "$digest  $file.part" | sha256sum --status -c -
    mv "$file.part" "$file"
}
download https://dl.google.com/android/repository/android-ndk-r27d-linux.zip \
    "$tools/android-ndk-r27d-linux.zip" \
    601246087a682d1944e1e16dd85bc6e49560fe8b6d61255be2829178c8ed15d9
if [[ ! -f "$tools/android-ndk-r27d/build/cmake/android.toolchain.cmake" ]]; then
    unzip -q -n "$tools/android-ndk-r27d-linux.zip" -d "$tools"
fi
revision=$(cat "$root/android/vcpkg-revision.txt")
download "https://codeload.github.com/microsoft/vcpkg/tar.gz/$revision" \
    "$tools/vcpkg.tar.gz" \
    021064a3b11d9280be28c91634a92b963e3b70ac15cc86a35d78ccdcc2b5e474
if [[ ! -d "$tools/vcpkg-$revision" ]]; then tar -xzf "$tools/vcpkg.tar.gz" -C "$tools"; fi
if [[ ! -x "$tools/vcpkg-$revision/vcpkg" ]]; then
    (cd "$tools/vcpkg-$revision" && ./bootstrap-vcpkg.sh -disableMetrics)
fi
download https://files.pythonhosted.org/packages/6e/53/ebfed7b689c338dd8ebeec9c0730c8d56821292f14e2536e5f3ef1a05744/ninja-1.13.2-py3-none-manylinux2014_x86_64.manylinux_2_17_x86_64.whl \
    "$tools/ninja-fast.whl" \
    65a24341b5ac09fcadcc37082660be40a94174e51a937fabf6e2cae26225fa2c
unzip -q -n "$tools/ninja-fast.whl" -d "$tools/ninja-fast"
chmod 0755 "$tools/ninja-fast/ninja-1.13.2.data/scripts/ninja"
echo "Toolchain ready. Run: bash scripts/build-android.sh"
