# Android 12 ARM64 原生服务部署

此版本是 `arm64-v8a` 原生可执行程序，最低 API 31，不是 APK。与 Ubuntu 共用 C++ 核心代码，两端分别编译。

## 编译

在 x86_64 Ubuntu / WSL 的项目根目录执行：

```bash
sudo apt-get install -y build-essential cmake pkg-config curl unzip zip tar perl autoconf automake libtool ca-certificates
bash scripts/prepare-android.sh
bash scripts/build-android.sh
```

准备脚本将固定版本 NDK 和 vcpkg 下载到被 Git 忽略的 `tmp/android-tools/`。首次构建需要下载源码并编译依赖，后续复用缓存。无需在 Android 上安装这些构建工具。

也可通过 `ANDROID_NDK_HOME`、`VCPKG_ROOT` 指向已有 Linux 工具链。vcpkg 源码版本记录在 `android/vcpkg-revision.txt`；替换版本后需要重新验证依赖兼容性。构建使用静态第三方库及静态 libc++，仍使用 Android 系统的 Bionic 动态加载器与系统库。

完整分发包为 `out/logagent-android12-arm64.tar.gz`，可解压部署。展开后的产物目录：

```text
out/android-arm64/
  bin/device-agent
  config/device-agent.conf
  config/device-agent.cloud.conf.example
  config/log-sources.json
  config/ca-bundle.pem
  init/vendor.logagent.rc
  sepolicy/vendor_logagent.te
  sepolicy/file_contexts
  SHA256SUMS
```

包中只放凭证占位模板，不包含实际设备注册码或 Token。

## 配置目录

Android 程序启动时固定进入 `/data/vendor/logagent/`，不依赖 init 的工作目录，也不接受命令行参数。在此目录部署：

- `device-agent.conf`：内容为 `environment=cloud`。
- `device-agent.cloud.conf`：由模板复制，填写设备码、注册码。
- `log-sources.json`：实际业务日志目录与文件名规则。
- `ca-bundle.pem`：用于 HTTPS 注册及上传的可信 CA 证书包。

每个日志来源可单独配置绝对路径或相对路径，不再受统一 allowed-root 限制。相对路径以来源配置文件所在目录为基准。运行用户仍需要目录遍历、文件读取和 SELinux 权限；跳过符号链接。

Android 运行日志写入 Logcat，标签为 `DeviceAgent`；同时备份到 `/data/vendor/logagent/run/agent.log`。Token 位于 `/data/vendor/logagent/run/cloud-agent/tokens/<设备码>.token`。已注册设备应妥善迁移原 Token，避免重复注册。凭证和运行数据不可放到只读 vendor 分区。

云端地址、端口、注册接口、型号和运行路径均从 device-agent.cloud.conf 读取，修改后重启即可，无需重新编译。

## 集成系统固件

1. 将可执行程序安装到 `/vendor/bin/device-agent`，权限 0755。
2. 将启动文件安装到 `/vendor/etc/init/vendor.logagent.rc`。
3. 将 `sepolicy/` 中的模板合入设备 vendor SELinux 策略目录，并加入设备的 `BOARD_VENDOR_SEPOLICY_DIRS`。
4. 在启动服务前向数据目录部署配置、证书和设备凭证，属主设为 `system:system`。数据目录权限 0700，含凭证的文件权限 0600。
5. 确认业务写入进程可以写日志目录，采集服务可以读取。模板只给采集进程日志读取权限，业务写入权限需在对应业务域中配置。
6. 编译固件并刷入设备，验证启动及权限。策略模板需要结合设备现有策略编译检查，不可直接假定所有固件都能通用。

服务使用 system 用户，在 `sys.boot_completed=1` 时启动，异常退出后由 init 重启，配置重启间隔为 10 秒。服务启动前配置文件及 CA 必须存在；网络未就绪时观察后续连接和重连记录。

若实际业务日志已经有专用 SELinux 类型，优先复用该类型并增加最小读取规则，不要随意改动其他应用私有目录的标签。App 私有日志不能仅靠把路径写入配置就获得读取权限，应由固件设计明确授权的日志共享目录。

## 设备端检查

在具备对应系统调试权限的设备上，通过 adb 执行：

```bash
adb shell getprop ro.product.cpu.abi
adb shell getprop init.svc.vendor.logagent
adb logcat -s DeviceAgent:I
```

Logcat 的 INFO 记录启动、配置、握手和正常任务进度；WARN 记录无效消息、命令拒绝和发送异常；ERROR 记录启动失败、连接或订阅被拒绝、任务失败。仅查看错误可执行 `adb logcat -s DeviceAgent:E`。

配置目录不存在或日志文件无法打开等早期失败，也会通过 `DeviceAgent` 标签输出。策略模板已增加 `write_logd(vendor_logagent)`，集成固件时需同步更新；Ubuntu 仍使用终端和文件日志。

修改配置后重启：

```bash
adb shell setprop ctl.restart vendor.logagent
```

看到 `READY hello handshake complete` 表示 MQTT 及应用层握手完成。然后从云端创建新的采集命令，检查上传及下载结果。按天采集的业务文件名日期须匹配 `filename_date_formats`（默认 `%Y-%m-%d`），程序不解析日志正文。

如果只有编译成功和 ELF 检查，仍不能代替 Android 真机的启动、SELinux、联网及上传验证。

## Ubuntu 编译

原有 Ubuntu 编译方式保留；Android 的编译产物和缓存使用独立目录。Ubuntu 云端配置和运行步骤见 [使用说明](使用说明.md)。

开机自启配置、从手动运行迁移及服务控制，见 [Android 开机自启说明](Android开机自启.md)。
