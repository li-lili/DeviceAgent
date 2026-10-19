#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
cmake -S . -B build -DCMAKE_BUILD_TYPE=Debug
cmake --build build -j2
mkdir -p build/python
protoc -I proto --python_out=build/python proto/cloud.proto proto/business.proto
