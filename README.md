# DeviceAgent

C++17 日志采集程序，通过 MQTT 5 + JSON 接收云端命令，按文件名日期或最新文件规则选择业务日志，打包上传并回报结果。

系统设计约定见 [日志系统顶层设计](docs/顶层设计.md)，第一条为日志大小、轮转与磁盘空间管理。

完整操作见 [云端使用说明](docs/使用说明.md)，包含设备注册、配置、前台启动、日志采集、下载解压与排错。

业务日志配置见 [业务端使用说明](docs/业务端使用说明.md)。

## 编译

Ubuntu 22.04 / WSL Ubuntu-22.04 中执行：

```bash
sudo apt-get update
sudo apt-get install -y build-essential cmake pkg-config protobuf-compiler libprotobuf-dev \
  libmosquitto-dev libarchive-dev libcurl4-openssl-dev libssl-dev
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j2
```

## 配置和启动

首次克隆后，在项目根目录创建云端配置；`-n` 防止覆盖已有文件：

```bash
cp -n device-agent.cloud.conf.example device-agent.cloud.conf
```

在 `device-agent.cloud.conf` 中填写设备码（即 SN）和设备私钥路径 `private-key`；测试阶段可改用 `identity-dir` 指向含 `identity.json`（`sn`、`public_key`）与 `priv.pem` 的目录，此时无需再写 `device`、`private-key`，在 `device-agent.conf` 中设置 `environment=cloud`。设备公钥需先由 PC 管理工具调用 `POST /v1/device-keys/bind` 绑定到云端，之后 Agent 用私钥对 `noetix-enroll-v1|SN` 做 RSA-PSS 签名换取 Token。然后编辑 `log-sources.json` 的业务日志目录和匹配规则，采集根目录为 `<ANDROID_LOG_ROOT>=/data/log`，下分 `app/`、`cpp-sdk/`、`embedded/` 三个来源目录。

从项目根目录前台启动：

```bash
./build/device-agent
```

按 Ctrl+C 停止。Agent 不接收命令行参数。运行日志位于 `run/agent.log`，Token 缓存在 `run/cloud-agent/tokens/`，采集包通过云端下载入口获取。

当前代码内置的是 `test.humanuniverse.cn` 下的测试环境地址。接入生产环境时，需要在 `src/main.cpp` 的 `environment_defaults()` 中更新云端地址并重新编译。

## 仓库内容

| 路径 | 内容 |
| --- | --- |
| `src/` | C++ 源码 |
| `proto/` | 协议定义，编译必需 |
| `CMakeLists.txt` | 编译配置 |
| `device-agent.cloud.conf.example` | 云端配置占位模板 |
| `device-agent.conf`、`log-sources.json` | 环境选择及日志来源配置 |
| `docs/` | 使用说明与接口文档 |
| `scripts/`、`tests/`、`mocks/` | 构建及开发验证工具 |

真实云端配置、Token、运行记录、业务日志、编译产物及编辑器缓存已通过 `.gitignore` 排除。请勿使用 `git add -f` 强制提交这些文件。

Android 12 ARM64 原生服务的构建与固件集成见 [Android 部署说明](docs/Android部署说明.md)。

编译和打包命令见 [Android 编译说明](docs/Android编译说明.md)。

enroll 的结构化脱敏日志、响应判定及离线验证见 [enroll 响应诊断](docs/enroll响应诊断.md)。
