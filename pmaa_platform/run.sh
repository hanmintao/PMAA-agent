#!/usr/bin/env bash
# PMAA 甲基化分析平台 —— 一键启动脚本（macOS / Linux）
set -euo pipefail
cd "$(dirname "$0")"

PY="${PMAA_PYTHON:-python3}"
VENV=".venv"

if [ ! -d "$VENV" ]; then
  echo ">>> 创建虚拟环境 $VENV"
  "$PY" -m venv "$VENV"
fi
# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo ">>> 安装依赖"
pip install -q -r requirements.txt

echo ">>> 初始化数据库"
PYTHONPATH=src python -m pmaa.cli init-db

if [ -n "${PMAA_EI_TABLE:-}" ] && [ -f "${PMAA_EI_TABLE}" ]; then
  echo ">>> 导入 EI 汇总表（PMAA 数据库）"
  PYTHONPATH=src python -m pmaa.cli import-ei "${PMAA_EI_TABLE}" --commit --method-id SP2380_demo_v1
else
  echo ">>> 未设置 PMAA_EI_TABLE；保留现有参考库，可在网页上传 xlsx"
fi

echo ">>> 写入示例样本 / 方法 / 离线 LLM 配置"
PYTHONPATH=src python -m pmaa.cli seed-demo

echo
echo ">>> 启动服务： http://127.0.0.1:${PORT:-8000}"
echo "    API 文档： http://127.0.0.1:${PORT:-8000}/docs"
PYTHONPATH=src exec uvicorn pmaa.api.main:app --host 127.0.0.1 --port "${PORT:-8000}"
