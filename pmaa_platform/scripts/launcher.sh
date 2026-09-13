#!/usr/bin/env bash
# ============================================================================
#  PMAA 甲基化分析平台 —— 一键启动器（被 .command 与 .app 共用）
#
#  1. 定位项目目录（跟随脚本自身位置，双击也能正确工作）
#  2. 准备 Python 虚拟环境（仅首次创建）
#  3. 安装依赖（仅缺失时安装，之后秒开）
#  4. 初始化数据库；库为空时自动导入 PMAA 的 EI 汇总表
#  5. 端口占用则提示并退出，避免静默失败
#  6. 启动服务并自动打开浏览器
#
#  环境变量：PORT（默认 8000）、PMAA_PYTHON（默认 python3）、PMAA_EI_TABLE
#
#  注意：所有变量展开一律使用 ${VAR} 花括号形式。
#  macOS 自带 bash 3.2 在"全角字符紧邻 $VAR"时会把多字节字符误并入变量名，
#  $VAR） 会被解析成变量名 PYVER＋全角括号 → unbound variable。
# ============================================================================
set -euo pipefail
export LC_ALL="${LC_ALL:-en_US.UTF-8}" 2>/dev/null || true

# --- 定位项目目录（兼容符号链接） ------------------------------------------
SOURCE="${BASH_SOURCE[0]}"
while [ -L "${SOURCE}" ]; do
  DIR="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
  SOURCE="$(readlink "${SOURCE}")"
  [[ ${SOURCE} != /* ]] && SOURCE="${DIR}/${SOURCE}"
done
SCRIPT_DIR="$(cd -P "$(dirname "${SOURCE}")" && pwd)"
PROJECT_DIR="$(cd -P "${SCRIPT_DIR}/.." && pwd)"
cd "${PROJECT_DIR}"

PORT="${PORT:-8000}"
PY="${PMAA_PYTHON:-python3}"
VENV="${PROJECT_DIR}/.venv"
RUN_DIR="${PROJECT_DIR}/.run"
PID_FILE="${RUN_DIR}/server.pid"
PORT_FILE="${RUN_DIR}/server.port"
mkdir -p "${RUN_DIR}"
EI_TABLE="${PMAA_EI_TABLE:-}"
if [ -z "${EI_TABLE}" ]; then
  EI_TABLE="$(find "${PROJECT_DIR}/data/uploads" -maxdepth 1 -type f -name '*.xlsx' -print -quit 2>/dev/null || true)"
fi

say() { printf '\033[36m>>> %s\033[0m\n' "${1}"; }
ok()  { printf '\033[32m[ok] %s\033[0m\n' "${1}"; }
err() { printf '\033[31m[!!] %s\033[0m\n' "${1}" >&2; }

# --- 0. 前置检查 ------------------------------------------------------------
command -v "${PY}" >/dev/null 2>&1 || { err "找不到 ${PY} ，请先安装 Python 3.10+"; exit 1; }

if [ ! -x "${VENV}/bin/python" ]; then
  say "首次运行：创建虚拟环境 .venv"
  "${PY}" -m venv "${VENV}"
fi
# shellcheck disable=SC1091
source "${VENV}/bin/activate"
PYVER="$("${VENV}/bin/python" -c 'import sys;print("%d.%d" % sys.version_info[:2])')"
ok "Python ${PYVER}"

# --- 2. 依赖（用标记文件避免每次都 pip） ------------------------------------
STAMP="${VENV}/.deps.stamp"
if [ ! -f "${STAMP}" ] || [ requirements.txt -nt "${STAMP}" ]; then
  say "安装/更新依赖（首次约需 1 分钟）"
  python -m pip install --quiet --upgrade pip >/dev/null 2>&1 || true
  python -m pip install --quiet -r requirements.txt
  touch "${STAMP}"
  ok "依赖就绪"
else
  ok "依赖已就绪（跳过安装）"
fi

# --- 3. 数据库 --------------------------------------------------------------
export PYTHONPATH="${PROJECT_DIR}/src"
if [ ! -f "${PROJECT_DIR}/data/pmaa.db" ]; then
  say "初始化数据库"
  python -m pmaa.cli init-db
fi

# --- 4. 参考库为空则导入 EI 汇总表 ------------------------------------------
REF_COUNT="$(python -c 'from pmaa.data import repositories as r
try:
    print(r.ReferenceRepo.count())
except Exception:
    print(0)' 2>/dev/null || echo 0)"

if [ "${REF_COUNT:-0}" = "0" ]; then
  if [ -n "${EI_TABLE}" ] && [ -f "${EI_TABLE}" ]; then
    say "参考库为空，导入 PMAA 的 EI 汇总表"
    python -m pmaa.cli import-ei --commit --method-id SP2380_demo_v1 >/tmp/pmaa_import.json 2>&1 || true
    python -m pmaa.cli seed-demo >/dev/null 2>&1 || true
    ok "参考库已导入"
  else
    err "参考库为空，且未找到 EI 汇总表。"
    err "可在「数据导入」页上传 xlsx，或设置环境变量 PMAA_EI_TABLE 指向该文件。"
  fi
else
  ok "参考库已有 ${REF_COUNT} 条记录"
fi

# --- 5. 单实例：再次双击时重启受管实例，确保当前代码真正生效 ------------------
if [ -f "${PID_FILE}" ]; then
  OLD_PID="$(sed -n '1p' "${PID_FILE}" 2>/dev/null || true)"
  OLD_PORT="$(sed -n '1p' "${PORT_FILE}" 2>/dev/null || true)"
  if [ -n "${OLD_PID}" ] && kill -0 "${OLD_PID}" >/dev/null 2>&1; then
    say "检测到本平台旧实例 PID ${OLD_PID}${OLD_PORT:+（端口 ${OLD_PORT}）}，正在重启以加载当前代码"
    kill "${OLD_PID}" >/dev/null 2>&1 || true
    for _ in $(seq 1 40); do
      kill -0 "${OLD_PID}" >/dev/null 2>&1 || break
      sleep 0.1
    done
    if kill -0 "${OLD_PID}" >/dev/null 2>&1; then
      err "旧实例未能正常停止，请在其终端按 Ctrl+C 后重试"
      exit 1
    fi
  fi
  rm -f "${PID_FILE}" "${PORT_FILE}"
fi

# --- 6. 端口选择：被占用则自动顺延，避免双击后静默失败 ----------------------
port_free() { ! lsof -nP -iTCP:"${1}" -sTCP:LISTEN >/dev/null 2>&1; }

WANTED="${PORT}"
TRIED=0
while ! port_free "${PORT}" && [ "${TRIED}" -lt 20 ]; do
  PORT=$((PORT + 1)); TRIED=$((TRIED + 1))
done
if ! port_free "${PORT}"; then
  err "端口 ${WANTED} 起的 20 个端口全被占用。请显式指定： PORT=8010 ./启动平台.command"
  exit 1
fi
if [ "${PORT}" != "${WANTED}" ]; then
  err "端口 ${WANTED} 已被占用，自动改用 ${PORT}"
fi

# --- 7. 启动 ----------------------------------------------------------------
URL="http://127.0.0.1:${PORT}"
say "启动服务：${URL}"
printf '    API 文档：%s/docs\n' "${URL}"
printf '    停止服务：在本窗口按 Ctrl+C\n\n'

# 服务就绪后自动打开浏览器
(
  for _ in $(seq 1 60); do
    if curl -fsS "${URL}/v1/health" >/dev/null 2>&1; then
      if command -v open >/dev/null 2>&1; then open "${URL}"
      elif command -v xdg-open >/dev/null 2>&1; then xdg-open "${URL}"
      fi
      break
    fi
    sleep 0.5
  done
) >/dev/null 2>&1 &

python -m uvicorn pmaa.api.main:app --host 127.0.0.1 --port "${PORT}" &
SERVER_PID=$!
printf '%s\n' "${SERVER_PID}" > "${PID_FILE}"
printf '%s\n' "${PORT}" > "${PORT_FILE}"

cleanup() {
  trap - EXIT INT TERM
  if kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
    wait "${SERVER_PID}" >/dev/null 2>&1 || true
  fi
  rm -f "${PID_FILE}" "${PORT_FILE}"
  echo
  say "已停止服务"
}
trap cleanup EXIT INT TERM
wait "${SERVER_PID}"
