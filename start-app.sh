#!/usr/bin/env bash
set -euo pipefail
export HOME=/config
cd /app
exec /opt/atmos-venv/bin/python /app/main.py
