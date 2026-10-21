#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
revision=$(cat "$root/android/vcpkg-revision.txt")
export ANDROID_NDK_HOME=${ANDROID_NDK_HOME:-"$root/tmp/android-tools/android-ndk-r27d"}
export VCPKG_ROOT=${VCPKG_ROOT:-"$root/tmp/android-tools/vcpkg-$revision"}
export VCPKG_MAX_CONCURRENCY=${VCPKG_MAX_CONCURRENCY:-4}
export VCPKG_DISABLE_METRICS=1
# Ubuntu 22.04 自带 CMake 可用；优先使用准备脚本下载的 Linux Ninja。
if [[ -x "$root/tmp/android-tools/ninja-fast/ninja-1.13.2.data/scripts/ninja" ]]; then
    export PATH="$root/tmp/android-tools/ninja-fast/ninja-1.13.2.data/scripts:$PATH"
fi
command -v ninja >/dev/null || { echo "Run bash scripts/prepare-android.sh first" >&2; exit 1; }
export VCPKG_FORCE_SYSTEM_BINARIES=1
# 使用系统 curl 下载依赖，保留 vcpkg 的内容摘要校验。
export X_VCPKG_ASSET_SOURCES=${X_VCPKG_ASSET_SOURCES:-"x-script,/usr/bin/curl -sSfL --retry 2 --retry-all-errors --connect-timeout 15 --max-time 600 {url} --output {dst};x-block-origin"}
if [[ ! -f "$ANDROID_NDK_HOME/build/cmake/android.toolchain.cmake" ]]; then
    echo "Set ANDROID_NDK_HOME to a Linux Android NDK directory" >&2
    exit 1
fi
if [[ ! -x "$VCPKG_ROOT/vcpkg" ]]; then
    echo "Set VCPKG_ROOT to a bootstrapped vcpkg checkout (revision $revision)" >&2
    exit 1
fi
build="$root/build/android-arm64"
installed="$build/vcpkg_installed"
"$VCPKG_ROOT/vcpkg" install --triplet arm64-logagent --host-triplet x64-logagent-host \
    --x-manifest-root="$root/android" --x-install-root="$installed" \
    --overlay-triplets="$root/android/triplets"
cmake -S "$root" -B "$build" -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_TOOLCHAIN_FILE="$VCPKG_ROOT/scripts/buildsystems/vcpkg.cmake" \
    -DVCPKG_CHAINLOAD_TOOLCHAIN_FILE="$ANDROID_NDK_HOME/build/cmake/android.toolchain.cmake" \
    -DVCPKG_TARGET_TRIPLET=arm64-logagent -DVCPKG_HOST_TRIPLET=x64-logagent-host \
    -DVCPKG_OVERLAY_TRIPLETS="$root/android/triplets" \
    -DVCPKG_MANIFEST_DIR="$root/android" -DVCPKG_INSTALLED_DIR="$installed" \
    -DVCPKG_MANIFEST_INSTALL=OFF -DANDROID_ABI=arm64-v8a \
    -DANDROID_PLATFORM=android-31 -DANDROID_STL=c++_static \
    -DDEVICE_AGENT_PROTOC="$installed/x64-logagent-host/tools/protobuf/protoc"
cmake --build "$build" --target device-agent -j "$VCPKG_MAX_CONCURRENCY"
package="$root/out/android-arm64"
mkdir -p "$package/bin" "$package/config" "$package/init" "$package/sepolicy"
install -m 0755 "$build/device-agent" "$package/bin/device-agent"
"$ANDROID_NDK_HOME/toolchains/llvm/prebuilt/linux-x86_64/bin/llvm-strip" "$package/bin/device-agent"
install -m 0644 "$root/android/config/device-agent.conf" "$root/android/config/log-sources.json" "$package/config/"
install -m 0644 "$root/android/config/device-agent.cloud.conf.example" "$package/config/"
install -m 0644 "$root/android/init/vendor.logagent.rc" "$package/init/"
install -m 0644 "$root/android/sepolicy/"* "$package/sepolicy/"
# CA 包是公开证书，打包时不复制真实设备凭证。
ca_bundle=${DEVICE_AGENT_CA_BUNDLE:-/etc/ssl/certs/ca-certificates.crt}
if [[ ! -s "$ca_bundle" ]]; then
    echo "Set DEVICE_AGENT_CA_BUNDLE to a trusted PEM CA bundle" >&2
    exit 1
fi
install -m 0644 "$ca_bundle" "$package/config/ca-bundle.pem"
mkdir -p "$package/licenses"
while IFS= read -r -d '' license; do
    port=$(basename "$(dirname "$license")")
    mkdir -p "$package/licenses/$port"
    install -m 0644 "$license" "$package/licenses/$port/copyright"
done < <(find "$installed/arm64-logagent/share" -mindepth 2 -maxdepth 2 -name copyright -type f -print0)
mkdir -p "$package/licenses/mosquitto"
for license in "$build/_deps/mosquitto-src/"*LICENSE* "$build/_deps/mosquitto-src/"*licence*; do
    [[ ! -f "$license" ]] || install -m 0644 "$license" "$package/licenses/mosquitto/"
done
mkdir -p "$package/licenses/android-ndk"
for license in "$ANDROID_NDK_HOME/NOTICE" "$ANDROID_NDK_HOME/NOTICE.toolchain"; do
    [[ ! -f "$license" ]] || install -m 0644 "$license" "$package/licenses/android-ndk/"
done
install -m 0644 "$root/docs/Android部署说明.md" "$package/README.md"
install -m 0644 "$root/docs/Android开机自启.md" "$package/Android开机自启.md"
install -m 0644 "$root/docs/使用说明.md" "$package/使用说明.md"
install -m 0644 "$root/docs/配置参数说明.md" "$package/配置参数说明.md"
install -m 0644 "$root/docs/业务端使用说明.doc" "$package/业务端使用说明.doc"
(cd "$package" && sha256sum bin/device-agent > SHA256SUMS)
# 使用明确的文件清单，避免把用户另放到输出目录的真实凭证打进分发包。
tar -czf "$root/out/logagent-android12-arm64.tar.gz" -C "$package" \
    bin/device-agent config/device-agent.conf config/device-agent.cloud.conf.example \
    config/log-sources.json config/ca-bundle.pem init/vendor.logagent.rc \
    sepolicy/vendor_logagent.te sepolicy/file_contexts licenses README.md 使用说明.md Android开机自启.md 配置参数说明.md 业务端使用说明.doc SHA256SUMS
echo "Android arm64 package: $package"
