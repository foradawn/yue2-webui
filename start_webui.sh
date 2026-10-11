#!/usr/bin/env bash
# 一键启动 YuE2 Web（GPU 推理服务）。幂等：已运行则直接返回。
set -euo pipefail

PORT="${PORT:-7860}"
HOST="${HOST:-0.0.0.0}"
APP_ROOT="/workspace/yue2-web"
VENV="/workspace/YuE/.venv"
LOG_DIR="/workspace/logs"
LOG_FILE="${LOG_DIR}/yue2-web.log"
PID_FILE="${LOG_DIR}/yue2-web.pid"
PYTHON="${VENV}/bin/python"

if [ ! -x "${PYTHON}" ]; then
  echo "[start_webui] 找不到虚拟环境解释器: ${PYTHON}" >&2
  exit 1
fi
if [ ! -f "${APP_ROOT}/app.py" ]; then
  echo "[start_webui] 找不到应用入口: ${APP_ROOT}/app.py" >&2
  exit 1
fi

mkdir -p "${LOG_DIR}"

if curl -sf "http://127.0.0.1:${PORT}/api/device" >/dev/null 2>&1; then
  echo "[start_webui] 服务已在运行: http://127.0.0.1:${PORT}"
  exit 0
fi

cd "${APP_ROOT}"
# shellcheck disable=SC2086
CUDA_VISIBLE_DEVICES="${CUDA_VISIBLE_DEVICES:-0}" nohup "${PYTHON}" app.py \
  --host "${HOST}" --port "${PORT}" >>"${LOG_FILE}" 2>&1 &
PID=$!
echo "${PID}" >"${PID_FILE}"

for _ in $(seq 1 60); do
  if curl -sf "http://127.0.0.1:${PORT}/api/device" >/dev/null 2>&1; then
    GPU=$(curl -s "http://127.0.0.1:${PORT}/api/device")
    echo "[start_webui] 已启动 pid=${PID}"
    echo "[start_webui] 访问: http://127.0.0.1:${PORT}"
    echo "[start_webui] ${GPU}"
    echo "[start_webui] 日志: ${LOG_FILE}"
    exit 0
  fi
  if ! kill -0 "${PID}" 2>/dev/null; then
    echo "[start_webui] 进程已退出，请查看日志: ${LOG_FILE}" >&2
    tail -n 20 "${LOG_FILE}" >&2 || true
    exit 1
  fi
  sleep 1
done

echo "[start_webui] 启动超时（60s），请查看日志: ${LOG_FILE}" >&2
exit 1
