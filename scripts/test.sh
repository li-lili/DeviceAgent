#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/build.sh
c++ -std=c++17 -I build tests/filename_date_formats.cpp build/cloud.pb.cc build/business.pb.cc -o build/filename-date-formats-test $(pkg-config --cflags --libs protobuf libmosquitto libcurl libarchive openssl) -pthread
./build/filename-date-formats-test
/usr/bin/python3 tests/e2e.py
/usr/bin/python3 tests/file_sources_e2e.py
/usr/bin/python3 tests/cloud_defaults.py
/usr/bin/python3 tests/handshake_auto_e2e.py
/usr/bin/python3 tests/config_read.py
/usr/bin/python3 tests/enrollment_e2e.py
/usr/bin/python3 tests/https_ca_e2e.py
