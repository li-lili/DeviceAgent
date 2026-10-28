# Android 12 ARM64 部署

本包目标 arm64-v8a、API 31。先检查 BUILD-INFO.txt 的运行目录和 SHA256SUMS。
程序启动时切换到编译指定的 DEVICE_AGENT_ANDROID_DATA_DIR，再读取 device-agent.conf；不接受命令行参数。
system 目录模板用于固件集成；隔离测试版本必须使用自己的数据目录和输出路径，不得覆盖旧服务。

## 配置

config/device-agent.conf 选择 cloud。部署时创建 device-agent.cloud.conf，填写已授权 SN、已有配套私钥路径、enroll-url、host/port、sources-config、state-dir 和 work-dir。
私钥不在包内，必须经单独授权的受控流程配置，不能从示例生成、打印或上传；不可把另一身份的 SN 与密钥混用。
HTTPS 必须验证证书和主机名。包内 ca-bundle.pem 是构建机维护的公共 CA 集合，不是 localhost 测试 CA。
NDK curl/OpenSSL 不能假定自动使用 Android Java 信任库，因此模板显式配置 https-ca。
MQTT 示例为 1883，未启用 TLS；若服务端提供 TLS，须确认端口与 ca 配置。

## 权限与验证

运行目录仅服务用户可访问，私钥 0400/0600，Token 0600；状态和工作目录必须可写，公钥证书可读。
同一 SN 是 MQTT Client ID，测试前停止其他使用同一身份的客户端。
先用独立空业务日志目录验证 enroll、Token 保存、订阅、hello_reply 和 READY。
全新测试状态目录可验证实际签名；已有 Token 会优先加载，当前代码不自动刷新过期 Token。
运行身份、SELinux 域和文件标签必须由平台集成并验证，禁止用关闭 SELinux 代替权限配置。

## 回滚

停止新测试进程，撤销新增启动项，恢复原配置和标签；保留原服务、原身份及状态数据。
云端新 Token 是否影响旧 Token 需事先确认，本地回滚不能撤销云端令牌变更。
