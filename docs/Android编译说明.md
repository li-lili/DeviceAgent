# Android 编译与打包命令

在 **x86_64 Ubuntu 22.04 或 WSL Ubuntu-22.04** 中执行。产物适用于 **Android 12 及以上、ARM64（arm64-v8a）**，是原生可执行程序，不是 APK。

## 当前机器重新编译

打开 Ubuntu 终端，执行以下命令。当前机器的工具链位于 `/home/liziang/logagent/tmp/android-tools/`，可直接复用：

```bash
cd /home/liziang/DeviceAgent

export ANDROID_NDK_HOME=/home/liziang/logagent/tmp/android-tools/android-ndk-r27d
export VCPKG_ROOT=/home/liziang/logagent/tmp/android-tools/vcpkg-$(cat android/vcpkg-revision.txt)
export PATH="/home/liziang/logagent/tmp/android-tools/ninja-fast/ninja-1.13.2.data/scripts:$PATH"

bash scripts/build-android.sh
```

如果当前在 Windows PowerShell，先执行 `wsl -d Ubuntu-22.04` 进入 Ubuntu，再执行上述命令。这套命令已在当前机器完成编译及打包验证。

## 新环境首次编译

进入项目根目录，安装依赖并准备工具链：

```bash
cd /你的项目路径/DeviceAgent

sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config curl unzip zip tar \
  perl autoconf automake libtool ca-certificates

bash scripts/prepare-android.sh
bash scripts/build-android.sh
```

准备脚本会将固定版本的 Android NDK、vcpkg 和 Ninja 下载到项目的 `tmp/android-tools/`。首次编译需要联网下载并编译依赖。

后续在同一环境、项目根目录执行以下命令即可重新编译并打包：

```bash
bash scripts/build-android.sh
```

若工具链放在项目外部，需像“当前机器重新编译”示例一样设置 `ANDROID_NDK_HOME`、`VCPKG_ROOT` 和 Ninja 的 `PATH`。

## 产物与校验

| 路径 | 内容 |
| --- | --- |
| `out/logagent-android12-arm64.tar.gz` | 完整分发包，部署时使用 |
| `out/android-arm64/bin/device-agent` | 已去除调试符号的可执行文件 |
| `out/android-arm64/config/` | 配置模板、日志来源示例和 CA 证书包 |
| `out/android-arm64/` | 展开的分发包，包含启动文件、SELinux 模板及说明文档 |
| `build/android-arm64/` | 编译缓存和中间产物，后续编译会复用 |

编译成功后，检查文件架构及校验值：

```bash
file out/android-arm64/bin/device-agent
(cd out/android-arm64 && sha256sum -c SHA256SUMS)
sha256sum out/logagent-android12-arm64.tar.gz
```

文件架构应为 `ARM aarch64`，文件校验应输出 `bin/device-agent: OK`。编译脚本会更新上述分发包，包内使用设备凭证占位模板；实际设备码和注册码在部署时填写。真机部署见 [Android 部署说明](Android部署说明.md)。
