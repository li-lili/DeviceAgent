# 固定 Mosquitto 2.0.22 源码；只链接 MQTT 客户端静态库，不构建 Broker。
include(FetchContent)
foreach(option WITH_BROKER WITH_CLIENTS WITH_APPS WITH_PLUGINS WITH_CJSON WITH_LIB_CPP DOCUMENTATION WITH_SRV)
    set(${option} OFF CACHE BOOL "" FORCE)
endforeach()
set(WITH_TLS ON CACHE BOOL "" FORCE)
set(WITH_THREADING ON CACHE BOOL "" FORCE)
set(WITH_STATIC_LIBRARIES ON CACHE BOOL "" FORCE)
set(WITH_PIC ON CACHE BOOL "" FORCE)
FetchContent_Declare(mosquitto
    URL https://codeload.github.com/eclipse-mosquitto/mosquitto/tar.gz/12e4aaacb7075c7c4425df01a1cfd69416187559
    URL_HASH SHA256=d7e8db7e87c2a7a8d280f2bfa04ee4a6e970e13fb1f3f80b4dcb03c9216ce3d5)
FetchContent_GetProperties(mosquitto)
if(NOT mosquitto_POPULATED)
    FetchContent_Populate(mosquitto)
    add_subdirectory("${mosquitto_SOURCE_DIR}" "${mosquitto_BINARY_DIR}" EXCLUDE_FROM_ALL)
endif()

# Mosquitto 用 ANDROID 宏避开 Bionic 不支持的 pthread_cancel。
target_compile_definitions(libmosquitto_static PRIVATE ANDROID)
