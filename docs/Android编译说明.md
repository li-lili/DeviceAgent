# Android ARM64 编译

在 x86_64 Ubuntu/WSL 的 Linux 文件系统内运行：

```sh
bash scripts/prepare-android.sh
bash scripts/build-android.sh
```

需要 CMake、C/C++ 编译器、curl、unzip、zip、tar、Git、pkg-config、Autotools、Perl 和 Ninja。
准备脚本固定 NDK r27d、vcpkg 修订和下载摘要；构建目标 API 31、arm64-v8a、c++_static。
OpenSSL、curl、Protobuf、libarchive 和 Mosquitto 为目标架构编译，protoc 为主机 x86_64 工具。
可通过 ANDROID_NDK_HOME、VCPKG_ROOT、VCPKG_MAX_CONCURRENCY 指定工具链和并行度。
DEVICE_AGENT_ANDROID_DATA_DIR 指定运行目录；DEVICE_AGENT_ANDROID_BUILD_DIR、DEVICE_AGENT_ANDROID_PACKAGE_DIR 和 DEVICE_AGENT_ANDROID_ARCHIVE 指定独立输出。
发布目录和压缩包已存在时拒绝覆盖。构建目录缓存可以复用。
检查 readelf -h/-l/-d、SHA256SUMS 和 BUILD-INFO.txt；不允许打包真实配置、私钥或 Token。
