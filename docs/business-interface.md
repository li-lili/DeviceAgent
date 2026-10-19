# 业务与 DeviceAgent：首版接入协议

当前默认在 log-sources.json 中配置来源，无需业务注册。以下 MQTT 注册方式仅在 sources-config 留空时启用。业务只需正常写本地日志，并声明日志来源；不要求业务实现取日志命令、打包、上传或进程崩溃检测。

## MQTT

业务连接端侧 Broker，Agent 使用独立的端侧 MQTT 连接订阅。云端命令使用 Agent 的另一条连接。

| 方向 | 主题 | QoS | Retain | PB |
|---|---|---|---|---|
| 业务 → Agent | `local/v1/{device_id}/biz/{business_id}/up/register` | 1 | 是 | `Registration` |
| Agent → 业务 | `local/v1/{device_id}/biz/{business_id}/down/register_ack` | 1 | 否 | `RegistrationAck` |

两方使用 MQTT 5，发送二进制 Protobuf，User Property `proto=1`。生成代码用 `proto/business.proto`。业务先订阅自己的 ACK 主题，收到 SUBACK 后发布注册，避免错过确认。

Broker 必须限制业务只能发布本业务注册主题、订阅本业务确认主题；不能发布云端命令、上传凭证或其他业务配置。当前模拟 Broker 是仅监听回环地址的匿名测试服务，未模拟生产 ACL。

## 完整注册

```text
business_id: camera_service
config_version: 1
log_sources:
  source_id: business
  path: /data/logs/app
  recursive: true
  file_patterns: ["*.log"]
log_sources:
  source_id: camera_sdk
  path: /data/logs/sdk
  recursive: true
  file_patterns: ["*.log"]
```

`business_id`、`source_id` 为 1～64 个字母、数字、点、横线或下划线，不能单独为 `.` / `..`。`source_id` 在设备内唯一，是云端 `collectLogs.sources` 使用的名称。云端需要预先约定或另行获取这些来源名称，本原型未扩展来源发现 API。

`path` 为绝对文件路径或目录，必须位于 Agent 配置文件的 `allowed-root` 内。允许注册尚未创建的目录。Agent 读取时再次校验实际路径，忽略符号链接文件及符号链接子目录。业务必须给 Agent 运行用户目录遍历和文件读取权限；配置注册成功不代表稍后一定可读取。

未配置 `file_patterns` 表示不过滤文件名，所以目录应只存日志；原型暂不解压 `.gz/.zst` 历史日志。`recursive` 默认 false。不需要 time_format、timezone 或级别配置。按天拉取要求文件名含合法 YYYY-MM-DD 日期，按北京时间解释；例如 2026-10-16.log。Agent 直接打包原始文件，不解析内容。

## 生命周期

1. 每次连接成功，发布完整配置，而非增量。
2. 同版本、同内容可以重发，Agent 再次确认；同版本改内容或使用旧版本会被拒绝。
3. 配置变化，递增 `config_version`。来源列表整体替换，空列表表示取消本业务所有来源。
4. Agent 校验通过、写入小型配置文件后，发送 `accepted=true`。
5. 拒绝时整份新配置不生效，旧配置继续使用；业务应修正 retained 注册内容，避免 Agent 重启后反复收到错误声明。
6. 业务断开不删除来源，便于退出后取日志。Agent 重启从配置文件恢复，同时订阅 retained 声明。
7. 注册 ACK 超时可用同一配置重试；Agent 晚启动时会收到 retained 消息。MQTT Retain 是否跨 Broker 重启保存取决于 Broker 的持久化设置，不能替代 Agent 本地配置文件。

已运行的取日志任务使用接受任务时的配置快照；新注册配置对后续任务生效。业务不用因正在取日志而停止正常写入。

## 首版持久化边界

- 仅保存注册配置、最近 100 个命令状态/结果，以及任务期间的临时包。
- 命令重复下发时返回已有状态，完成后返回原结果。
- Agent 在执行任务期间重启，记录改为 `CMD_FAILED / AGENT_RESTARTED`；云端用新的命令编号重新发起。
- 不维护全部遥测的磁盘缓存，不恢复中断上传，不删除业务原始日志。
