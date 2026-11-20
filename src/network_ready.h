#pragma once

#include <curl/curl.h>
#include <atomic>
#include <memory>
#include <stdexcept>
#include <string>

namespace network_ready {

struct Target {
    std::string label, host, port, url;
};

// Extract only the endpoint: never probe the enroll path or log URL credentials.
inline Target target(const std::string& label, std::string url) {
    if(url.rfind("wss://",0)==0)url.replace(0,6,"https://");
    std::unique_ptr<CURLU,decltype(&curl_url_cleanup)> parsed(curl_url(),curl_url_cleanup);
    if(!parsed||curl_url_set(parsed.get(),CURLUPART_URL,url.c_str(),0)!=CURLUE_OK)
        throw std::runtime_error("invalid network target: "+label);
    auto part=[&](CURLUPart field) {
        char* value=nullptr;
        if(curl_url_get(parsed.get(),field,&value,CURLU_DEFAULT_PORT)!=CURLUE_OK)
            throw std::runtime_error("invalid network target: "+label);
        std::unique_ptr<char,decltype(&curl_free)> owned(value,curl_free);
        return std::string(value);
    };
    const auto scheme=part(CURLUPART_SCHEME);
    if(scheme!="http"&&scheme!="https")throw std::runtime_error("invalid network scheme: "+label);
    Target result{label,part(CURLUPART_HOST),part(CURLUPART_PORT),{}};
    // Plain TCP only: actual HTTPS/WSS authentication still happens afterwards.
    result.url="http://"+result.host+":"+result.port+"/";
    return result;
}

inline bool probe(const Target& endpoint,std::atomic<bool>& stopping,std::string& reason) {
    std::unique_ptr<CURL,decltype(&curl_easy_cleanup)> curl(curl_easy_init(),curl_easy_cleanup);
    if(!curl)throw std::runtime_error("cannot initialize network readiness probe");
    curl_easy_setopt(curl.get(),CURLOPT_URL,endpoint.url.c_str());
    curl_easy_setopt(curl.get(),CURLOPT_CONNECT_ONLY,1L);
    curl_easy_setopt(curl.get(),CURLOPT_PROXY,"");
    curl_easy_setopt(curl.get(),CURLOPT_CONNECTTIMEOUT_MS,5000L);
    curl_easy_setopt(curl.get(),CURLOPT_TIMEOUT_MS,5000L);
    curl_easy_setopt(curl.get(),CURLOPT_NOSIGNAL,1L);
    curl_easy_setopt(curl.get(),CURLOPT_NOPROGRESS,0L);
    curl_easy_setopt(curl.get(),CURLOPT_XFERINFOFUNCTION,
        +[](void* context,curl_off_t,curl_off_t,curl_off_t,curl_off_t)->int {
            return static_cast<std::atomic<bool>*>(context)->load()?1:0;
        });
    curl_easy_setopt(curl.get(),CURLOPT_XFERINFODATA,&stopping);
    auto status=curl_easy_perform(curl.get());
    reason=curl_easy_strerror(status);
    return status==CURLE_OK;
}

} // namespace network_ready
