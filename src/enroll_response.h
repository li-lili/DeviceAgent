#pragma once

#include <google/protobuf/struct.pb.h>
#include <google/protobuf/util/json_util.h>
#include <algorithm>
#include <cctype>
#include <regex>
#include <string>
#include <vector>

// Only selected, sanitized metadata may cross the log boundary. Never log the
// raw response/parser error: either can contain a Token or echoed signature.
namespace enrollment {
inline bool valid_token(const std::string& value) {
    return !value.empty()&&value.size()<=65536
        &&value.find_first_of(" \t\r\n")==std::string::npos&&value.find('\0')==std::string::npos;
}

inline std::string normalized(std::string value) {
    std::string result;
    for(unsigned char c:value)if(std::isalnum(c))result+=static_cast<char>(std::tolower(c));
    return result;
}
inline bool sensitive_name(const std::string& key) {
    auto name=normalized(key);
    for(const char* part:{"token","signature","privatekey","password","passwd","secret",
                         "authorization","credential","apikey","cookie"})
        if(name.find(part)!=std::string::npos)return true;
    return false;
}

// Bound traversal of untrusted JSON; if exceeded, omit dynamic metadata rather
// than risk logging an uncollected credential. The body already has a 1 MiB cap.
inline bool collect_secrets(const google::protobuf::Value& value,bool sensitive,
                            std::vector<std::string>& secrets,size_t& budget,int depth=0) {
    if(!budget--||depth>24)return false;
    if(value.kind_case()==google::protobuf::Value::kStringValue&&sensitive) {
        if(value.string_value().size()>65536)return false;
        if(!value.string_value().empty())secrets.push_back(value.string_value());
    } else if(value.kind_case()==google::protobuf::Value::kStructValue) {
        for(const auto& item:value.struct_value().fields())
            if(!collect_secrets(item.second,sensitive||sensitive_name(item.first),secrets,budget,depth+1))return false;
    } else if(value.kind_case()==google::protobuf::Value::kListValue) {
        for(const auto& item:value.list_value().values())
            if(!collect_secrets(item,sensitive,secrets,budget,depth+1))return false;
    }
    return true;
}

inline std::string redact(std::string text,const std::vector<std::string>& secrets) {
    if(text.size()>2048)return "[omitted: metadata too long]";
    for(const auto& secret:secrets) {
        size_t at=0;
        while(!secret.empty()&&(at=text.find(secret,at))!=std::string::npos) {
            text.replace(at,secret.size(),"[redacted]");at+=10;
        }
    }
    // Suppress a credential-bearing suffix, including PEM, headers and echoed
    // request JSON. No partial/truncated secret or forged log line is retained.
    static const std::regex credential_context(
        R"((-----BEGIN|\b(?:bearer|basic|authorization|password|passwd|signature|private[ _-]?key|secret|credentials?|api[ _-]?key|(?:access[ _-]?|refresh[ _-]?)?token|cookie)\b)[\s\S]*)",
        std::regex::icase);
    text=std::regex_replace(text,credential_context,"[redacted credential context]");
    static const std::regex prefixed(R"(\b(?:lsd_|sk-|sk_|ghp_|AKIA)[A-Za-z0-9_+/=.-]*)");
    text=std::regex_replace(text,prefixed,"[redacted]");
    static const std::regex jwt(R"(\b[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b)");
    text=std::regex_replace(text,jwt,"[redacted dotted value]");
    static const std::regex url(R"([A-Za-z][A-Za-z0-9+.-]*://[^\s]+)");
    text=std::regex_replace(text,url,"[redacted URL]");
    static const std::regex opaque(R"([A-Za-z0-9_+/=-]{24,})");
    text=std::regex_replace(text,opaque,"[redacted opaque value]");
    for(char& c:text)if(static_cast<unsigned char>(c)<32||static_cast<unsigned char>(c)==127)c=' ';
    return text;
}

inline std::string hint(long status) {
    switch(status) {
    case 200:return "认证响应成功，仍需校验并保存新 Token";
    case 201:return "认证响应成功，仍需校验并保存新 Token";
    case 400:return "请求参数或签名格式非法，请核对协议";
    case 401:return "身份未授权或签名校验失败，请由云端核对绑定与验签记录";
    case 403:return "请求被拒绝，请核对出口白名单及访问策略";
    case 409:return "身份或注册状态冲突，请由云端排查；不要自行重新绑定或覆盖公钥";
    case 429:return "请求被限流；本次停止，不自动重试";
    case 502:return "网关或上游服务失败；不能据此判断密钥不匹配";
    case 503:return "服务暂不可用；本次停止，不自动重试";
    default:return "未预期的 HTTP 状态；本次停止，不自动重试";
    }
}

struct Response {
    google::protobuf::Struct data;
    bool parsed=false, success=false;
    std::string diagnostic;
};

inline Response inspect(long status,const std::string& body,const std::string& request_signature={},
                        bool transport_complete=true) {
    Response result;
    auto first=body.find_first_not_of(" \t\r\n");
    result.parsed=first!=std::string::npos&&body[first]=='{'
        &&google::protobuf::util::JsonStringToMessage(body,&result.data).ok();
    const auto& fields=result.data.fields();
    const google::protobuf::Value* code=nullptr;
    const google::protobuf::Value* nested_message=nullptr;
    auto error=fields.find("error");
    bool has_error=result.parsed&&error!=fields.end();
    if(has_error) {
        if(error->second.kind_case()==google::protobuf::Value::kStringValue)code=&error->second;
        else if(error->second.kind_case()==google::protobuf::Value::kStructValue) {
            const auto& info=error->second.struct_value().fields();
            auto item=info.find("code");if(item!=info.end())code=&item->second;
            item=info.find("message");if(item!=info.end())nested_message=&item->second;
        }
    }
    google::protobuf::Value all;*all.mutable_struct_value()=result.data;
    std::vector<std::string> secrets;
    if(!request_signature.empty())secrets.push_back(request_signature);
    size_t budget=512;
    bool metadata_safe=result.parsed&&collect_secrets(all,false,secrets,budget);
    // Long values first so short overlapping values cannot leave secret suffixes.
    std::sort(secrets.begin(),secrets.end(),[](const auto& a,const auto& b){return a.size()>b.size();});
    google::protobuf::Struct summary;
    auto& out=*summary.mutable_fields();
    out["http_status"].set_number_value(status);
    out["response_complete"].set_bool_value(transport_complete);
    out["json_state"].set_string_value(result.parsed?"object":"invalid_or_non_object");
    out["error_state"].set_string_value(!has_error?"missing":
        error->second.kind_case()==google::protobuf::Value::kStringValue?"flat_string":
        error->second.kind_case()==google::protobuf::Value::kStructValue?"object":"invalid_type");
    auto field=[&](const char* name,const google::protobuf::Value* value,bool is_code=false) {
        std::string state="missing",text;
        if(value) {
            if(value->kind_case()!=google::protobuf::Value::kStringValue)state="invalid_type";
            else if(!metadata_safe){state="omitted_unsafe_structure";}
            else {
                const auto& raw=value->string_value();
                text=redact(raw,secrets);
                state=text==raw?"present":"redacted";
                if(is_code) {
                    static const std::regex format("[a-z][a-z0-9_]{0,63}");
                    bool credential=false;
                    for(const auto& secret:secrets)if(!secret.empty()&&raw.find(secret)!=std::string::npos)credential=true;
                    if(raw.rfind("lsd_",0)==0||raw.rfind("sk_",0)==0
                        ||(raw.size()>=32&&raw.find('_')==std::string::npos))credential=true;
                    if(credential||!std::regex_match(raw,format)){text="[redacted or invalid code]";state="redacted_or_invalid";}
                    else {text=raw;state="present";}
                }
            }
        }
        out[std::string(name)+"_state"].set_string_value(state);
        if(!text.empty())out[name].set_string_value(text);
    };
    field("error_code",code,true);
    auto message=fields.find("message");
    field("message",result.parsed&&message!=fields.end()?&message->second:nullptr);
    field("error_message",nested_message);
    auto token=fields.find("token");
    bool token_ok=result.parsed&&token!=fields.end()
        &&token->second.kind_case()==google::protobuf::Value::kStringValue&&valid_token(token->second.string_value());
    result.success=transport_complete&&(status==200||status==201)&&token_ok&&!has_error;
    std::string outcome,explanation=hint(status);
    if(!transport_complete) {
        outcome="transport_error";explanation="网络、TLS 或超时失败；认证结果待核实，本次停止";
    } else if(status!=200&&status!=201)outcome="http_error";
    else if(!result.parsed){outcome="invalid_json";explanation="响应不是有效 JSON 对象；不接受为认证成功";}
    else if(has_error){outcome="conflicting_response";explanation="成功 HTTP 状态同时携带 error；不接受为认证成功";}
    else if(!token_ok){outcome="invalid_token_response";explanation="enrollment response has no valid device token";}
    else outcome="token_received";
    out["outcome"].set_string_value(outcome);
    out["hint"].set_string_value(explanation);
    // Protobuf escapes quotes/control bytes. No arbitrary response fields, Token,
    // device_id, parser diagnostics or request contents are serialized to logs.
    if(!google::protobuf::util::MessageToJsonString(summary,&result.diagnostic).ok())
        result.diagnostic="{\"outcome\":\"diagnostic_serialization_failed\"}";
    return result;
}
} // namespace enrollment
