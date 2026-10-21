// Exercise the production parser, including literal escaping and invalid dates.
#define main device_agent_main
#include "../src/main.cpp"
#undef main
#include <cassert>
int main() {
    std::vector<FilenameDateFormat> defaults{FilenameDateFormat("%Y-%m-%d")};
    const auto expected=filename_day("2026-10-21.txt",defaults);
    assert(expected>0);
    std::vector<FilenameDateFormat> formats{
        FilenameDateFormat("%Y-%m-%d.txt"), FilenameDateFormat("%Y%m%d.txt"),
        FilenameDateFormat("app_%d.%m.%Y.log")};
    for(const auto* name:{"2026-10-21.txt","20261021.txt","prefix-20261021.txt","app_21.10.2026.log"})
        assert(filename_day(name,formats)==expected);
    for(const auto* name:{"20260230.txt","20261301.txt","2026102.txt","120261021.txt","app_21x10x2026.log","current.txt"})
        assert(filename_day(name,formats)==-1);
    assert(filename_day("2024-02-29.log",defaults)>0);
    assert(filename_day("2026-02-29.log",defaults)==-1);
    assert(filename_day("app-2026-10-21.1.log",defaults)==expected);
    std::vector<FilenameDateFormat> percent{FilenameDateFormat("%Y%m%d%%.txt")};
    assert(filename_day("20261021%.txt",percent)==expected);
    for(const auto* format:{"","%Y%m","%Y%Y%m%d","%y%m%d","%Y%m%d%"}) {
        bool rejected=false;
        try{FilenameDateFormat parsed(format);}catch(const Failure&){rejected=true;}
        assert(rejected);
    }
}
