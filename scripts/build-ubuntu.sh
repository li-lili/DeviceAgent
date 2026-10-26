#!/usr/bin/env bash
set -euo pipefail
root=$(cd "$(dirname "$0")/.." && pwd)
build="$root/build/ubuntu-x86_64"
package="$root/out/ubuntu-x86_64"
[[ $(uname -s) == Linux && $(uname -m) == x86_64 ]] || { echo "Run on x86_64 Ubuntu 22.04/WSL" >&2; exit 1; }
cmake -S "$root" -B "$build" -DCMAKE_BUILD_TYPE=Release
cmake --build "$build" -j "${DEVICE_AGENT_BUILD_JOBS:-2}"
mkdir -p "$package/docs"
install -m 0755 "$build/device-agent" "$package/device-agent"
strip "$package/device-agent"
printf 'environment=cloud\n' > "$package/device-agent.conf"
install -m 0644 "$root/device-agent.cloud.conf.example" "$root/log-sources.json" "$package/"
install -m 0644 "$root/docs/Ubuntu部署说明.md" "$package/README.md"
(cd "$package" && sha256sum device-agent device-agent.conf device-agent.cloud.conf.example log-sources.json README.md > SHA256SUMS)
# Explicit list excludes credentials and runtime files added to the output directory.
tar -czf "$root/out/logagent-ubuntu22.04-x86_64.tar.gz" -C "$package" \
    device-agent device-agent.conf device-agent.cloud.conf.example log-sources.json README.md \
    SHA256SUMS
echo "Ubuntu package: $root/out/logagent-ubuntu22.04-x86_64.tar.gz"
