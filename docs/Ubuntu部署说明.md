# Ubuntu 22.04 x86_64 部署

本包为 Ubuntu 22.04 x86_64 原生 Release 版本，依赖系统动态库。

## 安装与启动

```bash
sudo apt-get update
sudo apt-get install -y libprotobuf23 libmosquitto1 libcurl4 libarchive13 libssl3 libstdc++6 ca-certificates
tar -xzf logagent-ubuntu22.04-x86_64.tar.gz -C /你的部署目录
cd /你的部署目录
sha256sum -c SHA256SUMS
cp -n device-agent.cloud.conf.example device-agent.cloud.conf
```

编辑 `device-agent.cloud.conf`，填写云端设备 SN（`device`）、设备私钥路径（`private-key`）和设备型号（`model`）；测试阶段也可用 `identity-dir` 指向含 `identity.json`（`sn`、`public_key`）与 `priv.pem` 的目录来替代这两项；服务地址使用云端提供的值。设备公钥需先由 PC 管理工具调用 `POST /v1/device-keys/bind` 绑定到云端，Agent 只用私钥签名换 Token。编辑 `log-sources.json`，填写业务日志目录、文件匹配规则及文件名日期格式。然后从部署目录运行：

```bash
./device-agent
```

按 Ctrl+C 停止。修改配置后重启程序。运行日志为 `run/agent.log`，设备 Token 缓存在 `run/cloud-agent/tokens/`，重新部署时保留该目录。

## 从源码重新打包

在项目根目录执行：

```bash
sudo apt-get install -y build-essential cmake pkg-config protobuf-compiler libprotobuf-dev libmosquitto-dev libarchive-dev libcurl4-openssl-dev libssl-dev
bash scripts/build-ubuntu.sh
```

分发包：`out/logagent-ubuntu22.04-x86_64.tar.gz`。展开目录：`out/ubuntu-x86_64/`。包内只含凭证占位模板，真实设备配置在部署时填写。
