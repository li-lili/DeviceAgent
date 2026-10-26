cmake_minimum_required(VERSION 3.17)
project(DeviceAgent LANGUAGES C CXX)
set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
find_package(Threads REQUIRED)
find_package(OpenSSL REQUIRED)

if(ANDROID)
    # Android 使用目标架构的依赖，以及在构建电脑上运行的 protoc。
    find_package(Protobuf CONFIG REQUIRED)
    find_package(CURL CONFIG REQUIRED)
    find_package(LibArchive REQUIRED)
    set(DEVICE_AGENT_PROTOC "" CACHE FILEPATH "Host protoc matching the target Protobuf version")
    if(NOT EXISTS "${DEVICE_AGENT_PROTOC}")
        message(FATAL_ERROR "Set DEVICE_AGENT_PROTOC to the host protoc executable")
    endif()
    execute_process(COMMAND "${DEVICE_AGENT_PROTOC}" --version
        RESULT_VARIABLE protoc_status OUTPUT_VARIABLE protoc_version ERROR_VARIABLE protoc_error)
    if(NOT protoc_status EQUAL 0)
        message(FATAL_ERROR "Host protoc cannot run: ${protoc_error}")
    endif()
    message(STATUS "Host protocol compiler: ${protoc_version}")
    set(PROTO_SRCS)
    foreach(schema cloud business)
        set(generated "${CMAKE_CURRENT_BINARY_DIR}/${schema}.pb.cc")
        set(header "${CMAKE_CURRENT_BINARY_DIR}/${schema}.pb.h")
        add_custom_command(OUTPUT "${generated}" "${header}"
            COMMAND "${DEVICE_AGENT_PROTOC}" "--proto_path=${CMAKE_CURRENT_SOURCE_DIR}/proto"
                "--cpp_out=${CMAKE_CURRENT_BINARY_DIR}" "${schema}.proto"
            DEPENDS "${CMAKE_CURRENT_SOURCE_DIR}/proto/${schema}.proto"
            VERBATIM)
        list(APPEND PROTO_SRCS "${generated}")
    endforeach()
    include(android/cmake/Mosquitto.cmake)
    set(AGENT_LIBRARIES protobuf::libprotobuf libmosquitto_static
        CURL::libcurl LibArchive::LibArchive)
else()
    # Ubuntu 沿用系统开发包，不需要下载 Android 依赖。
    find_package(Protobuf REQUIRED)
    find_package(PkgConfig REQUIRED)
    pkg_check_modules(MQTT REQUIRED IMPORTED_TARGET libmosquitto)
    pkg_check_modules(CURL REQUIRED IMPORTED_TARGET libcurl)
    pkg_check_modules(ARCHIVE REQUIRED IMPORTED_TARGET libarchive)
    protobuf_generate_cpp(PROTO_SRCS PROTO_HDRS proto/cloud.proto proto/business.proto)
    set(AGENT_LIBRARIES protobuf::libprotobuf PkgConfig::MQTT PkgConfig::CURL PkgConfig::ARCHIVE)
endif()

add_executable(device-agent src/main.cpp ${PROTO_SRCS})
target_include_directories(device-agent PRIVATE "${CMAKE_CURRENT_BINARY_DIR}")
target_link_libraries(device-agent PRIVATE ${AGENT_LIBRARIES} OpenSSL::Crypto Threads::Threads)
target_compile_options(device-agent PRIVATE -Wall -Wextra -Wpedantic)

if(ANDROID)
    set(DEVICE_AGENT_ANDROID_DATA_DIR "/data/vendor/logagent" CACHE STRING "Android runtime directory")
    target_compile_definitions(device-agent PRIVATE
        DEVICE_AGENT_ANDROID_DATA_DIR="${DEVICE_AGENT_ANDROID_DATA_DIR}")
    target_include_directories(device-agent PRIVATE "${mosquitto_SOURCE_DIR}/include")
    # Agent 自身直接写 Logcat，不能仅依赖其他库间接链接 liblog。
    find_library(ANDROID_LOG_LIBRARY log)
    if(NOT ANDROID_LOG_LIBRARY)
        message(FATAL_ERROR "Android liblog was not found")
    endif()
    target_link_libraries(device-agent PRIVATE "${ANDROID_LOG_LIBRARY}")
endif()
