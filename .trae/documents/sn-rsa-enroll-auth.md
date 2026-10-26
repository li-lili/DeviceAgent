# 计划：注册码换 Token 改为 SN + RSA-PSS 签名换 Token

## Context

云端认证方案变更：设备不再用 `enrollment-key`（注册码）换 MQTT Token，改为 **SN + RSA-PSS 签名**（规格见 [device-key-auth-cpp-spec.md](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\device-key-auth-cpp-spec.md)，流程见 [设备激活与日志采集流程.png](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\设备激活与日志采集流程.png)）。

- **Agent 职责**（流程图阶段二）：读 SN/私钥/Token 缓存 → 无 Token 时对 `"noetix-enroll-v1|"+SN` 做 RSA-PSS(SHA-256, MGF1-SHA256, salt=32) 签名 → Base64 单行 → `POST /v1/enroll` body **仅** `{sn, signature}` → 保存 Token → MQTT `client_id=sn, password=token`（现状不变）。
- **bind（公钥绑定）由 PC 管理工具调用**，Agent 不实现 bind。
- **私钥只读不生成**（按 spec 文档）；开发期在 `run/device-keys/` 下用 openssl 生成一套模拟设备密钥供联调。

## 已确认决策（用户拍板）

| 决策 | 结论 |
| --- | --- |
| bind 调用方 | PC 端工具，Agent 不实现 |
| Agent 密钥生成 | 不生成，只读配置路径；先在本地路径模拟一套密钥 |
| 429/5xx 重试 | 维持快速失败（与规格第六节的有意偏差，日志报 rate_limited 即退出） |
| SN 来源 | 复用现有 `device` 配置键（即 SN，`identifier()` 校验规则与规格 sn 规则一致） |
| 用户 live conf | `device-agent.cloud.conf` 一并改为新方案（指向模拟密钥路径） |
| salt 长度 | `RSA_PSS_SALTLEN_DIGEST`（=32）。**规格文档 358 行勘误**：RSA-2048+SHA-256 的 `MAX_LENGTH`=222≠32，文档声称两者等价有误；salt=32 与服务端 `equals_hash` 及 auto 检测均兼容 |

## 改动清单

### 1. [src/main.cpp](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\src\main.cpp)

**Options（144-154 行）**：删 `enrollment_key`；增 `fs::path private_key;`。`model`/`serial` 保留（Hello/Inventory 仍在用）。

**load_options()（789-883 行）**：
- 新键 `private-key`（`path_value` 解析，相对路径基于配置文件目录）。
- `enrollment-key` 按第 836 行 `allowed-root` 先例改为"obsolete and ignored"警告。
- 删 880 行 `model is required for enrollment` 检查（新 body 无 model）。

**新增 static 函数（放在 sha256() 附近）**：
- `read_private_key(path)`：fopen + `PEM_read_PrivateKey`（兼容 PKCS#8/PKCS#1），失败 throw `ENROLL_FAILED "cannot read private key: <path>"`。
- `make_enroll_signature(sn, priv)`，OpenSSL 3 正确序列（规格 271 行示例把 `EVP_sha256()` 传给 `EVP_DigestSignInit_ex` 的 `const char* mdname` 是类型错误，不用）：
  1. `EVP_DigestSignInit(ctx, &pctx, EVP_sha256(), nullptr, priv)`（经典 API）
  2. `EVP_PKEY_CTX_set_rsa_padding(pctx, RSA_PKCS1_PSS_PADDING)` → `set_rsa_pss_saltlen(pctx, RSA_PSS_SALTLEN_DIGEST)` → `set_rsa_mgf1_md(pctx, EVP_sha256())`
  3. `EVP_DigestSignUpdate(ctx, payload.data(), payload.size())`，payload = `"noetix-enroll-v1|" + sn`
  4. `EVP_DigestSignFinal` 两次调用拿 256 字节
  5. `EVP_EncodeBlock` 单行 Base64（缓冲区 `(len+2)/3*4`=344，返回值是长度**不写 NUL**，构造 `std::string(buf, ret)`）
- 头文件加 `#include <openssl/pem.h>`；CMake 已链 OpenSSL::Crypto 无需改。

**resolve_token()（205-327 行）**：
- 215 行 enrollment 条件改为 `!o.private_key.empty() || !o.enroll_url.empty()`。
- 234-235 行缺项报错改为 `ENROLL_FAILED "token is missing; set private-key and enroll-url"`。
- 请求体从 `{enrollment_key, device_id, model, serial}` 改为 `{sn, signature}` 两字段（protobuf Struct → `cloud_json()`；map 序不影响 JSON 语义）。
- 签名生成失败 → `ENROLL_FAILED "cannot sign enrollment payload"`。
- 响应错误解析（286-301 行）：兼容新扁平 `{"error":"code"}`（kStringValue）与旧嵌套 `{"error":{"code"}}`；已知码集合改为 `{"bad_request","unauthorized","sn_key_mismatch","rate_limited"}`；409 hint 改为提示需产线/PC 工具介入。
- 成功响应：取 `token` 不变；新增 `device_id` 字段与 device 不符时 `log(Warning)`（不失败）；`reissued` 忽略。
- 临时文件 fd/0600/fsync/rename/valid_token/catch 清理（241-246、308-326 行）全部保留不动。

### 2. 测试

**[tests/enrollment_e2e.py](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\tests\enrollment_e2e.py) 重写**：
- 用 cryptography（/usr/bin/python3 3.10 + cryptography 3.4.8 已确认可用）生成 RSA-2048，私钥 PKCS#8 PEM / 公钥 SPKI PEM 落临时目录。
- mock `/v1/enroll`：`set(body.keys())=={"sn","signature"}`；`base64.b64decode(sig, validate=True)` 防换行混入；`pub.verify(sig, f"noetix-enroll-v1|{sn}".encode(), padding.PSS(mgf=MGF1(SHA256()), salt_length=32), SHA256())`——**salt_length 必须写字面量 32**（3.4.8 无 `PSS.DIGEST_LENGTH` 常量；严格 32 隐式断言 C++ 端 salt 正确，防 MAX_LENGTH=222 回归）。
- 响应 `200 {"device_id","token","reissued":False}`；mosquitto password_file 校验 token 流程沿用。
- 用例：成功→缓存 0600→MQTT 上线；二次启动复用缓存（enroll 零请求）；坏签名→`401 {"error":"unauthorized"}`→ENROLL_FAILED；missing token 响应；非回环 HTTP enroll-url→"requires HTTPS"；新增"未配 private-key 明确报错"用例；沿用"日志无凭证泄漏"断言。
- 删除 already_enrolled/invalid_enrollment_key 用例（服务端不再有这些码）。

**[tests/https_ca_e2e.py](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\tests\https_ca_e2e.py) 小改**：配置里 `enrollment-key=` 行换成 `private-key=`（用该测试 openssl 已生成的私钥，genpkey 的 PKCS#8 可直接读）；若原断言检查 body 的 device_id 字段则改为 `sn`。服务器仍无条件回 `{"token"}`，无 bind 请求。

**[tests/config_read.py](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\tests/config_read.py)、cloud_defaults.py 不改**（离线用例的报错点在 token 缓存/device 校验，不依赖注册码；`enr_synthetic` 泄漏断言在废弃键警告下仍成立）——跑通即可。

### 3. 配置与文档

- **模拟设备密钥**：`run/device-keys/priv.pem` + `pub.pem`（`openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048` + `openssl pkey -pubout`；私钥 chmod 600）。run/ 已在 .gitignore。
- **[device-agent.cloud.conf](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\device-agent.cloud.conf)**（用户 live conf）：删 enrollment-key 行，加 `private-key=run/device-keys/priv.pem`，注释说明需 PC 工具先把 `run/device-keys/pub.pem` 绑到云端。
- **[device-agent.cloud.conf.example](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\device-agent.cloud.conf.example) 与 android/config/ 同名文件**：删 enrollment-key，加 `private-key=` 占位（如 `/nv/priv.pem`）及说明注释。
- **[README.md](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\README.md)、[docs/Ubuntu部署说明.md](file:///\\wsl.localhost\Ubuntu-22.04\home\liziang\DeviceAgent\docs\Ubuntu部署说明.md)**："注册码"措辞改为"设备私钥签名换 Token"（绑定由 PC 工具完成）。

### 4. 规格文档勘误（口头反馈给云端/PC 工具团队，不改 spec 文件）

1. 358 行：`MAX_LENGTH`=222≠32，与 `equals_hash` 不等价；设备端必须 salt=32。
2. 271 行：`EVP_DigestSignInit_ex` 的 mdname 是 `const char*`，示例传 `EVP_sha256()` 编译不过。
3. bind 路径不一致：spec `/v1/device-keys/bind` vs 流程图 `/v1/device/keys/bind`，PC 工具实现前需与服务端对齐。

## 验证

1. `bash scripts/test.sh` 全绿（8 个测试）；重点：enrollment_e2e 的 body 键集合恰为 `{sn,signature}`、严格 salt=32 验签通过、缓存 0600、二次启动零请求、无凭证泄漏。
2. 手动冒烟：用模拟密钥 + `environment=cloud` 启动 `./build/device-agent`，确认签名日志 `ENROLL requesting device token` 后对真实测试云端的请求路径/错误码合理（真实验签通过需 PC 工具先把模拟公钥绑定到云端，此步在本机环境无法完成时以 401/明确错误为预期）。
3. C++ 单测 filename-date-formats 与其余 e2e 回归不受影响。
