#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
bash scripts/build.sh
/usr/bin/python3 mocks/demo.py "$@"
