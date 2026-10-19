# DeviceAgent

C++17 日志采集程序，通过 MQTT 5 + JSON 接收云端命令，按文件名日期或最新文件规则选择业务日志，打包上传并回报结果。

完整操作见 [云端使用说明](docs/使用说明.md)，包含设备注册、配置、前台启动、日志采集、下载解压与排错。

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

在 `device-agent.cloud.conf` 中填写设备码和注册码，在 `device-agent.conf` 中设置 `environment=cloud`。然后编辑 `log-sources.json` 的业务日志目录和匹配规则，当前允许采集的根目录为项目下的 `business-logs/`。

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
