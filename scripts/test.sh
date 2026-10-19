#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/build.sh
/usr/bin/python3 tests/e2e.py
/usr/bin/python3 tests/file_sources_e2e.py
/usr/bin/python3 tests/cloud_defaults.py
/usr/bin/python3 tests/handshake_auto_e2e.py
/usr/bin/python3 tests/config_read.py
/usr/bin/python3 tests/enrollment_e2e.py
