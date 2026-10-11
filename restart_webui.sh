#!/usr/bin/env bash
# 重启 YuE2 Web 服务：先停掉旧进程（pid 文件 + 端口 + 命令行兜底），再重新拉起。
set -euo pipefail

PORT="${PORT:-7860}"
LOG_DIR="/workspace/logs"
LOG_FILE="${LOG_DIR}/yue2-web.log"
PID_FILE="${LOG_DIR}/yue2-web.pid"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

stopped=0
was_running=0

if curl -sf "http://127.0.0.1:${PORT}/api/device" >/dev/null 2>&1; then
  was_running=1
fi

stop_pid() {
  local pid=$1
  if [ -n "${pid}" ] && kill -0 "${pid}" 2>/dev/null; then
    kill "${pid}" 2>/dev/null || true
    for _ in $(seq 1 20); do
      kill -0 "${pid}" 2>/dev/null || { stopped=1; return 0; }
      sleep 0.5
    done
    kill -9 "${pid}" 2>/dev/null || true
    stopped=1
  fi
}

if [ -f "${PID_FILE}" ]; then
  stop_pid "$(cat "${PID_FILE}" 2>/dev/null || true)"
  rm -f "${PID_FILE}"
fi

# 兜底：按端口与命令行清理残留进程
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" >/dev/null 2>&1 || true
fi
pkill -f "yue2-web/app.py" >/dev/null 2>&1 || true
pkill -f "app.py --host" >/dev/null 2>&1 || true
sleep 1

if [ "${was_running}" = "1" ] || [ "${stopped}" = "1" ]; then
  echo "[restart_webui] 旧进程已停止"
else
  echo "[restart_webui] 未发现运行中的旧进程"
fi

mv -f "${LOG_FILE}" "${LOG_FILE}.old" 2>/dev/null || true
exec "${SCRIPT_DIR}/start_webui.sh"
