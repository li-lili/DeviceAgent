// WSS 隧道实现：TLS 1.2+ 连接、WebSocket 客户端握手、二进制帧编解码、本地 TCP 双向转发。
// MQTT over WebSocket 的报文与二进制帧 payload 一一对应，因此无需理解 MQTT 协议，只做透明转发。
#include "ws_tunnel.h"

#include <arpa/inet.h>
#include <netdb.h>
#include <netinet/in.h>
#include <sys/socket.h>
#include <unistd.h>

#include <openssl/err.h>
#include <openssl/evp.h>
#include <openssl/rand.h>
#include <openssl/ssl.h>

#include <atomic>
#include <cctype>
#include <cerrno>
#include <cstdint>
#include <cstring>
#include <mutex>
#include <stdexcept>
#include <thread>
#include <vector>

namespace ws_tunnel {
namespace {

constexpr const char* kWebSocketGuid = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
constexpr size_t kMaxFrameBytes = 16u << 20; // 单帧上限，防止异常长度耗尽内存。

struct Endpoint {
    std::string host;
    std::string path;
    int port = 443;
};

// 只接受 wss:// 前缀；host 可为域名或 IPv4/IPv6 字面量，path 缺省为 /。
Endpoint parse_url(const std::string& url) {
    const std::string prefix = "wss://";
    if(url.rfind(prefix,0)!=0)throw std::runtime_error("mqtt-url must start with wss://");
    std::string rest=url.substr(prefix.size());
    if(rest.empty())throw std::runtime_error("mqtt-url has no host");
    Endpoint ep;
    auto slash=rest.find('/');
    std::string authority=slash==std::string::npos?rest:rest.substr(0,slash);
    ep.path=slash==std::string::npos?"/":rest.substr(slash);
    if(authority.empty())throw std::runtime_error("mqtt-url has no host");
    auto colon=authority.rfind(':');
    if(colon!=std::string::npos) {
        ep.host=authority.substr(0,colon);
        std::string port=authority.substr(colon+1);
        size_t used=0;
        int parsed=0;
        try{parsed=std::stoi(port,&used);}catch(const std::exception&){throw std::runtime_error("invalid port in mqtt-url");}
        if(port.empty()||used!=port.size()||parsed<1||parsed>65535)throw std::runtime_error("invalid port in mqtt-url");
        ep.port=parsed;
    } else {
        ep.host=authority;
    }
    if(ep.host.empty())throw std::runtime_error("mqtt-url has no host");
    if(ep.path.empty())ep.path="/";
    return ep;
}

std::string base64_encode(const unsigned char* data, size_t len) {
    std::string out(4*((len+2)/3),'\0');
    int written=EVP_EncodeBlock(reinterpret_cast<unsigned char*>(&out[0]),data,static_cast<int>(len));
    if(written<0)throw std::runtime_error("base64 encode failed");
    out.resize(static_cast<size_t>(written));
    return out;
}

// Sec-WebSocket-Accept = base64(SHA1(client_key + GUID))。
std::string websocket_accept(const std::string& client_key) {
    std::string input=client_key+kWebSocketGuid;
    unsigned char digest[EVP_MAX_MD_SIZE];
    unsigned int length=0;
    if(EVP_Digest(input.data(),input.size(),digest,&length,EVP_sha1(),nullptr)!=1)
        throw std::runtime_error("SHA1 failed");
    return base64_encode(digest,length);
}

std::string openssl_error() {
    unsigned long code=ERR_get_error();
    if(!code)return "unknown TLS error";
    char buffer[256];
    ERR_error_string_n(code,buffer,sizeof buffer);
    return buffer;
}

bool tcp_write_all(int fd,const void* data,size_t len) {
    auto* p=static_cast<const unsigned char*>(data);
    while(len>0) {
        ssize_t n=send(fd,p,len,MSG_NOSIGNAL);
        if(n<=0)return false;
        p+=n;len-=static_cast<size_t>(n);
    }
    return true;
}

bool ssl_write_all(SSL* ssl,const void* data,size_t len) {
    auto* p=static_cast<const unsigned char*>(data);
    while(len>0) {
        int n=SSL_write(ssl,p,static_cast<int>(len));
        if(n<=0)return false;
        p+=n;len-=static_cast<size_t>(n);
    }
    return true;
}

bool ssl_read_exact(SSL* ssl,void* data,size_t len) {
    auto* p=static_cast<unsigned char*>(data);
    while(len>0) {
        int n=SSL_read(ssl,p,static_cast<int>(len));
        if(n<=0)return false;
        p+=n;len-=static_cast<size_t>(n);
    }
    return true;
}

int tcp_connect(const Endpoint& ep) {
    addrinfo hints{};
    hints.ai_family=AF_UNSPEC;
    hints.ai_socktype=SOCK_STREAM;
    addrinfo* result=nullptr;
    std::string port=std::to_string(ep.port);
    int rc=getaddrinfo(ep.host.c_str(),port.c_str(),&hints,&result);
    if(rc!=0)throw std::runtime_error("cannot resolve "+ep.host+": "+gai_strerror(rc));
    int fd=-1;
    std::string last_error;
    for(addrinfo* entry=result;entry;entry=entry->ai_next) {
        fd=socket(entry->ai_family,entry->ai_socktype,entry->ai_protocol);
        if(fd<0){last_error=std::strerror(errno);continue;}
        if(connect(fd,entry->ai_addr,entry->ai_addrlen)==0)break;
        last_error=std::strerror(errno);
        close(fd);fd=-1;
    }
    freeaddrinfo(result);
    if(fd<0)throw std::runtime_error("cannot connect "+ep.host+":"+port+(last_error.empty()?"":": "+last_error));
    return fd;
}

SSL_CTX* make_context(const std::string& ca_path) {
    SSL_CTX* ctx=SSL_CTX_new(TLS_client_method());
    if(!ctx)throw std::runtime_error("cannot create TLS context: "+openssl_error());
    SSL_CTX_set_min_proto_version(ctx,TLS1_2_VERSION);
    int rc=ca_path.empty()?SSL_CTX_set_default_verify_paths(ctx)
                          :SSL_CTX_load_verify_locations(ctx,ca_path.c_str(),nullptr);
    if(rc!=1) {
        std::string message=openssl_error();
        SSL_CTX_free(ctx);
        throw std::runtime_error("cannot load CA certificates: "+message);
    }
    SSL_CTX_set_verify(ctx,SSL_VERIFY_PEER,nullptr);
    return ctx;
}

// 发送一个客户端帧；客户端帧必须带掩码。
bool ws_send(SSL* ssl,int opcode,const unsigned char* payload,size_t len) {
    unsigned char header[14];
    size_t header_len=0;
    header[header_len++]=static_cast<unsigned char>(0x80|opcode);
    if(len<126) {
        header[header_len++]=static_cast<unsigned char>(0x80|len);
    } else if(len<=0xFFFF) {
        header[header_len++]=0x80|126;
        header[header_len++]=static_cast<unsigned char>((len>>8)&0xFF);
        header[header_len++]=static_cast<unsigned char>(len&0xFF);
    } else {
        header[header_len++]=0x80|127;
        for(int i=7;i>=0;--i)header[header_len++]=static_cast<unsigned char>((static_cast<uint64_t>(len)>>(8*i))&0xFF);
    }
    unsigned char mask[4];
    if(RAND_bytes(mask,sizeof mask)!=1)return false;
    std::memcpy(header+header_len,mask,sizeof mask);
    header_len+=sizeof mask;
    if(!ssl_write_all(ssl,header,header_len))return false;
    std::vector<unsigned char> masked(len);
    for(size_t i=0;i<len;++i)masked[i]=payload[i]^mask[i&3];
    return len==0||ssl_write_all(ssl,masked.data(),masked.size());
}

// 读取一条完整消息（自动合并分片）；控制帧单独返回。
bool ws_recv(SSL* ssl,std::vector<unsigned char>& message,int& opcode) {
    message.clear();
    int first_opcode=-1;
    for(;;) {
        unsigned char head[2];
        if(!ssl_read_exact(ssl,head,sizeof head))return false;
        bool fin=(head[0]&0x80)!=0;
        int op=head[0]&0x0F;
        bool masked=(head[1]&0x80)!=0;
        uint64_t length=head[1]&0x7F;
        if(length==126) {
            unsigned char extended[2];
            if(!ssl_read_exact(ssl,extended,sizeof extended))return false;
            length=(static_cast<uint64_t>(extended[0])<<8)|extended[1];
        } else if(length==127) {
            unsigned char extended[8];
            if(!ssl_read_exact(ssl,extended,sizeof extended))return false;
            length=0;
            for(unsigned char byte:extended)length=(length<<8)|byte;
        }
        if(length>kMaxFrameBytes)return false;
        unsigned char mask[4]={0,0,0,0};
        if(masked&&!ssl_read_exact(ssl,mask,sizeof mask))return false;
        std::vector<unsigned char> chunk(static_cast<size_t>(length));
        if(length>0&&!ssl_read_exact(ssl,chunk.data(),chunk.size()))return false;
        if(masked)for(size_t i=0;i<chunk.size();++i)chunk[i]^=mask[i&3];
        if(op==0x0) {
            if(first_opcode<0)return false; // 缺少起始帧
            message.insert(message.end(),chunk.begin(),chunk.end());
        } else {
            if(first_opcode>=0)return false; // 分片未结束又出现新帧
            first_opcode=op;
            message=std::move(chunk);
        }
        if(fin||op>=0x8) {
            opcode=first_opcode<0?op:first_opcode;
            return true;
        }
    }
}

bool ws_handshake(SSL* ssl,const Endpoint& ep) {
    unsigned char key[16];
    if(RAND_bytes(key,sizeof key)!=1)return false;
    std::string key64=base64_encode(key,sizeof key);
    std::string request="GET "+ep.path+" HTTP/1.1\r\n"
        "Host: "+ep.host+":"+std::to_string(ep.port)+"\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Key: "+key64+"\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Protocol: mqtt\r\n"
        "\r\n";
    if(!ssl_write_all(ssl,request.data(),request.size()))return false;
    // 逐字节读到响应头结束，避免吞掉后续 WebSocket 数据。
    std::string response;
    char byte=0;
    while(response.size()<8192) {
        if(SSL_read(ssl,&byte,1)!=1)return false;
        response.push_back(byte);
        if(response.size()>=4&&response.compare(response.size()-4,4,"\r\n\r\n")==0)break;
    }
    if(response.rfind("HTTP/1.1 101",0)!=0)return false;
    std::string expected=websocket_accept(key64);
    // 头部名按 RFC 7230 不区分大小写；EMQX 实际返回 Sec-Websocket-Accept。
    std::string lowered=response;
    for(char& c:lowered)c=static_cast<char>(std::tolower(static_cast<unsigned char>(c)));
    const std::string name="sec-websocket-accept:";
    auto pos=lowered.find(name);
    if(pos==std::string::npos)return false;
    pos+=name.size();
    while(pos<response.size()&&(response[pos]==' '||response[pos]=='\t'))++pos;
    if(response.compare(pos,expected.size(),expected)!=0)return false;
    return true;
}

} // namespace

struct Tunnel::Impl {
    Config config;
    Endpoint endpoint;
    SSL_CTX* context=nullptr;
    int listen_fd=-1;
    int bound_port=0;
    std::atomic<bool> stopped{false};
    std::atomic<int> active_fd{-1};
    std::thread accept_thread;

    void emit(const std::string& message) { if(config.log)config.log(message); }

    // 处理单个本地连接：建立到远端的 WSS 连接后双向转发，连接结束即关闭，由 MQTT 客户端触发重连。
    void serve(int client_fd) {
        int remote_fd=-1;
        SSL* ssl=nullptr;
        std::mutex write_mutex;
        try {
            remote_fd=tcp_connect(endpoint);
            ssl=SSL_new(context);
            if(!ssl)throw std::runtime_error("cannot create TLS session");
            SSL_set_fd(ssl,remote_fd);
            SSL_set_tlsext_host_name(ssl,endpoint.host.c_str());
            SSL_set1_host(ssl,endpoint.host.c_str());
            if(SSL_connect(ssl)!=1)throw std::runtime_error("TLS handshake failed: "+openssl_error());
            if(!ws_handshake(ssl,endpoint))throw std::runtime_error("WebSocket handshake failed");
            emit("WSS tunnel connected: "+config.url);
            // TCP → WebSocket：把本地明文 MQTT 字节流逐个写成二进制帧。
            std::atomic<bool> finished{false};
            std::thread uplink([&]{
                std::vector<unsigned char> buffer(16384);
                while(!stopped&&!finished) {
                    ssize_t n=recv(client_fd,buffer.data(),buffer.size(),0);
                    if(n<=0)break;
                    std::lock_guard<std::mutex> lock(write_mutex);
                    if(!ws_send(ssl,0x2,buffer.data(),static_cast<size_t>(n)))break;
                }
                finished=true;
                shutdown(client_fd,SHUT_RDWR);
            });
            // WebSocket → TCP：数据帧写入本地连接，ping 回 pong，close 结束。
            while(!stopped&&!finished) {
                std::vector<unsigned char> payload;
                int opcode=0;
                if(!ws_recv(ssl,payload,opcode))break;
                if(opcode==0x9) {
                    std::lock_guard<std::mutex> lock(write_mutex);
                    if(!ws_send(ssl,0xA,payload.data(),payload.size()))break;
                    continue;
                }
                if(opcode==0x8)break;
                if(opcode==0x1||opcode==0x2||opcode==0x0) {
                    if(!payload.empty()&&!tcp_write_all(client_fd,payload.data(),payload.size()))break;
                }
            }
            finished=true;
            shutdown(client_fd,SHUT_RDWR);
            uplink.join();
        } catch(const std::exception& e) {
            emit("WSS tunnel error: "+std::string(e.what()));
        }
        if(ssl){SSL_shutdown(ssl);SSL_free(ssl);}
        if(remote_fd>=0)close(remote_fd);
        close(client_fd);
        active_fd=-1;
    }

    void run() {
        while(!stopped) {
            sockaddr_in peer{};
            socklen_t length=sizeof peer;
            int client=accept(listen_fd,reinterpret_cast<sockaddr*>(&peer),&length);
            if(client<0) {
                if(stopped)break;
                if(errno==EINTR)continue;
                break;
            }
            active_fd=client;
            serve(client);
        }
    }
};

Tunnel::Tunnel(Config config) : impl(new Impl) {
    impl->config=std::move(config);
    impl->endpoint=parse_url(impl->config.url);
    impl->context=make_context(impl->config.ca_path);
}

Tunnel::~Tunnel() { stop(); }

void Tunnel::start() {
    if(impl->listen_fd>=0)return;
    int fd=socket(AF_INET,SOCK_STREAM,0);
    if(fd<0)throw std::runtime_error("cannot create tunnel socket");
    int reuse=1;
    setsockopt(fd,SOL_SOCKET,SO_REUSEADDR,&reuse,sizeof reuse);
    sockaddr_in address{};
    address.sin_family=AF_INET;
    address.sin_addr.s_addr=htonl(INADDR_LOOPBACK); // 只服务本机，不对外暴露。
    address.sin_port=0;                             // 由内核分配空闲端口。
    if(bind(fd,reinterpret_cast<sockaddr*>(&address),sizeof address)!=0||listen(fd,4)!=0) {
        close(fd);
        throw std::runtime_error("cannot listen on 127.0.0.1 for WSS tunnel");
    }
    socklen_t length=sizeof address;
    if(getsockname(fd,reinterpret_cast<sockaddr*>(&address),&length)!=0) {
        close(fd);
        throw std::runtime_error("cannot read tunnel port");
    }
    impl->listen_fd=fd;
    impl->bound_port=ntohs(address.sin_port);
    impl->accept_thread=std::thread([this]{impl->run();});
}

void Tunnel::stop() {
    if(!impl)return;
    bool expected=false;
    if(!impl->stopped.compare_exchange_strong(expected,true))return;
    if(impl->listen_fd>=0)shutdown(impl->listen_fd,SHUT_RDWR);
    int active=impl->active_fd.load();
    if(active>=0)shutdown(active,SHUT_RDWR);
    if(impl->accept_thread.joinable())impl->accept_thread.join();
    if(impl->listen_fd>=0){close(impl->listen_fd);impl->listen_fd=-1;}
    if(impl->context){SSL_CTX_free(impl->context);impl->context=nullptr;}
}

int Tunnel::port() const { return impl?impl->bound_port:0; }

} // namespace ws_tunnel
