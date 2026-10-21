#include "business.pb.h"
#include "cloud.pb.h"
#include <google/protobuf/struct.pb.h>
#include <google/protobuf/util/json_util.h>
#include <mosquitto.h>
#include <mqtt_protocol.h>
#include <curl/curl.h>
#include <archive.h>
#include <archive_entry.h>
#include <openssl/evp.h>
#include <sys/stat.h>
#include <fnmatch.h>
#include <unistd.h>
#include <fcntl.h>
#include <cerrno>
#include <algorithm>
#include <atomic>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <filesystem>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <map>
#include <mutex>
#include <memory>
#include <optional>
#include <regex>
#include <set>
#include <sstream>
#include <thread>
#include <vector>
#ifdef __ANDROID__
#include <android/log.h>
#endif

// 阅读路线：main() → Agent 构造函数 → run()；收到命令后由 work_loop() 调用 execute()。
// 采集流程：sources() → discover() → bundle() → upload() → result()。
// cloud_pb 对应云端协议，biz 对应日志来源协议；头文件由 proto 文件生成。
namespace fs = std::filesystem;
namespace cloud_pb = logserver::v1;
namespace biz = deviceagent::business::v1;
using Clock = std::chrono::system_clock;
// 主线程、MQTT 网络线程和工作线程共享退出标志；Ctrl+C / SIGTERM 只设置标志。
static std::atomic<bool> stopping{false};
static int64_t now_ns() { return std::chrono::duration_cast<std::chrono::nanoseconds>(Clock::now().time_since_epoch()).count(); }
static std::ofstream log_file;
static std::mutex log_mutex;
// 只区分 Agent 自身的运行日志级别，不解析或筛选业务日志内容。
enum class LogLevel { Info, Warning, Error };
// Android 使用 Logcat 的 DeviceAgent 标签；Ubuntu 输出到终端。两端保留文件记录。
static void log(const std::string& s, LogLevel level=LogLevel::Info) {
    std::lock_guard<std::mutex> lock(log_mutex);
#ifdef __ANDROID__
    const int priority=level==LogLevel::Error?ANDROID_LOG_ERROR:
        level==LogLevel::Warning?ANDROID_LOG_WARN:ANDROID_LOG_INFO;
    __android_log_write(priority,"DeviceAgent",s.c_str());
#else
    // 保持 Ubuntu 的原有输出行为；日志文件尚未打开时的致命错误走 stderr。
    auto& output=level==LogLevel::Error&&!log_file?std::cerr:std::cout;
    output << s << std::endl;
#endif
    if(log_file)log_file << s << std::endl;
}
// 带错误码的业务异常；code 回传云端，what() 提供具体失败原因。
struct Failure : std::runtime_error {
    std::string code;
    Failure(std::string c, std::string m) : std::runtime_error(m), code(std::move(c)) {}
};
// 限制命令、业务、来源 ID 的字符，避免路径分隔符进入状态文件名。
static bool identifier(const std::string& s) { return std::regex_match(s, std::regex("[A-Za-z0-9_.-]{1,64}")) && s != "." && s != ".."; }
// 按路径组件判断是否位于根目录内；调用方先规范化路径，不能只比较字符串前缀。
static bool inside(const fs::path& file, const fs::path& root) {
    auto f = file.begin(), r = root.begin();
    for (; r != root.end(); ++r, ++f) if (f == file.end() || *f != *r) return false;
    return true;
}
// 先写临时 PB 文件，再 rename 替换目标，减少状态只写一半的风险；此处未调用 fsync。
static void save_pb(const fs::path& path, const google::protobuf::Message& msg) {
    fs::create_directories(path.parent_path());
    auto tmp = path; tmp += ".tmp";
    { std::ofstream f(tmp, std::ios::binary); if (!msg.SerializeToOstream(&f)) throw Failure("DISK_FULL", "cannot save " + tmp.string()); f.flush(); if (!f) throw Failure("DISK_FULL", "write failed"); }
    fs::rename(tmp, path);
}
// 分块计算文件摘要，用于包内清单和上传校验，避免将整个文件读入内存。
static std::string sha256(const fs::path& path) {
    std::ifstream f(path, std::ios::binary);
    if (!f) throw Failure("READ_FAILED", "cannot hash file");
    auto* ctx = EVP_MD_CTX_new();
    if (!ctx) throw std::runtime_error("digest allocation failed");
    EVP_DigestInit_ex(ctx, EVP_sha256(), nullptr);
    char b[65536];
    while (f.read(b, sizeof b) || f.gcount()) EVP_DigestUpdate(ctx, b, static_cast<size_t>(f.gcount()));
    if (f.bad()) { EVP_MD_CTX_free(ctx); throw Failure("READ_FAILED", "hash read failed"); }
    unsigned char digest[EVP_MAX_MD_SIZE]; unsigned int n = 0;
    EVP_DigestFinal_ex(ctx, digest, &n); EVP_MD_CTX_free(ctx);
    std::ostringstream o; for (unsigned int i=0;i<n;++i) o << std::hex << std::setw(2) << std::setfill('0') << static_cast<int>(digest[i]);
    return o.str();
}
// 根据来源配置识别文件名中的日期。
// 文件日期按北京时间零点转换为纳秒；没有合法日期时返回 -1，不读取日志内容。
struct FilenameDateFormat {
    std::regex pattern;
    int year=0, month=0, day=0;
    explicit FilenameDateFormat(const std::string& format) {
        if(format.empty()||format.size()>256)throw Failure("INVALID","invalid filename date format length");
        std::string expression="(^|[^0-9])";
        int group=1;
        for(size_t i=0;i<format.size();++i) {
            char c=format[i];
            if(c=='%') {
                if(++i==format.size())throw Failure("INVALID","incomplete filename date directive");
                c=format[i];
                if(c!='%') {
                    int* field=c=='Y'?&year:c=='m'?&month:c=='d'?&day:nullptr;
                    if(!field||*field)throw Failure("INVALID","filename date format requires one %Y, %m and %d");
                    *field=++group;expression+=c=='Y'?"([0-9]{4})":"([0-9]{2})";
                    continue;
                }
            }
            if(std::string(R"(\.^$|()[]{}*+?)").find(c)!=std::string::npos)expression+='\\';
            expression+=c;
        }
        if(!year||!month||!day)throw Failure("INVALID","filename date format requires %Y, %m and %d");
        pattern=std::regex(expression+"([^0-9]|$)");
    }
};
static int64_t filename_day(const fs::path& path,const std::vector<FilenameDateFormat>& formats) {
    const std::string name=path.filename().string();
    for(const auto& format:formats) {
        std::smatch match;
        if(!std::regex_search(name,match,format.pattern))continue;
        int year=std::stoi(match[format.year]),month=std::stoi(match[format.month]),day=std::stoi(match[format.day]);
        if(year<1970||year>2261)continue;
        std::tm tm{};tm.tm_year=year-1900;tm.tm_mon=month-1;tm.tm_mday=day;
        auto seconds=timegm(&tm);
        if(seconds<0||tm.tm_year!=year-1900||tm.tm_mon!=month-1||tm.tm_mday!=day)continue;
        return (static_cast<int64_t>(seconds)-8*3600)*1000000000LL;
    }
    return -1;
}
// 启动设置由默认值和配置文件确定。sources_config 非空时直接从文件加载来源。
struct Options {
    std::string host, device, username, token, ca;
    std::string local_host, local_username, local_token, local_ca;
    std::string enroll_url, enrollment_key, model, serial;
    std::string https_ca; // 注册和对象存储上传使用的 PEM CA 证书包；与 MQTT ca 分开。
    fs::path token_file;
    int port=0;
    int local_port=0;
    fs::path state, work, sources_config, log_path;
    bool insecure_upload=false, insecure_enroll=false;
};
// 保留字符串和行位置，允许来源 JSON 携带注释。
// 只去除字符串外的 JSON 注释，保留换行及字符串内容，避免破坏路径和错误位置。
static std::string strip_json_comments(std::string text) {
    bool quoted=false, escaped=false;
    for(size_t i=0;i<text.size();++i) {
        char c=text[i];
        if(quoted) {
            if(escaped)escaped=false;
            else if(c=='\\')escaped=true;
            else if(c=='"')quoted=false;
            continue;
        }
        if(c=='"'){quoted=true;continue;}
        if(c!='/'||i+1==text.size())continue;
        if(text[i+1]=='/') {
            text[i++]=' ';text[i]=' ';
            while(i+1<text.size()&&text[i+1]!='\n'&&text[i+1]!='\r')text[++i]=' ';
        } else if(text[i+1]=='*') {
            text[i++]=' ';text[i]=' ';
            bool closed=false;
            while(++i<text.size()) {
                if(text[i]=='*'&&i+1<text.size()&&text[i+1]=='/') {
                    text[i++]=' ';text[i]=' ';closed=true;break;
                }
                if(text[i]!='\n'&&text[i]!='\r')text[i]=' ';
            }
            if(!closed)throw std::runtime_error("unterminated comment in sources config");
        }
    }
    return text;
}
// PB 仅用作内部数据结构；通过标准转换器生成云端 JSON，避免手写字段名或丢失整数精度。
static std::string cloud_json(const google::protobuf::Message& message) {
    std::string json;
    auto status=google::protobuf::util::MessageToJsonString(message,&json);
    if(!status.ok())throw std::runtime_error("cannot encode cloud JSON");
    return json;
}
// 容忍云端新增字段；无效 JSON 只记录消息类型，不输出可能包含上传凭证的正文。
static bool parse_cloud_json(const void* data,int length,google::protobuf::Message& message) {
    google::protobuf::util::JsonParseOptions options;
    options.ignore_unknown_fields=true;
    auto status=google::protobuf::util::JsonStringToMessage(
        std::string(static_cast<const char*>(data),static_cast<size_t>(length)),&message,options);
    if(!status.ok())log("Dropped invalid cloud JSON: "+std::string(message.GetTypeName()),LogLevel::Warning);
    return status.ok();
}

// Token 优先级与 Python 模拟器一致：配置 token → 已保存 token-file → 注册码换 Token。
// 文件存在但为空或无法读取时明确失败，避免误把缓存损坏当成新设备而重复注册。
static void resolve_token(Options& o) {
    auto valid_token=[](const std::string& token) {
        return !token.empty()&&token.size()<=65536
            &&token.find_first_of(" \t\r\n")==std::string::npos&&token.find('\0')==std::string::npos;
    };
    if(!o.token.empty()) {
        if(!valid_token(o.token))throw Failure("AUTH_FAILED","invalid configured token");
        if(o.username.empty())o.username=o.device;
        log("AUTH using configured device token");return;
    }
    bool enrollment_configured=!o.enrollment_key.empty()||!o.enroll_url.empty();
    if(o.token_file.empty()&&!enrollment_configured)return; // 本地匿名 MQTT 无需注册。
    fs::path token_path=o.token_file.empty()?o.state/"tokens"/(o.device+".token"):o.token_file;
    if(fs::exists(token_path)) {
        std::ifstream input(token_path,std::ios::binary);
        std::string token;
        if(!input)throw Failure("AUTH_FAILED","cannot read token file: "+token_path.string());
        char buffer[4096];
        while(input.read(buffer,sizeof buffer)||input.gcount()) {
            token.append(buffer,static_cast<size_t>(input.gcount()));
            if(token.size()>65536)throw Failure("AUTH_FAILED","token file is too large");
        }
        // 兼容手动保存文件时末尾的一次换行，但不容忍多个 Token 或空白内容。
        while(!token.empty()&&(token.back()=='\r'||token.back()=='\n'))token.pop_back();
        if(input.bad()||!valid_token(token))throw Failure("AUTH_FAILED","invalid token file: "+token_path.string());
        o.token=std::move(token);
        if(o.username.empty())o.username=o.device;
        log("AUTH loaded cached device token: "+token_path.string());return;
    }
    if(o.enrollment_key.empty()||o.enroll_url.empty())
        throw Failure("ENROLL_FAILED","token is missing; set enrollment-key and enroll-url");
    bool https=o.enroll_url.rfind("https://",0)==0;
    bool loopback=o.enroll_url.rfind("http://127.0.0.1:",0)==0||o.enroll_url.rfind("http://localhost:",0)==0;
    if(!https&&!(o.insecure_enroll&&loopback))
        throw Failure("ENROLL_FAILED","enroll-url requires HTTPS; loopback HTTP is only allowed in local tests");

    fs::path parent=fs::absolute(token_path).parent_path();
    fs::create_directories(parent);
    auto temporary=token_path;temporary+=".tmp."+std::to_string(getpid());
    // 在消费一次性注册码之前确认可以创建缓存文件；新 Token 文件仅所有者可读写。
    int fd=open(temporary.c_str(),O_WRONLY|O_CREAT|O_EXCL|O_NOFOLLOW,0600);
    if(fd<0)throw Failure("ENROLL_FAILED","cannot create token cache: "+token_path.string());
    try {
        google::protobuf::Struct body;
        auto& fields=*body.mutable_fields();
        fields["enrollment_key"].set_string_value(o.enrollment_key);
        fields["device_id"].set_string_value(o.device);
        fields["model"].set_string_value(o.model);
        fields["serial"].set_string_value(o.serial);
        std::string request=cloud_json(body),response;
        std::unique_ptr<CURL,decltype(&curl_easy_cleanup)> curl(curl_easy_init(),curl_easy_cleanup);
        if(!curl)throw Failure("ENROLL_FAILED","cannot initialize enrollment request");
        curl_slist* raw_headers=curl_slist_append(nullptr,"Content-Type: application/json");
        if(!raw_headers)throw std::runtime_error("cannot allocate HTTP headers");
        std::unique_ptr<curl_slist,decltype(&curl_slist_free_all)> headers(raw_headers,curl_slist_free_all);
        curl_easy_setopt(curl.get(),CURLOPT_URL,o.enroll_url.c_str());
        if(!o.https_ca.empty())curl_easy_setopt(curl.get(),CURLOPT_CAINFO,o.https_ca.c_str());
        curl_easy_setopt(curl.get(),CURLOPT_HTTPHEADER,headers.get());
        curl_easy_setopt(curl.get(),CURLOPT_POSTFIELDS,request.c_str());
        curl_easy_setopt(curl.get(),CURLOPT_POSTFIELDSIZE_LARGE,static_cast<curl_off_t>(request.size()));
        curl_easy_setopt(curl.get(),CURLOPT_CONNECTTIMEOUT,5L);
        curl_easy_setopt(curl.get(),CURLOPT_TIMEOUT,15L);
        curl_easy_setopt(curl.get(),CURLOPT_NOSIGNAL,1L);
        // 不跟随重定向，避免将注册码转发给其他地址；HTTPS 使用 curl 默认的证书校验。
        curl_easy_setopt(curl.get(),CURLOPT_PROTOCOLS,https?CURLPROTO_HTTPS:CURLPROTO_HTTP);
        curl_easy_setopt(curl.get(),CURLOPT_WRITEFUNCTION,+[](char* data,size_t size,size_t count,void* target)->size_t {
            auto& out=*static_cast<std::string*>(target);
            constexpr size_t maximum=1024*1024;
            if(size&&count>maximum/size)return 0;
            size_t length=size*count;
            if(length>maximum-out.size())return 0;
            try {out.append(data,length);}catch(...){return 0;}
            return length;
        });
        curl_easy_setopt(curl.get(),CURLOPT_WRITEDATA,&response);
        log("ENROLL requesting device token for "+o.device);
        auto result=curl_easy_perform(curl.get());
        long status=0;curl_easy_getinfo(curl.get(),CURLINFO_RESPONSE_CODE,&status);
        if(result!=CURLE_OK)throw Failure("ENROLL_FAILED","enrollment request failed: "+std::string(curl_easy_strerror(result)));
        google::protobuf::Struct reply;
        auto parsed=google::protobuf::util::JsonStringToMessage(response,&reply);
        if(status<200||status>=300) {
            std::string code="request_failed";
            if(parsed.ok()) {
                auto error=reply.fields().find("error");
                if(error!=reply.fields().end()&&error->second.kind_case()==google::protobuf::Value::kStructValue) {
                    const auto& info=error->second.struct_value().fields();
                    auto item=info.find("code");
                    if(item!=info.end()) {
                        // 只输出已知错误码，绝不将可能包含凭证的服务端正文写到日志。
                        const std::set<std::string> known={"invalid_enrollment_key","not_allowed","already_enrolled","rate_limited"};
                        if(known.count(item->second.string_value()))code=item->second.string_value();
                    }
                }
            }
            std::string hint=code=="already_enrolled"?"; device already registered: recover its token or ask administrator to allow re-enrollment":"";
            throw Failure("ENROLL_FAILED","HTTP "+std::to_string(status)+" "+code+hint);
        }
        auto token=reply.fields().find("token");
        if(!parsed.ok()||token==reply.fields().end()||token->second.kind_case()!=google::protobuf::Value::kStringValue
            ||!valid_token(token->second.string_value()))
            throw Failure("ENROLL_FAILED","enrollment response has no valid device token");
        const auto& value=token->second.string_value();
        size_t offset=0;
        while(offset<value.size()) {
            auto count=write(fd,value.data()+offset,value.size()-offset);
            if(count<0&&errno==EINTR)continue;
            if(count<=0)throw Failure("ENROLL_FAILED","cannot save newly issued token");
            offset+=static_cast<size_t>(count);
        }
        if(fsync(fd)!=0)throw Failure("ENROLL_FAILED","cannot sync newly issued token");
        int closed=close(fd);fd=-1;
        if(closed!=0)throw Failure("ENROLL_FAILED","cannot close token cache");
        fs::rename(temporary,token_path);
        o.token=value;
        if(o.username.empty())o.username=o.device;
        log("ENROLL succeeded; device token saved: "+token_path.string());
    }catch(...) {
        if(fd>=0)close(fd);
        std::error_code error;fs::remove(temporary,error);
        throw;
    }
}
// 原始文件直接进入压缩包；hash 在写入 tar 时计算，和包内实际字节一致。
struct File {
    biz::LogSource source;
    fs::path path;
    std::string name, hash;
    uint64_t size=0;
    int64_t mtime=0;
};
// 线程分工：主线程等待退出；MQTT 线程收发消息；worker 串行采集、压缩和上传。
// mutex 保护任务、结果和凭证；cv 用于唤醒 worker 或等待上传凭证。
class Agent {
    Options opts;
    mosquitto* mqtt=nullptr;
    mosquitto* local_mqtt=nullptr;
    std::string boot, up, down, local;
    std::mutex mutex;
    std::condition_variable cv;
    // 以业务 ID 组织来源配置；文件模式也复用此结构，但内容以来源 JSON 为准。
    std::map<std::string,biz::Registration> registrations;
    std::map<std::string,cloud_pb::CommandResult> results;
    std::map<std::string,cloud_pb::UploadGrant> grants;
    // pending 是待领取任务；active 在任务执行完才清空，期间新命令会返回 BUSY。
    std::optional<cloud_pb::Command> pending;
    std::string active;
    bool ready=false;
    std::thread worker;

    // 云端消息使用 JSON：字段为 lowerCamelCase，64 位整数为字符串，枚举为名称。
    // 本地主题走 local_mqtt，其余走云端 mqtt；发送返回不代表对端已处理完成。
    void publish(const std::string& topic, const google::protobuf::Message& msg, bool retain=false) {
        bool business_topic=topic.rfind(local,0)==0;
        auto data=business_topic?msg.SerializeAsString():cloud_json(msg);
        mosquitto_property* props=nullptr;
        mosquitto_property_add_string_pair(&props,MQTT_PROP_USER_PROPERTY,"proto","1");
        if(!business_topic) {
            mosquitto_property_add_string_pair(&props,MQTT_PROP_USER_PROPERTY,"ct","json");
            mosquitto_property_add_string(&props,MQTT_PROP_CONTENT_TYPE,"application/json");
        }
        auto* connection=business_topic?local_mqtt:mqtt;
        int rc=mosquitto_publish_v5(connection,nullptr,topic.c_str(),static_cast<int>(data.size()),data.data(),1,retain,props);
        mosquitto_property_free_all(&props);
        if(rc != MOSQ_ERR_SUCCESS) log("MQTT send pending/failed: "+std::string(mosquitto_strerror(rc)),LogLevel::Warning);
    }
    // 最多保留 100 条命令结果，按更新时间删除旧记录，不删除正在执行的任务。
    void trim_results() {
        while(results.size()>100) {
            auto oldest=results.end();
            for(auto it=results.begin();it!=results.end();++it)
                if(it->first!=active && (oldest==results.end()||it->second.time().wall_ns()<oldest->second.time().wall_ns())) oldest=it;
            if(oldest==results.end())break;
            fs::remove(opts.state/"commands"/(oldest->first+".pb")); results.erase(oldest);
        }
    }
    // 调用方已持锁；先持久化再发布，供重启后的重复请求回放。
    // 调用方必须持有 mutex：先保存结果到磁盘，再发布给云端，支持重启后的重复命令回放。
    void result(const std::string& cid,cloud_pb::CommandStatus status,int progress=0,const std::string& code="",const std::string& message="",const cloud_pb::CollectLogsResult* collected=nullptr) {
        cloud_pb::CommandResult r; r.set_command_id(cid);r.set_status(status);r.set_progress_pct(progress);
        r.set_error_code(code);r.set_message(message);r.mutable_time()->set_wall_ns(now_ns());
        if(collected)*r.mutable_collect_logs()=*collected;
        save_pb(opts.state/"commands"/(cid+".pb"),r);results[cid]=r;trim_results();
        publish(up+"cmd_result",r);
        log("COMMAND "+cid+" "+cloud_pb::CommandStatus_Name(status)+(code.empty()?"":" "+code),
            status==cloud_pb::CMD_FAILED?LogLevel::Error:
            status==cloud_pb::CMD_REJECTED?LogLevel::Warning:LogLevel::Info);
    }
    // 工作线程报告进度时先加锁，再调用 result() 更新并发布状态。
    void progress(const std::string& cid,int pct,const std::string& message) {
        std::lock_guard<std::mutex> lock(mutex);result(cid,cloud_pb::CMD_RUNNING,pct,"",message);
    }
    // 两种来源模式共用校验：ID 不重复、路径不越界、文件匹配规则数量有效。
    void validate_registration(const std::string& owner,const biz::Registration& r) const {
            if(!identifier(owner)||r.business_id()!=owner||!r.config_version()||r.log_sources_size()>32)throw Failure("INVALID","invalid business/version/source count");
            std::set<std::string> ids;
            for(auto& s:r.log_sources()) {
                if(!identifier(s.source_id())||!ids.insert(s.source_id()).second)throw Failure("INVALID","invalid or duplicate source id");
                if(!fs::path(s.path()).is_absolute())throw Failure("INVALID","source path must be absolute");
                if(s.file_patterns_size()>16)throw Failure("INVALID","too many patterns");
                if(s.filename_date_formats_size()>16)throw Failure("INVALID","too many filename date formats");
                for(const auto& format:s.filename_date_formats())(void)FilenameDateFormat(format);
            }
    }
    // 启动时读取带注释的 JSON 并转为来源 PB；构造阶段尚未启动线程，无需加锁。
    void load_sources() {
        std::ifstream input(opts.sources_config);
        if(!input)throw std::runtime_error("cannot open sources config: "+opts.sources_config.string());
        std::ostringstream contents;contents << input.rdbuf();
        if(input.bad())throw std::runtime_error("cannot read sources config: "+opts.sources_config.string());
        biz::Registration r;
        auto status=google::protobuf::util::JsonStringToMessage(strip_json_comments(contents.str()),&r);
        if(!status.ok())throw std::runtime_error("invalid sources config "+opts.sources_config.string()+": "+status.ToString());
        if(r.business_id().empty())r.set_business_id("local-config");
        if(!r.config_version())r.set_config_version(1);
        if(!r.log_sources_size())throw std::runtime_error("sources config must contain at least one log source");
        // 来源的相对目录以 JSON 所在目录为基准，而不是以启动目录为基准。
        auto base=fs::absolute(opts.sources_config).parent_path();
        for(auto& source:*r.mutable_log_sources()) {
            if(source.path().empty())throw std::runtime_error("source path is required");
            if(fs::path(source.path()).is_relative())source.set_path((base/source.path()).string());
        }
        validate_registration(r.business_id(),r);
        // 文件配置为准，不合并缓存的 MQTT 注册。
        // 本地文件是完整来源清单，不合并旧 MQTT 注册缓存。
        registrations.clear();registrations[r.business_id()]=r;
        log("CONFIG loaded sources="+std::to_string(r.log_sources_size())+" file="+opts.sources_config.string());
        for(const auto& source:r.log_sources())
            log("SOURCE "+source.source_id()+" path="+source.path());
    }
    // 兼容旧的 MQTT 注册模式：解析、校验版本、保存，再返回 register_ack。
    // 文件模式拒绝业务注册覆盖；sources-config 留空才启用本地业务 MQTT 连接。
    void registration(const std::string& owner,const void* data,int len) {
        biz::Registration r;biz::RegistrationAck ack;ack.set_business_id(owner);
        try {
            if(!opts.sources_config.empty())throw Failure("INVALID","log sources are managed by sources-config");
            if(!r.ParseFromArray(data,len))throw Failure("INVALID","invalid Protobuf");
            ack.set_config_version(r.config_version());
            validate_registration(owner,r);
            std::set<std::string> ids;for(const auto& source:r.log_sources())ids.insert(source.source_id());
            std::lock_guard<std::mutex> lock(mutex);
            auto old=registrations.find(owner);
            if(old!=registrations.end()) {
                if(r.config_version()<old->second.config_version())throw Failure("INVALID","stale config version");
                if(r.config_version()==old->second.config_version()&&r.SerializeAsString()!=old->second.SerializeAsString())throw Failure("INVALID","changed config requires newer version");
            }
            for(const auto& [key,reg]:registrations)if(key!=owner)
                for(auto& a:reg.log_sources())if(ids.count(a.source_id()))throw Failure("INVALID","source id already owned by another business");
            save_pb(opts.state/"registrations"/(owner+".pb"),r);registrations[owner]=r;
            ack.set_accepted(true);ack.set_message("registered; missing directories will be scanned when requested");
            log("REGISTER "+owner+" sources="+std::to_string(r.log_sources_size()));
        }catch(const std::exception& e){ack.set_message(e.what());}
        publish(local+owner+"/down/register_ack",ack);
    }
    // MQTT 收到命令后只做校验和入队，耗时的文件与 HTTP 操作交给 worker。
    void command(const void* data,int len) {
        cloud_pb::Command c;if(!parse_cloud_json(data,len,c)||!identifier(c.command_id())){log("Dropped malformed command",LogLevel::Warning);return;}
        std::lock_guard<std::mutex> lock(mutex);
        // 重复 command_id 回放已有结果；结果仍在缓存期间不会再次打包上传。
        auto old=results.find(c.command_id());
        if(old!=results.end()){publish(up+"cmd_result",old->second);return;}
        const auto& a=c.collect_logs();
        std::string code,message;
        if(!ready){code="NOT_READY";message="hello handshake not complete";}
        else if(!c.has_collect_logs()){code="UNSUPPORTED";message="only collect_logs supported";}
        else if(c.expires_at_ns()<=now_ns()){code="EXPIRED";message="command expired";}
        else if(a.selection_mode()!=cloud_pb::LATEST_FILE&&a.selection_mode()!=cloud_pb::TIME_RANGE&&a.selection_mode()!=cloud_pb::LOG_SELECTION_MODE_UNSPECIFIED){code="INVALID_ARGUMENT";message="unknown selection mode";}
        else if(a.selection_mode()!=cloud_pb::LATEST_FILE&&(a.time_from_ns()<=0||a.time_to_ns()<=a.time_from_ns()||a.time_to_ns()-a.time_from_ns()>31LL*86400*1000000000)){code="INVALID_ARGUMENT";message="invalid range; maximum 31 days";}
        else if(!active.empty()){code="BUSY";message="one collect_logs at a time";}
        if(!code.empty()){result(c.command_id(),cloud_pb::CMD_REJECTED,0,code,message);return;}
        // 先落盘 ACCEPTED，再唤醒工作线程；收到命令不等于任务已成功。
        active=c.command_id();result(active,cloud_pb::CMD_ACCEPTED);pending=c;cv.notify_all();
    }
    // 消息分发：业务注册、hello_reply、采集命令、上传凭证。
    // 收到 hello_reply 后上报 inventory 并标记 ready，允许接收采集任务。
    void on_message(const mosquitto_message* m) {
        if(m->payloadlen>1024*1024){log("Dropped oversized MQTT message",LogLevel::Warning);return;}
        std::string topic=m->topic;
        if(topic.rfind(local,0)==0&&topic.size()>local.size()+12&&topic.substr(topic.size()-12)=="/up/register") {
            registration(topic.substr(local.size(),topic.size()-local.size()-12),m->payload,m->payloadlen);return;
        }
        if(topic==down+"hello_reply") {
            log("HELLO_REPLY received");
            cloud_pb::HelloReply reply;if(!parse_cloud_json(m->payload,m->payloadlen,reply))return;
            cloud_pb::Inventory inv;inv.set_model(opts.model);inv.set_serial(opts.serial);inv.set_device_id(opts.device);inv.set_boot_id(boot);inv.set_os("Linux");inv.mutable_time()->set_wall_ns(now_ns());
            auto* sw=inv.add_software();sw->set_name("device-agent");sw->set_version("0.1.0");sw->set_kind("service");
            publish(up+"inventory",inv);
            {std::lock_guard<std::mutex> lock(mutex);ready=true;}
            log("READY hello handshake complete");
        } else if(topic==down+"cmd") {
            // 只记录主题和长度，不输出可能含凭证的 MQTT 消息正文。
            log("MQTT command received topic="+topic+" bytes="+std::to_string(m->payloadlen));
            command(m->payload,m->payloadlen);
        }
        else if(topic==down+"upload_grant") {
            cloud_pb::UploadGrant g;if(!parse_cloud_json(m->payload,m->payloadlen,g))return;
            // 凭证只匹配当前命令；更新 grants 后唤醒等待中的工作线程，避免不同任务串用凭证。
            std::lock_guard<std::mutex> lock(mutex);if(g.command_id()==active){grants[active]=g;cv.notify_all();}
        }
    }
    // 连接成功后先订阅下行主题；MQTT 连接成功不代表应用层握手已完成。
    void connected(mosquitto* connection,int rc) {
        const std::string peer=connection==local_mqtt?"business":"cloud";
        if(rc){log("MQTT "+peer+" connect rejected code="+std::to_string(rc)+" reason="+mosquitto_reason_string(rc),LogLevel::Error);return;}
        const auto topic=connection==local_mqtt?local+"+/up/register":down+"#";
        log("MQTT "+peer+" connected; subscribing topic="+topic);
        const int subscribed_rc=mosquitto_subscribe_v5(connection,nullptr,topic.c_str(),1,0,nullptr);
        if(subscribed_rc!=MOSQ_ERR_SUCCESS)
            log("MQTT "+peer+" subscribe failed: "+mosquitto_strerror(subscribed_rc),LogLevel::Error);
    }
    // 下行订阅确认后才发送 presence 和 hello，保证云端的握手应答能被收到。
    void subscribed(int count,const int* qos) {
        for(int i=0;i<count;++i)if(qos[i]>=128){log("MQTT subscription rejected",LogLevel::Error);return;}
        log("MQTT cloud subscription confirmed; sending hello, waiting for hello_reply");
        // 仅在云端下行订阅确认后发布 presence/hello。
        cloud_pb::Presence p;p.set_online(true);p.set_boot_id(boot);p.set_sent_wall_ns(now_ns());publish(up+"presence",p);
        cloud_pb::Hello h;h.set_model(opts.model);h.set_device_id(opts.device);h.set_boot_id(boot);h.set_protocol_version("1");h.set_sent_wall_ns(now_ns());publish(up+"hello",h);
    }
    // 把来源 ID 映射为来源配置；不指定则选全部，找不到返回 UNKNOWN_SOURCE。
    std::vector<biz::LogSource> sources(const cloud_pb::CollectLogsArgs& args) {
        std::lock_guard<std::mutex> lock(mutex);std::map<std::string,biz::LogSource> all;
        for(auto& [owner,r]:registrations)for(auto& s:r.log_sources())all[s.source_id()]=s;
        std::vector<biz::LogSource> found;
        if(args.sources().empty())for(auto& [id,s]:all)found.push_back(s);
        else for(auto& id:args.sources()) {
            if(!all.count(id))throw Failure("UNKNOWN_SOURCE","source not registered: "+id);
            if(std::none_of(found.begin(),found.end(),[&](auto& s){return s.source_id()==id;}))found.push_back(all[id]);
        }
        return found;
    }
    // 扫描候选文件：检查路径边界，跳过符号链接、非普通文件和空文件，匹配文件名。
    // 最新文件模式每个来源选修改时间最新的一份；日期模式按文件名日期选择整份文件。
    std::vector<File> discover(const cloud_pb::CollectLogsArgs& args) {
        std::vector<File> files;
        for(auto& source:sources(args)) {
            fs::path root=fs::weakly_canonical(source.path());
            std::vector<File> local_files;
            std::vector<FilenameDateFormat> formats;
            for(const auto& format:source.filename_date_formats())formats.emplace_back(format);
            if(formats.empty())formats.emplace_back("%Y-%m-%d");
            auto add=[&](const fs::directory_entry& entry) {
                if(entry.is_symlink()||!entry.is_regular_file())return;
                auto path=fs::canonical(entry.path());if(!inside(path,root))return;
                bool matches=source.file_patterns().empty();
                for(auto& pattern:source.file_patterns())if(fnmatch(pattern.c_str(),path.filename().c_str(),0)==0)matches=true;
                if(!matches)return;
                struct stat st{};if(stat(path.c_str(),&st)||st.st_size==0)return;
                File f;f.source=source;f.path=path;f.size=st.st_size;f.mtime=static_cast<int64_t>(st.st_mtim.tv_sec)*1000000000+st.st_mtim.tv_nsec;
                // 按天只比较文件名日期与请求区间；最新文件模式无需文件名带日期。
                if(args.selection_mode()!=cloud_pb::LATEST_FILE) {
                    int64_t day=filename_day(path,formats);
                    if(day<0||day>=args.time_to_ns()||day+86400LL*1000000000<=args.time_from_ns())return;
                }
                f.name=source.source_id()+"/"+(fs::is_directory(root)?fs::relative(path,root).generic_string():path.filename().string());
                local_files.push_back(f);
            };
            if(fs::exists(root)) {
                if(fs::is_regular_file(root))add(fs::directory_entry(root));
                else if(source.recursive())for(auto& entry:fs::recursive_directory_iterator(root))add(entry);
                else for(auto& entry:fs::directory_iterator(root))add(entry);
            }
            // 修改时间仅用于选择最新文件；相同修改时间时按路径排序，保证选择稳定。
            std::sort(local_files.begin(),local_files.end(),[](auto& a,auto& b){return a.mtime==b.mtime?a.path.string()<b.path.string():a.mtime>b.mtime;});
            if(args.selection_mode()==cloud_pb::LATEST_FILE&&local_files.size()>1)local_files.resize(1);
            for(const auto& file:local_files)log("SELECT source="+source.source_id()+" file="+file.path.string());
            files.insert(files.end(),local_files.begin(),local_files.end());
            if(files.size()>10000)throw Failure("TOO_MANY_FILES","maximum 10000 files per request");
        }
        // 所有候选按修改时间从旧到新排序，便于包超限时优先去掉较旧整份文件。
        std::sort(files.begin(),files.end(),[](auto& a,auto& b){return a.mtime<b.mtime;});return files;
    }
    // 原文件直接分块写入 tar.gz，不创建日志副本，不读取或解释日志行。
    // 读取范围固定为扫描时的大小，防止业务持续追加导致任务无法结束。
    fs::path bundle(const cloud_pb::Command& cmd,const fs::path& dir,std::vector<File>& files,bool& truncated) {
        fs::path target=dir/"logs.tar.gz";
        uint64_t limit=cmd.collect_logs().max_bundle_bytes()?cmd.collect_logs().max_bundle_bytes():500ULL*1024*1024;
        for(;;) {
            if(files.empty())throw Failure("BUNDLE_TOO_SMALL","no whole file fits bundle size limit");
            archive* a=archive_write_new();archive_write_add_filter_gzip(a);archive_write_set_format_pax_restricted(a);
            if(archive_write_open_filename(a,target.c_str())!=ARCHIVE_OK){std::string msg=archive_error_string(a);archive_write_free(a);throw Failure("DISK_FULL",msg);}
            try {
                // 摘要与 tar 使用同一批字节，避免提前计算摘要后业务又改写文件导致摘要不一致。
                auto append=[&](const std::string& name,const fs::path& path,uint64_t size) {
                    std::ifstream in(path,std::ios::binary);
                    if(!in)throw Failure("READ_FAILED","cannot open "+path.string());
                    std::unique_ptr<EVP_MD_CTX,decltype(&EVP_MD_CTX_free)> digest(EVP_MD_CTX_new(),EVP_MD_CTX_free);
                    if(!digest||EVP_DigestInit_ex(digest.get(),EVP_sha256(),nullptr)!=1)
                        throw std::runtime_error("digest initialization failed");
                    archive_entry* entry=archive_entry_new();archive_entry_set_pathname(entry,name.c_str());
                    archive_entry_set_size(entry,size);archive_entry_set_filetype(entry,AE_IFREG);archive_entry_set_perm(entry,0600);
                    int rc=archive_write_header(a,entry);archive_entry_free(entry);
                    if(rc!=ARCHIVE_OK)throw Failure("PACK_FAILED",archive_error_string(a));
                    char data[65536];
                    while(size) {
                        if(stopping)throw Failure("INTERRUPTED","Agent stopping");
                        auto count=static_cast<std::streamsize>(std::min<uint64_t>(size,sizeof data));
                        in.read(data,count);
                        if(in.gcount()!=count)throw Failure("READ_FAILED","file shrank or read failed: "+path.string());
                        if(archive_write_data(a,data,count)!=count)throw Failure("PACK_FAILED","archive write failed");
                        if(EVP_DigestUpdate(digest.get(),data,static_cast<size_t>(count))!=1)
                            throw std::runtime_error("digest update failed");
                        size-=static_cast<uint64_t>(count);
                    }
                    unsigned char bytes[EVP_MAX_MD_SIZE];unsigned int length=0;
                    if(EVP_DigestFinal_ex(digest.get(),bytes,&length)!=1)throw std::runtime_error("digest finalization failed");
                    std::ostringstream hex;
                    for(unsigned int i=0;i<length;++i)hex<<std::hex<<std::setw(2)<<std::setfill('0')<<static_cast<int>(bytes[i]);
                    return hex.str();
                };
                for(auto& f:files)f.hash=append(f.name,f.path,f.size);

                // 文件写完后才生成清单，确保清单摘要对应刚刚写入压缩包的原始内容。
                google::protobuf::Struct manifest;auto& fields=*manifest.mutable_fields();
                fields["device_id"].set_string_value(opts.device);fields["command_id"].set_string_value(cmd.command_id());
                fields["selection_mode"].set_string_value(cmd.collect_logs().selection_mode()==cloud_pb::LATEST_FILE?"latest_file":"filename_date");
                fields["requested_from_ns"].set_string_value(std::to_string(cmd.collect_logs().time_from_ns()));
                fields["requested_to_ns"].set_string_value(std::to_string(cmd.collect_logs().time_to_ns()));
                fields["truncated"].set_bool_value(truncated);fields["agent_version"].set_string_value("0.1.0");
                auto* list=fields["files"].mutable_list_value();
                for(const auto& f:files) {
                    auto& meta=*list->add_values()->mutable_struct_value()->mutable_fields();
                    meta["source_id"].set_string_value(f.source.source_id());meta["name"].set_string_value(f.name);
                    meta["sha256"].set_string_value(f.hash);meta["size_bytes"].set_string_value(std::to_string(f.size));
                    meta["mtime_ns"].set_string_value(std::to_string(f.mtime));
                }
                std::string json;google::protobuf::util::MessageToJsonString(manifest,&json);
                {std::ofstream out(dir/"manifest.json");out<<json;if(!out)throw Failure("DISK_FULL","cannot write manifest");}
                append("manifest.json",dir/"manifest.json",fs::file_size(dir/"manifest.json"));
                if(archive_write_close(a)!=ARCHIVE_OK)throw Failure("PACK_FAILED","archive close failed");
                archive_write_free(a);
            }catch(...){archive_write_free(a);throw;}
            if(fs::file_size(target)<=limit){log("BUNDLE files="+std::to_string(files.size())+" bytes="+std::to_string(fs::file_size(target))+" sha256="+sha256(target));return target;}
            // 超过压缩包上限时移除最旧整份文件后重新打包，不截断文件内容。
            files.erase(files.begin());truncated=true;
        }
    }    // curl 响应体回调：丢弃正文，上传结果通过网络返回码和 HTTP 状态判断。
    static size_t discard(char*,size_t size,size_t n,void*){return size*n;}
    // 检查云端凭证的大小、过期时间，依次尝试上传地址。
    // 默认要求 HTTPS；只有显式允许本机演示时，才接受指定回环地址的 HTTP。
    void upload(const fs::path& path,const cloud_pb::UploadGrant& grant) {
        if(fs::file_size(path)>grant.max_size_bytes())throw Failure("UPLOAD_DENIED","grant size too small");
        std::string last="no upload hosts";
        for(auto& host:grant.up_hosts()) {
            if(grant.expires_at_ns()<=now_ns())throw Failure("UPLOAD_DENIED","grant expired");
            bool secure=host.rfind("https://",0)==0;
            bool loopback=host.rfind("http://127.0.0.1:",0)==0||host.rfind("http://localhost:",0)==0;
            if(!secure&&!(opts.insecure_upload&&loopback)){last="HTTPS required (HTTP loopback allowed only in simulation)";continue;}
            log("UPLOAD destination="+host+" bytes="+std::to_string(fs::file_size(path)));
            CURL* curl=curl_easy_init();curl_mime* mime=curl_mime_init(curl);
            auto field=[&](const char* name,const std::string& value){auto* p=curl_mime_addpart(mime);curl_mime_name(p,name);curl_mime_data(p,value.c_str(),CURL_ZERO_TERMINATED);};
            // 上传使用 multipart/form-data：token、key、file；令牌只参与请求，不打印日志。
            field("token",grant.uptoken());field("key",grant.storage_key());
            auto* p=curl_mime_addpart(mime);curl_mime_name(p,"file");curl_mime_filedata(p,path.c_str());
            curl_easy_setopt(curl,CURLOPT_URL,host.c_str());curl_easy_setopt(curl,CURLOPT_MIMEPOST,mime);
            if(!opts.https_ca.empty())curl_easy_setopt(curl,CURLOPT_CAINFO,opts.https_ca.c_str());
            curl_easy_setopt(curl,CURLOPT_CONNECTTIMEOUT,5L);curl_easy_setopt(curl,CURLOPT_TIMEOUT,30L);
            curl_easy_setopt(curl,CURLOPT_NOSIGNAL,1L);curl_easy_setopt(curl,CURLOPT_WRITEFUNCTION,discard);
            curl_easy_setopt(curl,CURLOPT_PROTOCOLS,CURLPROTO_HTTP|CURLPROTO_HTTPS);
            auto rc=curl_easy_perform(curl);long status=0;curl_easy_getinfo(curl,CURLINFO_RESPONSE_CODE,&status);
            last=std::string(curl_easy_strerror(rc))+" HTTP "+std::to_string(status);
            curl_mime_free(mime);curl_easy_cleanup(curl);
            if(rc==CURLE_OK&&status>=200&&status<300)return;
        }
        throw Failure("UPLOAD_FAILED",last);
    }
    // 命令主流程：采集(10%) → 打包(40%) → 申请凭证 → 上传(70%) → 成功(100%)。
    // Failure 按业务错误码报告，其他异常统一报告 INTERNAL。
    void execute(const cloud_pb::Command& cmd) {
        auto dir=opts.work/cmd.command_id();fs::create_directories(dir);
        try {
            progress(cmd.command_id(),10,"collecting");
            // 没有匹配文件时返回 NO_LOGS_IN_RANGE，不生成空日志包。
            auto files=discover(cmd.collect_logs());if(files.empty())throw Failure("NO_LOGS_IN_RANGE","no matching log files");
            progress(cmd.command_id(),40,"packing");bool truncated=false;auto path=bundle(cmd,dir,files,truncated);
            auto hash=sha256(path);cloud_pb::UploadRequest req;req.set_command_id(cmd.command_id());req.set_filename(path.filename().string());req.set_size_bytes(fs::file_size(path));req.set_sha256(hash);req.set_content_type("application/gzip");
            cloud_pb::UploadGrant grant;bool received=false;
            // 先报告包大小和摘要，再向云端申请地址及令牌；最多申请 3 次，每次等 5 秒。
            // cv.wait_for 等待时释放 mutex，MQTT 线程才能写入凭证并唤醒这里。
            for(int attempt=0;attempt<3&&!stopping;++attempt) {
                publish(up+"upload_request",req);
                std::unique_lock<std::mutex> lock(mutex);
                if(cv.wait_for(lock,std::chrono::seconds(5),[&]{return stopping||grants.count(cmd.command_id());})&&grants.count(cmd.command_id())){grant=grants.at(cmd.command_id());received=true;break;}
            }
            if(!received)throw Failure("UPLOAD_DENIED","upload grant timeout");
            if(!grant.granted())throw Failure("UPLOAD_DENIED",grant.reject_reason());
            progress(cmd.command_id(),70,"uploading");upload(path,grant);
            // 成功时回传包 ID、摘要、来源和请求日期区间；云端另行校验上传对象的大小和摘要。
            cloud_pb::CollectLogsResult collected;collected.set_file_id(grant.file_id());collected.set_size_bytes(req.size_bytes());collected.set_sha256(hash);collected.set_truncated(truncated);
            std::set<std::string> included;
            for(const auto& f:files)included.insert(f.source.source_id());
            // 不解析内容时间；日期模式回传请求区间，最新模式不提供内容时间范围。
            if(cmd.collect_logs().selection_mode()!=cloud_pb::LATEST_FILE) {
                collected.set_time_from_ns(cmd.collect_logs().time_from_ns());
                collected.set_time_to_ns(cmd.collect_logs().time_to_ns());
            }
            for(const auto& s:included)collected.add_included_sources(s);
            std::lock_guard<std::mutex> lock(mutex);result(cmd.command_id(),cloud_pb::CMD_SUCCEEDED,100,"","uploaded; cloud verifies object",&collected);
        }catch(const Failure& e){std::lock_guard<std::mutex> lock(mutex);result(cmd.command_id(),cloud_pb::CMD_FAILED,0,e.code,e.what());}
        catch(const std::exception& e){std::lock_guard<std::mutex> lock(mutex);result(cmd.command_id(),cloud_pb::CMD_FAILED,0,"INTERNAL",e.what());}
        // 任务结束后仅清理本命令工作目录，保留业务原文件和云端已收到的日志包。
        std::error_code ec;fs::remove_all(dir,ec); // 只清理已验证命令 ID 的工作目录
    }
    // 串行领取并执行任务；执行文件和 HTTP 操作时不长期持有 mutex。
    // 领取后 pending 清空，执行结束才释放 active，随后允许下一条任务。
    void work_loop() {
        while(!stopping) {
            cloud_pb::Command cmd;
            {std::unique_lock<std::mutex> lock(mutex);cv.wait_for(lock,std::chrono::milliseconds(200),[&]{return pending.has_value()||stopping;});if(!pending)continue;cmd=*pending;pending.reset();}
            try{execute(cmd);}catch(const std::exception& e){log(std::string("TASK ERROR ")+e.what(),LogLevel::Error);}
            {std::lock_guard<std::mutex> lock(mutex);grants.erase(cmd.command_id());active.clear();}
        }
    }
public:
    // 构造阶段：规范化目录、加载来源、恢复命令状态、初始化 MQTT 客户端及回调。
    // 这里只准备资源，网络和工作线程由 run() 启动。
    explicit Agent(Options o):opts(std::move(o)) {
        opts.state=fs::absolute(opts.state);opts.work=fs::absolute(opts.work);
        fs::create_directories(opts.state/"registrations");fs::create_directories(opts.state/"commands");fs::create_directories(opts.work);
        // 文件模式直接加载 JSON；仅在 MQTT 模式下恢复磁盘中的业务注册 PB。
        if(!opts.sources_config.empty())load_sources();
        else for(auto& entry:fs::directory_iterator(opts.state/"registrations"))if(entry.path().extension()==".pb"){
            biz::Registration r;std::ifstream in(entry.path(),std::ios::binary);if(r.ParseFromIstream(&in)&&identifier(r.business_id()))registrations[r.business_id()]=r;
        }
        // 恢复历史结果；上次进程中断的 ACCEPTED/RUNNING 标记为失败，不自动重跑旧任务。
        for(auto& entry:fs::directory_iterator(opts.state/"commands"))if(entry.path().extension()==".pb"){
            cloud_pb::CommandResult r;std::ifstream in(entry.path(),std::ios::binary);if(!r.ParseFromIstream(&in)||!identifier(r.command_id()))continue;
            if(r.status()==cloud_pb::CMD_ACCEPTED||r.status()==cloud_pb::CMD_RUNNING){r.set_status(cloud_pb::CMD_FAILED);r.set_error_code("AGENT_RESTARTED");r.set_message("task interrupted; issue a new command");save_pb(entry.path(),r);}
            results[r.command_id()]=r;
        }
        trim_results();
        // 读取 Linux 内核启动 ID，用于在线状态和握手；失败时用当前时间作为后备。
        std::ifstream bootfile("/proc/sys/kernel/random/boot_id");std::getline(bootfile,boot);if(boot.empty())boot=std::to_string(now_ns());
        // 根据设备 ID 构造云端上行、下行及兼容模式的业务主题前缀。
        up="v1/d/"+opts.device+"/up/";down="v1/d/"+opts.device+"/down/";local="local/v1/"+opts.device+"/biz/";
        mqtt=mosquitto_new(opts.device.c_str(),true,this);
        local_mqtt=mosquitto_new(("agent-local-"+opts.device).c_str(),true,this);
        if(!mqtt||!local_mqtt)throw std::runtime_error("MQTT init failed");
        mosquitto_int_option(mqtt,MOSQ_OPT_PROTOCOL_VERSION,MQTT_PROTOCOL_V5);
        mosquitto_int_option(local_mqtt,MOSQ_OPT_PROTOCOL_VERSION,MQTT_PROTOCOL_V5);
        if(!opts.username.empty())mosquitto_username_pw_set(mqtt,opts.username.c_str(),opts.token.c_str());
        if(!opts.local_username.empty())mosquitto_username_pw_set(local_mqtt,opts.local_username.c_str(),opts.local_token.c_str());
        if(!opts.ca.empty()){int rc=mosquitto_tls_set(mqtt,opts.ca.c_str(),nullptr,nullptr,nullptr,nullptr);if(rc)throw std::runtime_error(mosquitto_strerror(rc));}
        if(!opts.local_ca.empty()){int rc=mosquitto_tls_set(local_mqtt,opts.local_ca.c_str(),nullptr,nullptr,nullptr,nullptr);if(rc)throw std::runtime_error(mosquitto_strerror(rc));}
        mosquitto_reconnect_delay_set(mqtt,1,60,true);
        mosquitto_reconnect_delay_set(local_mqtt,1,60,true);
        // MQTT 遗嘱：连接异常中断时，由 Broker 代为发送离线消息。
        cloud_pb::Presence will;will.set_online(false);will.set_boot_id(boot);auto data=cloud_json(will);
        mosquitto_property* props=nullptr;mosquitto_property_add_string_pair(&props,MQTT_PROP_USER_PROPERTY,"proto","1");
        mosquitto_property_add_string_pair(&props,MQTT_PROP_USER_PROPERTY,"ct","json");
        mosquitto_property_add_string(&props,MQTT_PROP_CONTENT_TYPE,"application/json");
        mosquitto_will_set_v5(mqtt,(up+"presence").c_str(),data.size(),data.data(),1,false,props);
        // 库回调通过 userdata=this 找回 Agent 实例；消息处理异常在回调边界记录。
        for(auto* connection:{mqtt,local_mqtt}) {
            mosquitto_connect_v5_callback_set(connection,[](auto* c,void* u,int rc,int,const auto*){static_cast<Agent*>(u)->connected(c,rc);});
            mosquitto_message_v5_callback_set(connection,[](auto*,void* u,const auto* m,const auto*){try{static_cast<Agent*>(u)->on_message(m);}catch(const std::exception& e){log(std::string("MESSAGE ERROR ")+e.what(),LogLevel::Warning);}});
        }
        mosquitto_subscribe_v5_callback_set(mqtt,[](auto*,void* u,int,int n,const int* qos,const auto*){static_cast<Agent*>(u)->subscribed(n,qos);});
        mosquitto_disconnect_v5_callback_set(mqtt,[](auto*,void* u,int rc,const auto*){
            auto* a=static_cast<Agent*>(u);
            {std::lock_guard<std::mutex> lock(a->mutex);a->ready=false;}
            const char* reason=rc>=128?mosquitto_reason_string(rc):mosquitto_strerror(rc);
            log("MQTT cloud disconnected code="+std::to_string(rc)+" reason="+reason+
                (stopping?"; stopping":"; reconnecting"),stopping?LogLevel::Info:LogLevel::Warning);
        });
    }
    // 前台运行：启动网络和工作线程，主线程等待退出信号。文件模式只连接云端 MQTT。
    void run() {
        log("MQTT cloud connecting host="+opts.host+" port="+std::to_string(opts.port)+" device="+opts.device);
        int rc=mosquitto_connect_async(mqtt,opts.host.c_str(),opts.port,30);if(rc)throw std::runtime_error(mosquitto_strerror(rc));
        if(opts.sources_config.empty()) {
            rc=mosquitto_connect_async(local_mqtt,opts.local_host.c_str(),opts.local_port?opts.local_port:opts.port,30);if(rc)throw std::runtime_error(mosquitto_strerror(rc));
            mosquitto_loop_start(local_mqtt);
        }
        mosquitto_loop_start(mqtt);worker=std::thread(&Agent::work_loop,this);
        while(!stopping)std::this_thread::sleep_for(std::chrono::milliseconds(100));
        // 退出时先唤醒并回收 worker，再停止网络；进行中的 HTTP 可能等到超时才返回。
        cv.notify_all();worker.join();mosquitto_disconnect(mqtt);mosquitto_loop_stop(mqtt,true);
        if(opts.sources_config.empty()){mosquitto_disconnect(local_mqtt);mosquitto_loop_stop(local_mqtt,true);}
    }
    ~Agent(){if(mqtt)mosquitto_destroy(mqtt);if(local_mqtt)mosquitto_destroy(local_mqtt);}
};
// 只从配置文件加载参数，不接收命令行参数。
// key=value 的相对路径以配置文件所在目录为基准；错误包含文件名和行号。
// 入口文件用 environment=local/cloud 选择环境；保留 config-file 供自定义测试配置使用。
static Options load_options(const fs::path& config,int depth=0,std::string environment="local") {
    if(depth>4)throw std::runtime_error("config-file nesting too deep");
    std::ifstream input(config);
    if(!input)throw std::runtime_error("cannot open config: "+config.string());
    auto base=fs::absolute(config).parent_path();
    Options o;
    fs::path selected;
    bool has_environment=false;
    bool has_options=false;
    auto trim=[](const std::string& s) {
        auto first=s.find_first_not_of(" \t\r\n");
        return first==std::string::npos?std::string():s.substr(first,s.find_last_not_of(" \t\r\n")-first+1);
    };
    auto path_value=[&](const std::string& value) -> fs::path {
        fs::path path=value;
        return !path.empty()&&path.is_relative()?base/path:path;
    };
    std::string line;
    size_t number=0;
    while(std::getline(input,line)) {
        ++number;line=trim(line);
        if(line.empty()||line[0]=='#')continue;
        try {
            auto split=line.find('=');
            if(split==std::string::npos)throw std::runtime_error("expected key=value");
            auto key=trim(line.substr(0,split)),value=trim(line.substr(split+1));
            if(key=="environment") {
                if(has_environment)throw std::runtime_error("duplicate environment");
                if(value!="local"&&value!="cloud")throw std::runtime_error("environment must be local or cloud");
                environment=value;has_environment=true;continue;
            }
            if(key=="config-file") {
                if(value.empty())throw std::runtime_error("config-file must not be empty");
                if(!selected.empty())throw std::runtime_error("duplicate config-file");
                selected=path_value(value);continue;
            }
            has_options=true;
            auto port=[&](bool allow_zero) {
                size_t used=0;
                int parsed=std::stoi(value,&used);
                if(used!=value.size()||parsed<(allow_zero?0:1)||parsed>65535)
                    throw std::runtime_error("port must be "+std::string(allow_zero?"0":"1")+"..65535");
                return parsed;
            };
            if(key=="host")o.host=value;
            else if(key=="port")o.port=port(false);
            else if(key=="device")o.device=value;
            else if(key=="allowed-root")log("CONFIG allowed-root is obsolete and ignored",LogLevel::Warning);
            else if(key=="log-file")o.log_path=path_value(value);
            else if(key=="state-dir")o.state=path_value(value);
            else if(key=="work-dir")o.work=path_value(value);
            else if(key=="sources-config")o.sources_config=path_value(value);
            else if(key=="username")o.username=value;
            else if(key=="token")o.token=value;
            else if(key=="token-file")o.token_file=path_value(value);
            else if(key=="enroll-url")o.enroll_url=value;
            else if(key=="enrollment-key")o.enrollment_key=value;
            else if(key=="model")o.model=value;
            else if(key=="serial")o.serial=value;
            else if(key=="allow-insecure-loopback-enroll") {
                if(value!="true"&&value!="false")throw std::runtime_error("expected true or false");
                o.insecure_enroll=value=="true";
            }
            else if(key=="ca")o.ca=path_value(value).string();
            else if(key=="https-ca")o.https_ca=path_value(value).string();
            else if(key=="local-host")o.local_host=value;
            else if(key=="local-port")o.local_port=port(true);
            else if(key=="local-username")o.local_username=value;
            else if(key=="local-token")o.local_token=value;
            else if(key=="local-ca")o.local_ca=path_value(value).string();
            else if(key=="allow-insecure-loopback-upload") {
                if(value!="true"&&value!="false")throw std::runtime_error("expected true or false");
                o.insecure_upload=value=="true";
            } else throw std::runtime_error("unknown config key: "+key);
        }catch(const std::exception& e) {
            throw std::runtime_error(config.string()+":"+std::to_string(number)+": "+e.what());
        }
    }
    if(input.bad())throw std::runtime_error("cannot read config: "+config.string());
    if(!selected.empty()) {
        if(has_options)throw std::runtime_error("config-file cannot be mixed with connection settings");
        return load_options(selected,depth+1,environment);
    }
    if(has_environment) {
        if(has_options)throw std::runtime_error("environment selector cannot be mixed with connection settings");
        return load_options(base/("device-agent."+environment+".conf"),depth+1,environment);
    }
    log("CONFIG profile="+fs::absolute(config).string()+" environment="+environment+" cloud-wire=json");
    if(!identifier(o.device))throw std::runtime_error("invalid device in "+config.string());
    if(o.state.empty()||o.work.empty())throw std::runtime_error("state-dir and work-dir must not be empty");
    if(o.host.empty()||!o.port)throw std::runtime_error("host and port are required");
    if(!o.enroll_url.empty()&&o.model.empty())throw std::runtime_error("model is required for enrollment");
    if(o.local_host.empty())o.local_host=o.host;
    return o;
}

// 程序入口：信号处理 → 运行日志 → 固定配置文件 → 网络库初始化 → Agent 生命周期。
int main() {
    std::signal(SIGINT,[](int){stopping=true;});std::signal(SIGTERM,[](int){stopping=true;});
    try {
#ifdef DEVICE_AGENT_ANDROID_DATA_DIR
        // 保持无命令行参数；Android 从固定目录读取配置并保存运行状态。
        fs::current_path(DEVICE_AGENT_ANDROID_DATA_DIR);
#endif
        Options o=load_options("device-agent.conf");
        if(!o.log_path.empty()) {
            if(!o.log_path.parent_path().empty())fs::create_directories(o.log_path.parent_path());
            log_file.open(o.log_path,std::ios::app);
            if(!log_file)throw std::runtime_error("cannot open log file: "+o.log_path.string());
        }
        log("START device-agent; log: "+(o.log_path.empty()?std::string("disabled"):fs::absolute(o.log_path).string()));
        if(!o.https_ca.empty()&&!fs::is_regular_file(o.https_ca))
            throw std::runtime_error("HTTPS CA bundle is missing: "+o.https_ca);
        // 创建 Agent 前初始化网络库，Agent 析构后再清理。
        if(mosquitto_lib_init()!=MOSQ_ERR_SUCCESS)throw std::runtime_error("MQTT library initialization failed");
        if(curl_global_init(CURL_GLOBAL_DEFAULT)!=CURLE_OK){mosquitto_lib_cleanup();throw std::runtime_error("curl initialization failed");}
        // 注册或连接失败也要清理全局网络资源；Token 必须先保存，再建立 MQTT 连接。
        struct NetworkCleanup { ~NetworkCleanup(){curl_global_cleanup();mosquitto_lib_cleanup();} } cleanup;
        resolve_token(o);
        {Agent agent(o);agent.run();}
        log("STOP device-agent");return 0;
    }catch(const std::exception& e){log(std::string("FATAL ")+e.what(),LogLevel::Error);return 1;}
}
