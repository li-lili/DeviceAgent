#include "enroll_response.h"
#include <iostream>
#include <stdexcept>

static int cases=0;
static void require(bool value,const char* label) {
    if(!value)throw std::runtime_error(label); // Never dump response/credentials on assertion failure.
}
static google::protobuf::Struct summary(const enrollment::Response& r) {
    google::protobuf::Struct value;
    require(google::protobuf::util::JsonStringToMessage(r.diagnostic,&value).ok(),"diagnostic JSON invalid");
    require(r.diagnostic.find('\n')==std::string::npos,"multiline diagnostic");
    ++cases;
    return value;
}
static std::string str(const google::protobuf::Struct& s,const char* key) {
    auto it=s.fields().find(key);return it==s.fields().end()?"":it->second.string_value();
}
static enrollment::Response response(long code,const std::string& body,const std::string& signature={},bool complete=true) {
    auto r=enrollment::inspect(code,body,signature,complete);
    auto s=summary(r);
    require(s.fields().at("http_status").number_value()==code,"HTTP status not retained");
    return r;
}
static void no_secret(const enrollment::Response& r,const std::string& value) {
    require(r.diagnostic.find(value)==std::string::npos,"credential appeared in diagnostic");
}

int main() {
    try {
        for(long code:{200,201}) {
            auto r=response(code,R"({"token":"FAKE_ACCESS_VALUE","message":"accepted"})");
            require(r.success,"valid success rejected");no_secret(r,"FAKE_ACCESS_VALUE");
            require(str(summary(r),"message")=="accepted","success message missing");
            require(str(summary(r),"outcome")=="token_received","success outcome incorrect");
        }
        for(auto item:{std::pair<long,const char*>{400,"bad_request"},{401,"unauthorized"},
                      {409,"already_enrolled"},{429,"rate_limited"},{502,"upstream_failed"}}) {
            for(bool nested:{false,true}) {
                std::string body=nested?std::string("{\"message\":\"top message\",\"error\":{\"code\":\"")+item.second+"\",\"message\":\"cloud detail\"}}":
                    std::string("{\"error\":\"")+item.second+"\",\"message\":\"cloud detail\"}";
                auto r=response(item.first,body);require(!r.success,"HTTP error accepted");
                auto s=summary(r);
                require(str(s,"error_code")==item.second,"flat/nested error code lost");
                require(str(s,nested?"error_message":"message")=="cloud detail","server message lost");
            }
        }
        auto r=response(409,R"({"error":{"code":"device_identity_already_exists","message":"existing registration"}})");
        require(str(summary(r),"error_code")=="device_identity_already_exists","unknown semantic code replaced");
        require(r.diagnostic.find("request_failed")==std::string::npos,"fallback invented");
        for(long code:{200,201,202,204,301,400,401,409,429,502,599}) {
            for(const std::string body:{"", "<html>gateway FAKE_HTML_SECRET</html>", "not json", "null", "[]", "42", "{\"error\":"}) {
                r=response(code,body);require(!r.success,"malformed/non-object body accepted");
                require(str(summary(r),"json_state")=="invalid_or_non_object","JSON error not identified");
                no_secret(r,"FAKE_HTML_SECRET");
            }
        }
        for(const std::string body:{"{}", "{\"token\":null}", "{\"token\":123}", "{\"token\":\"\"}",
                                   "{\"token\":\"two values\"}", "{\"token\":\"nul\\u0000value\"}",
                                   "{\"token\":\"FAKE_ACCESS_VALUE\",\"error\":{\"code\":\"unauthorized\"}}"}) {
            r=response(200,body);require(!r.success,"invalid/conflicting token accepted");no_secret(r,"FAKE_ACCESS_VALUE");
        }
        r=response(202,R"({"token":"FAKE_ACCESS_VALUE"})");
        require(!r.success,"unexpected 2xx accepted");no_secret(r,"FAKE_ACCESS_VALUE");
        for(const std::string error:{"null","5","[]","{}","{\"code\":5,\"message\":true}"}) {
            r=response(409,"{\"error\":"+error+"}");require(!r.success,"malformed error accepted");
            require(str(summary(r),"error_code_state")!="present","malformed code marked present");
        }
        // Structured secret fields also redact occurrences in metadata; arbitrary
        // response fields and cloud device IDs must never be logged.
        r=response(409,R"({"error":{"code":"identity_conflict","message":"echo FAKE_FIELD_VALUE"},"access_token":"FAKE_FIELD_VALUE","device_id":"FAKE_DEVICE_ID","extra":"FAKE_EXTRA"})");
        for(const char* secret:{"FAKE_FIELD_VALUE","FAKE_DEVICE_ID","FAKE_EXTRA"})no_secret(r,secret);
        r=response(409,R"({"error":{"code":"FAKE_CODE_TOKEN","message":"echo FAKE_CODE_TOKEN"},"refresh_token":"FAKE_CODE_TOKEN"})");
        no_secret(r,"FAKE_CODE_TOKEN");
        r=response(409,R"({"error":{"code":"bad_request","message":"echo REQUEST_SIG_DUMMY"}})","REQUEST_SIG_DUMMY");
        no_secret(r,"REQUEST_SIG_DUMMY");
        for(const std::string message:{"Bearer DUMMY_BEARER", "Basic DUMMY_BASIC", "token=DUMMY_TOKEN",
                                      "signature DUMMY_SIGNATURE", "password: DUMMY_PASSWORD",
                                      "private key DUMMY_PRIVATE", "-----BEGIN PRIVATE KEY----- DUMMY_PEM",
                                      "https://user:DUMMY_URL_PASS@server/x?credential=DUMMY_QUERY",
                                      "lsd_DUMMY_PREFIXED", "eyJheader.eyJclaims.DUMMY_JWT",
                                      "echo ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"}) {
            google::protobuf::Struct body;
            (*body.mutable_fields())["message"].set_string_value(message);
            std::string encoded;google::protobuf::util::MessageToJsonString(body,&encoded);
            r=response(409,encoded);
            require(r.diagnostic.find("DUMMY_")==std::string::npos,"credential context not removed");
            no_secret(r,"ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789");
        }
        r=response(409,R"({"error":{"code":"bad_request","message":"first\nFORGED\r\n\u001b[31m"}})");
        summary(r); // Must remain single-line valid JSON, never an injected log record.
        r=response(409,"{\"message\":\""+std::string(3000,'x')+"\"}");
        require(str(summary(r),"message")=="[omitted: metadata too long]","long metadata not suppressed");
        r=response(200,R"({"token":"FAKE_ACCESS_VALUE","message":"accepted"})",{},false);
        require(!r.success,"incomplete transport accepted");
        require(str(summary(r),"outcome")=="transport_error","transport failure not classified");
        r=response(0,"",{},false);require(!r.success,"no HTTP response accepted");
        std::cout<<"PASS "<<cases<<" offline response/diagnostic assertions; no network or credentials used\n";
    } catch(const std::exception& e) {
        std::cerr<<"FAIL "<<e.what()<<"\n";return 1;
    }
}
