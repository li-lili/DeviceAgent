# SN 与私钥读取改造说明

本文记录设备身份（SN 与 RSA 私钥）读取方式的改造：从"文件路径配置"扩展为"可直接从 RK3588 Vendor Storage 读取"，并说明安全边界、失败处理与部署要求。

## 1. 背景

早期实现要求把 SN 和私钥放在文件系统上，由配置项指向：

- `device-file`：单行 SN 文本
- `private-key`：PEM 私钥文件

产线板端的 SN 与私钥已经烧写在 RK3588 的 Vendor Storage（裸 eMMC 分区）中，再在文件系统保留副本既多余，也扩大了私钥泄露面。因此新增 `vendor-storage` 来源，直接读取硬件分区，私钥全程只在内存中出现。

## 2. 两种身份来源

通过配置项 `identity-source` 选择，缺省为 `file`。

| 模式 | 配置值 | SN 来源 | 私钥来源 | 适用场景 |
|---|---|---|---|---|
| 文件 | `file`（缺省） | `device-file` | `private-key` | 开发机、x86 模拟、调试 |
| 硬件 | `vendor-storage` | Vendor Storage item 1 | Vendor Storage item 101 | 产线 aarch64 板端 |

### 配置示例

硬件来源（产线板端）：

```ini
identity-source=vendor-storage
# 可选：覆盖库路径，缺省在动态库搜索路径中查找 libfactory_service.so.1
# factory-lib=/usr/lib/libfactory_service.so.1
state-dir=run/cloud-agent
work-dir=run/cloud-work
```

文件来源（开发机）：

```ini
identity-source=file
device-file=/home/liziang/DeviceAgent/run/device-keys/sn.txt
private-key=/home/liziang/DeviceAgent/run/device-keys/priv.pem
state-dir=run/cloud-agent
work-dir=run/cloud-work
```

配置项 `identity-source` 只接受 `file` 与 `vendor-storage`，其他值在配置加载阶段直接报错。

## 3. 启动时的 SN 读取流程

```
启动
 └─ identity-source == vendor-storage ?
     ├─ 否 → 读 device-file（单行 SN）
     └─ 是 → dlopen libfactory_service.so.1
         ├─ 成功
         │   ├─ 读 state-dir/sn.cache
         │   │   ├─ 命中且合法 → 直接使用（不访问裸 eMMC）
         │   │   └─ 未命中/非法 → fs_read_sn()
         │   │       ├─ 成功 → 写 sn.cache（0600 原子写）→ 使用
         │   │       └─ 尚未写入/内容不完整 → 每 10 秒重读；权限/I/O 等其他错误明确退出
         │   └─ 后续逻辑与 file 模式完全一致
         └─ 失败 → 告警
             ├─ 配置了 device-file → 回退 file 模式
             └─ 未配置 → FATAL 退出
```

引入 `sn.cache` 的原因：token 仍有效时，重启不需要读取裸 eMMC。这样做有两个好处：

1. 降低对硬件分区的访问频率，避免频繁打开只读设备
2. 普通权限启动时，只要缓存和 token 有效，就不会触发权限错误

缓存文件为明文 SN、权限 `0600`，与 token 同级敏感度。"配置文件中不写 SN 明文"的原则保持不变。

写入方式为临时文件 + `fsync` + `rename`，并带 `O_EXCL | O_NOFOLLOW`，避免断电或并发写入产生损坏文件。写缓存失败只告警、不阻断启动，下次启动会重新从硬件读取。

## 4. enroll 前重读 SN 的一致性校验

token 缺失、需要重新 enroll 时，会再次调用 `fs_read_sn()` 读取硬件 SN，并与当前内存中的 SN 比对：

- 一致：正常走签名换 Token 流程
- 不一致（例如产线重烧）：告警并输出变化前后的 SN，更新内存 SN、重写 `sn.cache`，并按新 SN 重建 token 路径 `state-dir/tokens/<SN>.token`

这样可以避免"SN 已更换但旧 token 仍被复用"导致的主题错配。

## 5. 私钥读取与内存处理

私钥只在 enroll 签名时读取，流程如下：

```
fs_read_privkey(buffer, FS_KEY_BUFSZ)
 └─ 成功 → 复制到 std::string
     ├─ OPENSSL_cleanse(原始缓冲区)
     ├─ 解析为 EVP_PKEY
     └─ OPENSSL_cleanse(临时字符串)
```

约束：

- 私钥**不落盘**，不写入任何状态目录
- 私钥**不进日志**，错误信息只描述失败原因，不包含密钥内容
- 缓冲区在解析完成后立即用 `OPENSSL_cleanse` 清零，两处（原始 buffer 和临时 string）都要清
- `EVP_PKEY` 由智能指针管理，用完释放

支持格式为 OpenSSH 新格式（`openssh-key-v1`）PEM 文本，无口令；加密私钥不支持。

## 6. 签名与 enroll 协议

从 Vendor Storage 读到的私钥用于生成 enroll 签名：

- 待签内容：`noetix-enroll-v1|<SN>`
- 算法：RSA-PSS + SHA-256 + MGF1-SHA256，salt 长度固定 32 字节
- 编码：标准 Base64，单行、无换行
- 请求体：严格只包含 `sn` 与 `signature` 两个字段

私钥始终留在设备内，PC 管理工具和云端都拿不到。

## 7. 动态库加载与回退

`libfactory_service.so.1` 通过 `dlopen(RTLD_NOW | RTLD_LOCAL)` 在运行时加载，仅依赖三个符号：

| 符号 | 作用 |
|---|---|
| `fs_read_sn` | 读 SN（Vendor Storage item 1） |
| `fs_read_privkey` | 读私钥（item 101） |
| `fs_strerror` | 状态码转可读文本 |

设计要点：

- 库缺失或缺少必需符号时，**告警并回退**到 `device-file` / `private-key` 模式，保证旧设备兼容
- 库句柄不 `dlclose`，随进程退出回收，避免卸载时机问题
- 库路径可用 `factory-lib` 覆盖，缺省在系统动态库搜索路径中查找

## 8. 错误处理与状态码映射

Vendor Storage 读取失败时，按状态码给出可操作的提示：

| 状态码 | 提示含义 |
|---|---|
| `FS_ERR_ITEM_NOT_FOUND` / `FS_ERR_SN_INVALID` / `FS_ERR_KEY_INVALID` | 产线未烧写 SN 或私钥，需先通过产线工具注入 |
| `FS_ERR_PERMISSION` | 无法访问裸 eMMC，需以 root 或 disk 组运行 |
| 其他 | 使用 `fs_strerror` 的描述，并附带状态码 |

SN 或私钥尚未写入、为空或格式不完整时，进程保持运行，每 10 秒重新读取，记录 `IDENTITY waiting`；补写完成后同一进程自动继续，不需要重启服务。等待期间不调用注册接口，且不会创建临时 Token 文件。SIGTERM/SIGINT 可正常停止等待。权限、设备 I/O、配置等其他错误仍明确报错；服务端 401/429 不属于身份等待，不在此处自动重试。已有 SN/Token 缓存时保留缓存启动行为，不额外读取私钥。

## 9. 权限与运行要求

- 读取裸 eMMC 设备（缺省 `/dev/mmcblk0`）需要 **root 身份或 disk 组**
- 可以用环境变量 `FACTORY_SERVICE_SN_DEV` 覆盖设备节点路径
- 以普通用户运行且无有效缓存/token 时，会直接 FATAL 退出，这是预期行为
- 生产环境建议由 systemd 以 root 启动，且部署目录归 root 所有

## 10. 安全约束汇总

1. 私钥永不落盘、不进日志、不离开设备
2. SN 缓存文件权限 `0600`，原子写入
3. 配置文件内不写 SN 明文
4. enroll 响应日志中 token、签名、私钥等敏感字段一律脱敏
5. 库加载失败不静默降级，必须输出告警

## 11. 涉及代码位置

| 位置 | 内容 |
|---|---|
| `src/factory_service.h` | Vendor Storage 只读 API 声明、物品 ID 与状态码 |
| `src/main.cpp`：`FactoryService` / `load_factory_service` | 动态库加载与符号解析 |
| `src/main.cpp`：`vendor_error` | 状态码到可操作提示的映射 |
| `src/main.cpp`：`vendor_sn` | 读 SN、去空白、标识符校验 |
| `src/main.cpp`：`vendor_private_key` | 读私钥、内存解析、缓冲区清零 |
| `src/main.cpp`：`read_sn_cache` / `write_sn_cache` | SN 缓存读写（0600 原子写） |
| `src/main.cpp`：`load_device` | 两种来源的选择与回退逻辑 |
| `src/main.cpp`：`resolve_token` | enroll 前重读 SN 与 token 路径重建 |
| `src/main.cpp`：`make_enroll_signature` | RSA-PSS 签名与 Base64 编码 |
| `src/main.cpp`：`load_options` | `identity-source` / `factory-lib` 解析与取值校验 |

## 12. 验证方法

以 root 在板端运行：

```bash
cd /path/to/device-agent && ./device-agent
```

正常日志序列：

```
IDENTITY loaded sn from cache: run/cloud-agent/sn.cache     # 或首次从硬件读取
AUTH loaded cached device token: run/cloud-agent/tokens/<SN>.token
```

权限不足时的典型错误：

```
FATAL cannot access vendor storage; run as root or with the disk group to read the raw eMMC device
```

未烧写时的典型错误：

```
FATAL vendor storage has no SN or private key provisioned; inject it via the factory line first
```

删除 `state-dir/sn.cache` 与对应 token 后重启，可以验证完整的"硬件读取 → 写入缓存 → enroll 签名"路径。
