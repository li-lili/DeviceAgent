# enroll 响应诊断

2026-10-28：修复错误码白名单导致 HTTP 409 等响应丢失业务信息的问题。

## 旧版为什么只有 request_failed

旧 `resolve_token()` 只接受 `bad_request`、`unauthorized`、`sn_key_mismatch`、`rate_limited` 四个字符串。其他 error.code、缺失字段、非 JSON 或解析失败均回退为 `request_failed`，未记录区分原因。`message`、`error.message` 从未写入日志；成功响应的具体 HTTP 状态也未记录。响应正文只在内存中使用，未落盘。

因此，历史 `HTTP 409 request_failed` 不能证明云端真的返回了 `error.code=request_failed`，也不能反推具体冲突原因。不能套用绑定 API 的 409 解释，更不能据此覆盖公钥。

## 新版行为

每次实际请求后只写一条 `ENROLL response {JSON}` 诊断记录。失败时该记录由原有 FATAL 异常出口写入；成功时写入正常日志。只保留以下经过筛选的字段：

- `http_status`：libcurl 实际取得的数字；0 表示未取得 HTTP 状态。
- `error_code` / `error_code_state`：兼容 `error.code` 和旧式 `error` 字符串，不再把未知业务码替换为 request_failed。只允许格式受限的短错误标识；含凭证、过长或非法格式时明确标记省略/脱敏。
- `message`、`error_message` 及各自 `_state`：分别对应顶层 message、error.message，缺失、类型错误、已脱敏、结构过复杂等情况明确区分。
- `json_state`、`error_state`、`response_complete`、`outcome` 和中文 `hint`。

字段 missing 不代表服务器没有错误，只表示该字段没有出现在可解析位置。非 JSON、空响应、类型错误和网络/TLS/超时均不能视为成功；不回显原始正文或解析器错误字符串。

| HTTP | 提示与行为 |
|---|---|
| 200 / 201 | 仅完整 JSON 对象、有效 token 且不存在 error 才接受。保留准确 HTTP 状态；token_received 不等于落盘，后续 `ENROLL succeeded; device token saved:` 才代表已保存 |
| 400 | 参数/签名格式非法，保留脱敏错误元数据，退出 |
| 401 | 未授权或验签失败，由服务端核对绑定和验签记录，退出；不建议自行重绑 |
| 403 | 访问策略或出口白名单拒绝，退出 |
| 409 | 身份/注册状态冲突，由云端排查；不要自行覆盖公钥 |
| 429 | 限流，本次停止，不自动重试 |
| 502 | 网关/上游失败，不能据此判断密钥不匹配 |
| 503 / 其他 | 保留实际状态和可安全展示的元数据，退出，不自动重试 |

成功后仍沿用原有 0600 临时 Token 文件、fsync、原子 rename 及 MQTT READY 流程。RSA-PSS / SHA-256 / MGF1-SHA256 / 32 字节盐长、请求两字段 sn/signature 及 HTTPS 验证未改变。

## 日志边界

不记录整个请求/响应，不序列化 token、私钥、signature、device_id 或任意其他响应字段。对响应中的凭证字段及本次请求签名建立仅在内存中的脱敏集合，处理其在 message/code 中的回显。对可识别的 PEM、Authorization/Bearer/Basic、凭证关键字上下文、URL、JWT、长编码串进一步遮盖；无法安全保留的元数据整体省略，避免截断后留下凭证片段。message 超过 2048 字节直接省略；JSON 遍历受深度及节点数量限制。

日志通过 JSON 序列化，控制字符不能伪造多条日志。脱敏可能隐藏部分解释性文字，应结合 error_code、HTTP、hint 和服务端日志排查。旧版 device_id 不匹配告警也已移除服务器原始值，防止异常响应通过该字段回显凭证。

## 离线验证

Linux/WSL 中使用独立 build 目录：

```bash
cmake -S . -B build-diagnostics -DCMAKE_BUILD_TYPE=Release -DDEVICE_AGENT_BUILD_RESPONSE_TESTS=ON
cmake --build build-diagnostics -j2
ctest --test-dir build-diagnostics --output-on-failure
DEVICE_AGENT_TEST_BINARY="$PWD/build-diagnostics/device-agent" python3 tests/enrollment_responses_e2e.py
```

`enroll-response-tests` 是无网络、无需密钥的 C++ 模拟响应测试。Python 集成测试只监听/连接 127.0.0.1，临时生成独立合成 RSA-2048 测试身份，验证实际 Agent 日志、单次 POST、请求协议、Token 缓存和错误收尾。临时身份和合成 Token 随测试目录清理，不访问既有身份或安卓板。

旧运行脚本若只匹配 `HTTP <数字> <白名单码>`，需要在下一次获准部署时同步读取 `ENROLL response` JSON；不能把脚本解析不到的码当成服务端 request_failed。

## 历史 409 是否可恢复

2026-10-28 11:11:10–11:11:21（北京时间），测试 SN `NTX_w1_v1.1.0.5_BJ_61026LV97` 的旧版 Android 调用只保留了 HTTP 409 与本地回退码。现有脱敏记录不足以恢复原始 error.code/message；补丁不会追溯恢复已丢弃数据。

需要云端按该时间和 SN 查询 logserver 网关/反向代理 access/error 日志及 noetix-log `/v1/enroll` 认证应用日志，提供原始错误码、脱敏 message、请求/追踪 ID、上游状态，以及验签、身份状态检查或 Token 签发的执行阶段。必要时关联入口使用的绑定数据库环境、SN 和公钥指纹记录。不要索取完整 signature、私钥、Token 或未脱敏请求/响应。

若服务器没有可关联记录，只能在云端确认可重试后，另行批准将新版部署到独立目录并执行下一次单次测试。新结果只能说明新请求的原因，不能自动证明与历史 409 完全相同。本次未执行部署或真实请求。
