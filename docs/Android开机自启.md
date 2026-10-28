# Android 服务集成

init/vendor.logagent.rc 是固件集成模板，不是可直接安装的通用脚本。
默认程序 /vendor/bin/device-agent，服务用户 system、组 system inet，默认数据目录 /data/vendor/logagent。
sepolicy/ 中的域和 file_contexts 必须编入设备策略，并匹配实际路径。
密钥放在其他安全存储时，还要为实际密钥标签配置最小读取权限；模板不包含 /nv 的授权。
正式集成前检查设备已有同名服务。测试使用独立服务名、目录和可执行文件，不改 /vendor 或旧 logagent。
本包不自动启动、不创建身份、不复制私钥或 Token。
