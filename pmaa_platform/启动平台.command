#!/bin/bash
# 双击即可启动 PMAA 甲基化分析平台
exec "$(cd -P "$(dirname "${BASH_SOURCE[0]}")" && pwd)/scripts/launcher.sh" "$@"
