#pragma once
// WSS 隧道：在 127.0.0.1 上监听明文 TCP，把字节流通过 WebSocket 转发到 wss:// 端点。
// 用于 libmosquitto 客户端不支持 WebSocket 的场景；MQTT 报文作为二进制帧透明传输。
#include <functional>
#include <memory>
#include <string>

namespace ws_tunnel {

struct Config {
    std::string url;      // 仅支持 wss://host[:port]/path
    std::string ca_path;  // 可选 PEM CA 证书包；为空时使用系统默认信任库
    std::function<void(const std::string&)> log; // 可选日志回调
};

class Tunnel {
public:
    explicit Tunnel(Config config);
    ~Tunnel();
    Tunnel(const Tunnel&) = delete;
    Tunnel& operator=(const Tunnel&) = delete;

    // 绑定 127.0.0.1 的随机端口并开始接受连接；端口可通过 port() 获取。
    void start();
    void stop();
    int port() const;

private:
    struct Impl;
    std::unique_ptr<Impl> impl;
};

} // namespace ws_tunnel
