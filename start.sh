#!/usr/bin/env bash
# Tabbed web terminal on http://<this-host>:7682 — no auth, meant for a trusted VLAN.
#   BIND=127.0.0.1 ./start.sh   -> localhost only
#   PORT=8000 ./start.sh
set -euo pipefail
cd "$(dirname "$0")"
[[ -d node_modules ]] || npm install --no-fund --no-audit
exec ./server.py
