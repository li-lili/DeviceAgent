#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/build.sh
c++ -std=c++17 -I build tests/filename_date_formats.cpp src/ws_tunnel.cpp build/cloud.pb.cc build/business.pb.cc -o build/filename-date-formats-test $(pkg-config --cflags --libs protobuf libmosquitto libcurl libarchive openssl) -pthread -ldl
./build/filename-date-formats-test
cc -shared -fPIC -Wall -Wextra -o build/libfactory_service_mock.so tests/factory_service_mock.c
/usr/bin/python3 tests/e2e.py
/usr/bin/python3 tests/file_sources_e2e.py
/usr/bin/python3 tests/cloud_defaults.py
/usr/bin/python3 tests/handshake_auto_e2e.py
/usr/bin/python3 tests/config_read.py
/usr/bin/python3 tests/enrollment_e2e.py
/usr/bin/python3 tests/vendor_storage_e2e.py
/usr/bin/python3 tests/identity_wait_e2e.py
/usr/bin/python3 tests/network_readiness_e2e.py
/usr/bin/python3 tests/enrollment_responses_e2e.py
/usr/bin/python3 tests/https_ca_e2e.py
