#!/usr/bin/env bash
set -u
set -o pipefail

# 兼容入口：保持历史使用方式 bash run.sh <mode>
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec bash "${SCRIPT_DIR}/verify_e2e.sh" "$@"

