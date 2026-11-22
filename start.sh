#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(realpath -- "$0")")"
exec python3 -u thsr_monitor.py "$@"
