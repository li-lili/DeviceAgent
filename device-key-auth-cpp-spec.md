# 设备端 C++ 对接规格：SN + RSA 签名换 Token

> **版本**：v1.0  
> **适用**：log-server 设备身份验签方案（替代现有注册码轨道）  
> **状态**：待评审  

本文档是设备端（C++）对接 log-server 的**精确规格**——每个参数的来源、生成步骤、算法名、示例数据全部钉死，不允许有任何歧义。设备端同事按本文档实现即可，服务端按相同规则校验。

> **重要前提**：密钥对在**出厂阶段**已由产线工具/固件内置工具/专用 PC 预先生成，私钥写入设备安全存储（文件系统加密分区 / 安全芯片）。**log-server 不参与、不关心密钥对生成过程**，设备固件启动时**只读取不生成**。

---

## 一、整体流程（两步）

```
步骤1：绑定（一次性，log-server 只收不生成密钥对）
  调用方（设备固件 / 产线工具均可）: POST /v1/device-keys/bind {sn, public_key(PEM)}
  log-server: 校验白名单 → 存 device_keys → 201

步骤2：换 Token（每次需要获取或续期 Token 时）
  设备读私钥 → 拼 payload → RSA 签名 → Base64 编码 → POST /v1/enroll → 拿 Token 连 MQTT
```

---

## 二、接口 1：公钥绑定（一次性）

### 请求

```
POST https://{log-server}/v1/device-keys/bind
Content-Type: application/json
```

### 请求体（2 个字段）

| 字段 | 类型 | 必填 | 来源 | 格式 |
|---|---|---|---|---|
| `sn` | string | ✅ | 硬件序列号（产线写入） | 1–64 字符，仅字母、数字、`.`、`_`、`-` |
| `public_key` | string | ✅ | 出厂阶段预先生成的公钥 PEM | PEM 全文，含 `-----BEGIN PUBLIC KEY-----` 和 `-----END PUBLIC KEY-----` 两行，中间标准 Base64（每行 64 字符） |

### public_key 示例（完整 PEM 文本）

```
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2K3m7vR9JcHn5LpX8wYq
R5tN1vB4sK6dF3gH9jM2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH
0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC
1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR
2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF
8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT
8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW
-----END PUBLIC KEY-----
```

> **注意**：PEM 文本里的换行必须保留——C++ 端直接把 `public.pem` 文件读成完整字符串（包括换行符）塞进 `public_key` 字段。不要把中间的 Base64 拼接成一行。

### 请求体示例

```json
{
  "sn":         "A1-000042",
  "public_key": "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA2K3m7vR9JcHn5LpX8wYqR5tN1vB4sK6dF3gH9jM2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH0jK2pL7wN5uT8vC1iF4aQ6sD8eW0gR2tY7uI9oP4aS6dF8gH\n-----END PUBLIC KEY-----"
}
```

### 响应

| HTTP | 含义 | 响应体 |
|---|---|---|
| 201 | 首次绑定成功 | `{"sn":"A1-000042","status":"bound"}` |
| 200 | 幂等重报（同 SN 同公钥，设备没收到上次响应重试） | `{"sn":"A1-000042","status":"already_bound"}` |
| 400 | 参数非法（sn 格式错 / public_key 解析失败 / 非 RSA-2048） | `{"error":"bad_request"}` |
| 401 | SN 不在白名单 | `{"error":"unauthorized"}` |
| 409 | SN 已绑定**不同**公钥（疑似被抢注） | `{"error":"sn_key_mismatch"}` |
| 429 | 触发限流（IP 或坏 SN 累计） | `{"error":"rate_limited"}` |

### C++ 端绑定完整代码（直接可用）

> **注意**：密钥对在出厂阶段已预先生成并写入设备安全存储（`/nv/priv.pem` 和 `/nv/pub.pem`）。设备固件只**读取**这两个文件，**绝对不要**在固件里生成密钥对。

```cpp
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <stdio.h>
#include <string.h>
#include <string>
#include <stdlib.h>

// 密钥文件路径（按设备安全存储调整，必须与产线写入路径一致）
#define PRIV_PATH "/nv/priv.pem"
#define PUB_PATH  "/nv/pub.pem"

// 读私钥（出厂时已落盘，固件只读不生成）
// 私钥文件不存在 → fatal error（设备身份丢失，上报产线）
static EVP_PKEY* read_private_key() {
    FILE* f = fopen(PRIV_PATH, "r");
    if (!f) {
        fprintf(stderr, "FATAL: private key not found at %s\n", PRIV_PATH);
        // 你们项目里的 fatal / assert / crash 处理放这里
        exit(1);
    }
    EVP_PKEY* k = PEM_read_PrivateKey(f, NULL, NULL, NULL);
    fclose(f);
    if (!k) {
        fprintf(stderr, "FATAL: failed to parse private key at %s\n", PRIV_PATH);
        exit(1);
    }
    return k;
}

// 读公钥 PEM 文本（接口 1 要上传的就是这份文本）
static std::string read_public_key_pem() {
    FILE* f = fopen(PUB_PATH, "r");
    if (!f) {
        fprintf(stderr, "FATAL: public key not found at %s\n", PUB_PATH);
        exit(1);
    }
    char buf[4096];
    size_t n = fread(buf, 1, sizeof(buf) - 1, f);
    fclose(f);
    buf[n] = 0;
    return std::string(buf);
}

// 绑定接口调用（用你们项目的 HTTP 库发 POST）
// POST /v1/device-keys/bind
// body: {"sn":"A1-000042","public_key":"<PEM全文>"}
// 成功条件: HTTP 200 或 201
```

---

## 三、接口 2：验签换 Token（每次需要获取或续期 Token 时）

### 请求

```
POST https://{log-server}/v1/enroll
Content-Type: application/json
```

### 请求体（2 个字段）

| 字段 | 类型 | 必填 | 来源 | 格式 |
|---|---|---|---|---|
| `sn` | string | ✅ | 硬件序列号 | 和接口 1 上传的完全一致 |
| `signature` | string | ✅ | **设备端用私钥算出来的签名结果（Base64 编码）** | 标准 Base64（`A-Z a-z 0-9 + / =`），**无换行**，约 344 字符 |

### 不请求的字段

- **不传 payload**——payload 由服务端按固定规则拼，和设备端拼的一模一样
- **不传 private_key**——私钥永不出设备
- **不传 nonce**——当前方案不防重放（靠 enroll 的 already_enrolled 判定兜底）

---

## 四、signature 是怎么生成的（精确到每一步）

signature 是设备端**用私钥对 payload 做 RSA 签名**的结果。三步运算：

### 第 1 步：拼 payload（一个字符串）

**payload 就是一行固定拼接的 UTF-8 字符串，没有多余的东西。**

```
payload = "noetix-enroll-v1|" + sn
```

- `noetix-enroll-v1|` 是**固定前缀**（域分隔），防签名被挪用到别的接口
- `|` 是 ASCII 0x7C 竖线分隔符，**就是单个竖线字符**，不是两个竖线，没有空格，没有冒号
- sn 就是请求体里要传的那个 sn，原样拼进去，**不转义、不编码**
- 整个 payload 转成 UTF-8 字节序列（英文数字就是 ASCII，字节值相同）

### payload 示例

sn = `A1-000042`，payload 就是：

```
noetix-enroll-v1|A1-000042
```

数一下字符：`noetix-enroll-v1|`（18 个） + `A1-000042`（9 个） = **27 字符**。UTF-8 编码后就是 27 字节的 ASCII 序列（全是英文数字和符号，没有多字节字符）。

### 第 2 步：RSA-PSS 签名（一个函数调用）

用设备的 **private.pem** 私钥，对 payload 的 UTF-8 字节做签名：

```
sign_bytes = RSA-PSS-Sign(
    private_key = private.pem,           // 私钥文件
    message     = payload_utf8_bytes,    // 第1步的 UTF-8 字节
    hash        = SHA-256,
    padding     = PSS,
    mgf         = MGF1-SHA256,
    salt_length = digest_len (32字节)    // = SHA-256 输出长度
)
```

输出：**256 字节二进制**（2048 位密钥固定输出 256 字节，这是 RSA 的数学特性）。

### 第 3 步：Base64 标准编码（一个函数调用）

256 字节二进制 → 可打印字符串，用**标准 Base64**（带 `+` `/` `=`，**无换行**）：

```
signature = Base64_Standard_Encode(sign_bytes)
```

输出：**约 344 字符**（256 ÷ 3 × 4 = 341.33，向上取整到 344）。

### Python 伪代码（C++ 同事能看懂逻辑）

```python
import hashlib, base64
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes, serialization

def make_signature(sn: str, priv_pem_path: str) -> str:
    # 第1步：拼 payload，转 UTF-8
    payload = f"noetix-enroll-v1|{sn}".encode("utf-8")  # 27 字节
    # （payload 本身明文，不加密，不压缩）

    # 第2步：RSA-PSS 私钥签名（内部自动先做 SHA-256 hash 再签名）
    priv = serialization.load_pem_private_key(open(priv_pem_path, "rb").read(), password=None)
    sign_bytes = priv.sign(
        payload,
        padding.PSS(
            mgf=padding.MGF1(hashes.SHA256()),
            salt_length=padding.PSS.MAX_LENGTH   # 设备端写 MAX_LENGTH 最稳
        ),
        hashes.SHA256()
    )
    # sign_bytes = 256 字节二进制

    # 第3步：Base64 标准编码（无换行）
    signature = base64.b64encode(sign_bytes).decode("ascii")
    # signature = 约 344 字符 ASCII

    return signature   # 这就是请求体里的 signature 字段
```

### C++ 完整签名代码（直接可用，OpenSSL 3.0+）

```cpp
#include <openssl/evp.h>
#include <openssl/pem.h>
#include <openssl/bio.h>
#include <openssl/core_names.h>
#include <openssl/params.h>
#include <openssl/err.h>
#include <string>
#include <vector>
#include <cstring>

std::string make_enroll_signature(const std::string& sn) {
    // 1. 读私钥（从接口1落盘的文件）
    FILE* f = fopen(PRIV_PATH, "r");
    if (!f) { /* error: 私钥不存在 */ }
    EVP_PKEY* priv = PEM_read_PrivateKey(f, NULL, NULL, NULL);
    fclose(f);

    // 2. 拼 payload（和服务端拼的一字不差）
    std::string payload = "noetix-enroll-v1|" + sn;

    // 3. 签名（EVP_DigestSign* 自动做 SHA-256 + RSA-PSS）
    EVP_MD_CTX* ctx = EVP_MD_CTX_new();
    EVP_PKEY_CTX* pctx = NULL;

    // 初始化：指定 SHA-256
    int ok = EVP_DigestSignInit_ex(ctx, &pctx, EVP_sha256(), NULL, priv);
    if (ok <= 0) { /* error */ }

    // 配置 RSA-PSS 参数（关键！服务端用的就是这套参数）
    OSSL_PARAM params[4];
    params[0] = OSSL_PARAM_construct_utf8_string(
        OSSL_PKEY_PARAM_PAD_MODE, (char*)"pss", 0);   // PSS padding
    params[1] = OSSL_PARAM_construct_utf8_string(
        OSSL_PKEY_PARAM_MGF1_DIGEST, (char*)"SHA256", 0);  // MGF1-SHA256
    params[2] = OSSL_PARAM_construct_utf8_string(
        OSSL_PKEY_PARAM_DIGEST, (char*)"SHA256", 0);   // 主 hash
    params[3] = OSSL_PARAM_construct_end();
    ok = EVP_PKEY_CTX_set_params(pctx, params);
    if (ok <= 0) { /* error */ }

    // 喂 payload 数据
    EVP_DigestSignUpdate(ctx, payload.data(), payload.size());

    // 先拿签名长度，再签名
    size_t sign_len = 0;
    EVP_DigestSignFinal(ctx, NULL, &sign_len);        // sign_len 应该是 256
    std::vector<unsigned char> sign_bytes(sign_len);
    EVP_DigestSignFinal(ctx, sign_bytes.data(), &sign_len);
    EVP_MD_CTX_free(ctx);
    EVP_PKEY_free(priv);

    // 4. Base64 标准编码（无换行）
    BIO* b64 = BIO_new(BIO_f_base64());
    BIO* mem = BIO_new(BIO_s_mem());
    BIO_set_flags(b64, BIO_FLAGS_BASE64_NO_NL);   // 关键：无换行
    BIO_push(b64, mem);
    BIO_write(b64, sign_bytes.data(), (int)sign_len);
    BIO_flush(b64);
    BUF_MEM* buf;
    BIO_get_mem_ptr(b64, &buf);
    std::string signature(buf->data, buf->length);
    BIO_free_all(b64);

    return signature;   // 返回值直接放 JSON body 的 signature 字段
}
```

### 完整接口 2 调用代码

```cpp
void enroll_for_token(const std::string& sn) {
    // 1. 确保私钥可读（出厂时已落盘，固件只读不生成）
    read_private_key();

    // 2. 算签名（三步合一的一行调用）
    std::string sig = make_enroll_signature(sn);

    // 3. 组装 JSON 请求体（用你们项目的 JSON 库）
    // {"sn":"A1-000042","signature":"<sig>"}

    // 4. POST /v1/enroll（用你们项目的 HTTP 库）
    //    Content-Type: application/json

    // 5. 解析响应
    // HTTP 201/200 + {"device_id":"A1-000042","token":"lsd_x9K2mP5...","reissued":false}
    //    → 把 token 落盘，用它连 MQTT（client_id=sn, password=token）
    // HTTP 401 → 签名不对或 SN 没绑，上报产线
    // HTTP 409 → 设备已上线（可能已被其他身份体系接管）
    // HTTP 429 → 限流，等几秒重试
}
```

---

## 五、完整参数映射表（给 C++ 同事的快速参考）

### 接口 2 请求体每个字段

| 字段 | 值 | 来源 | 怎么来的 |
|---|---|---|---|
| `sn` | `"A1-000042"` | 产线烧录的硬件序列号 | 从硬件读 |
| `signature` | `约344字符` | 设备端用私钥算出来的签名 | **三步**：①拼payload ②RSA-PSS签名 ③Base64编码 |

### 服务端验签的关键参数（必须完全一致，一字不差）

| 项 | 设备端设置 | 服务端设置 | 备注 |
|---|---|---|---|
| payload 拼接 | `"noetix-enroll-v1\|" + sn` | `"noetix-enroll-v1\|" + sn` | **完全一致才能验签通过** |
| payload 编码 | UTF-8 | UTF-8 | sn 是英文数字就是纯 ASCII |
| 签名算法 | RSA-PSS | RSA-PSS | PKCS#1 v1.5 不行 |
| 主 hash | SHA-256 | SHA-256 | SHA-1 不行 |
| MGF1 hash | SHA-256 | SHA-256 | MGF1-SHA1 不行 |
| salt 长度 | `MAX_LENGTH`（= SHA-256 输出 = 32） | `equals_hash` | 两者等价，都是 32 字节 |
| Base64 格式 | 标准（带 +/=）、**无换行** | 标准、无换行 | URL-safe（-_）不行 |
| 密钥格式 | PEM PKCS#8（不加密） | PEM PKIX（公钥） | 私钥 PKCS#8，公钥 PKIX |

### C++ OpenSSL 配置参数对应表

| OpenSSL 参数 | 值 | 含义 |
|---|---|---|
| `OSSL_PKEY_PARAM_PAD_MODE` | `"pss"` | RSA-PSS padding |
| `OSSL_PKEY_PARAM_MGF1_DIGEST` | `"SHA256"` | MGF1 内部 hash |
| `OSSL_PKEY_PARAM_DIGEST` | `"SHA256"` | 主签名 hash |
| `EVP_MD_CTX` init | `EVP_sha256()` | 同上，冗余配置也对 |
| `BIO_FLAGS_BASE64_NO_NL` | 设 | Base64 输出无换行 |

---

## 六、错误处理

| HTTP | 设备端应该做什么 |
|---|---|
| 201 / 200 | 解析 token 落盘 → MQTT 连接 |
| 400 | 上报产线（sn 格式错 / signature 无法 base64 解码） |
| 401 | 上报产线（SN 没绑定 / 签名算错——大概率 payload 拼错） |
| 409 | 上报产线（设备已被其他身份体系接管） |
| 429 | 等 10 秒后重试（指数退避） |
| 5xx | 网络抖动 → 重试 |

---

## 七、Python 模拟（C++ 同事可以在本地跑通逻辑再写 C++）

```bash
pip install cryptography
python sign_sim.py
```

```python
# sign_sim.py - 模拟设备端签名 + 服务端验签的完整链路
import hashlib, base64
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization

# ======== 模拟设备端（C++ 要做的事）========
# 注意：这只是本地测试用的模拟代码，实际产品里密钥对由产线工具预先生成
# 1. 模拟：生成密钥对（实际产品由产线工具完成，固件只读文件）
priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pub_pem = priv.public_key().public_bytes(
    serialization.Encoding.PEM,
    serialization.PublicFormat.SubjectPublicKeyInfo)
priv_pem = priv.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())

print("=== 公钥（接口1要上传的）===")
print(pub_pem.decode())

# 2. 接口2签名
sn = "A1-000042"
payload = f"noetix-enroll-v1|{sn}".encode()            # ① 拼 payload + UTF-8
sign_bytes = priv.sign(                                 # ② RSA-PSS 私钥签名
    payload,
    padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.MAX_LENGTH),
    hashes.SHA256())
signature = base64.b64encode(sign_bytes).decode()       # ③ Base64 标准编码

print(f"\nsn:        {sn}")
print(f"payload:   {payload.decode()}")
print(f"sign_bytes长度: {len(sign_bytes)} 字节 (应为 256)")
print(f"signature: {signature}")
print(f"signature长度: {len(signature)} 字符 (应为 ~344)")

# ======== 模拟服务端（Go 端要做的事）========
pub = serialization.load_pem_public_key(pub_pem)
# 服务端自己拼 payload（和设备拼的一模一样！）
server_payload = f"noetix-enroll-v1|{sn}".encode()

try:
    pub.verify(
        base64.b64decode(signature),
        server_payload,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                    salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256())
    print("\n✅ 服务端验签通过！")
except Exception as e:
    print(f"\n❌ 验签失败: {e}")

# 篡改 payload 看看
tampered = f"noetix-enroll-v1|A1-000043".encode()
try:
    pub.verify(base64.b64decode(signature), tampered,
               padding.PSS(mgf=padding.MGF1(hashes.SHA256()),
                           salt_length=padding.PSS.MAX_LENGTH),
               hashes.SHA256())
    print("❌ 篡改后居然通过了（bug）")
except Exception as e:
    print(f"✅ 篡改 payload 后验签失败（正常）: {type(e).__name__}")
```

运行输出：
```
=== 公钥（接口1要上传的）===
-----BEGIN PUBLIC KEY-----
MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEA...
-----END PUBLIC KEY-----

sn:        A1-000042
payload:   noetix-enroll-v1|A1-000042
sign_bytes长度: 256 字节 (应为 256)
signature: BJn3Kf8vLm2pQw9x...（约344字符）
signature长度: 344 字符 (应为 ~344)

✅ 服务端验签通过！
✅ 篡改 payload 后验签失败（正常）: InvalidSignature
```

---

## 八、总结：C++ 同事要做的事就这些

| 序号 | 做什么 | 代码位置 |
|---|---|---|
| 1 | 写 `read_private_key()` | 读私钥文件（出厂时已落盘），不存在则 fatal |
| 2 | 写 `read_public_key_pem()` | 接口 1 用 |
| 3 | 写 `make_enroll_signature(sn)` | 接口 2 核心——三步合一，**参数严格按第五节表来** |
| 4 | 调接口 1 绑公钥 | `POST /v1/device-keys/bind` |
| 5 | 调接口 2 换 Token | `POST /v1/enroll` |
| 6 | Token 落盘 → MQTT 连接 | 现有链路，零改动 |

**最容易踩的 3 个坑**：
1. payload 拼接多了空格/少了竖线 —— → 验签必失败 401
2. PSS 参数没设全（缺 `OSSL_PKEY_PARAM_MGF1_DIGEST` 等） —— → 验签必失败
3. Base64 用了 `BIO_FLAGS_BASE64_NO_NL` 以外的（有换行 / URL-safe） —— → 验签必失败

文档到此完全精确，C++ 同事按第五节的参数映射表 + C++ 代码直接实现即可。
