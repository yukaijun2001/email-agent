#!/bin/sh

set -u

ROOT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd) || exit 1
ENTRYPOINT="$ROOT_DIR/scripts/main.py"
RUNTIME_DIR="$ROOT_DIR/.runtime"
PID_FILE="$RUNTIME_DIR/inbox.pid"
LOG_FILE="$RUNTIME_DIR/inbox.log"

usage() {
  printf '用法: %s {start|stop|restart|status}\n' "$0" >&2
}

is_our_process() {
  candidate_pid=$1
  case "$candidate_pid" in
    ''|*[!0-9]*) return 1 ;;
  esac
  kill -0 "$candidate_pid" 2>/dev/null || return 1
  command_line=$(ps -p "$candidate_pid" -o command= 2>/dev/null || true)
  case "$command_line" in
    *"$ENTRYPOINT"*) return 0 ;;
    *) return 1 ;;
  esac
}

read_managed_pid() {
  [ -f "$PID_FILE" ] || return 1
  candidate_pid=$(cat "$PID_FILE" 2>/dev/null || true)
  is_our_process "$candidate_pid" || return 1
  printf '%s\n' "$candidate_pid"
}

start_service() {
  if managed_pid=$(read_managed_pid); then
    printf '服务已在运行，PID: %s\n' "$managed_pid"
    return 0
  fi

  if ! command -v python3 >/dev/null 2>&1; then
    printf '错误: 未找到 python3。\n' >&2
    return 1
  fi

  mkdir -p "$RUNTIME_DIR" || return 1
  if [ -f "$PID_FILE" ]; then
    rm -f "$PID_FILE"
  fi

  nohup python3 -u "$ENTRYPOINT" >>"$LOG_FILE" 2>&1 </dev/null &
  service_pid=$!
  if ! printf '%s\n' "$service_pid" >"$PID_FILE"; then
    kill "$service_pid" 2>/dev/null || true
    printf '错误: 无法写入 PID 文件。\n' >&2
    return 1
  fi

  sleep 1
  if is_our_process "$service_pid"; then
    printf '服务已启动，PID: %s\n' "$service_pid"
    printf '地址: http://127.0.0.1:8000\n'
    printf '日志: %s\n' "$LOG_FILE"
    return 0
  fi

  rm -f "$PID_FILE"
  printf '错误: 服务启动失败，最近日志如下:\n' >&2
  if [ -f "$LOG_FILE" ]; then
    tail -n 20 "$LOG_FILE" >&2
  fi
  return 1
}

stop_service() {
  if [ ! -f "$PID_FILE" ]; then
    printf '服务未运行（没有 PID 文件）。\n'
    return 0
  fi

  service_pid=$(cat "$PID_FILE" 2>/dev/null || true)
  if ! is_our_process "$service_pid"; then
    rm -f "$PID_FILE"
    printf '服务未运行（PID 文件已过期）。\n'
    return 0
  fi

  if ! kill "$service_pid" 2>/dev/null; then
    printf '错误: 无法停止服务 PID %s。\n' "$service_pid" >&2
    return 1
  fi

  attempts=0
  while is_our_process "$service_pid"; do
    if [ "$attempts" -ge 10 ]; then
      printf '错误: 服务 10 秒内未退出，PID 文件已保留。\n' >&2
      return 1
    fi
    sleep 1
    attempts=$((attempts + 1))
  done

  if [ "$(cat "$PID_FILE" 2>/dev/null || true)" = "$service_pid" ]; then
    rm -f "$PID_FILE"
  fi
  printf '服务已停止。\n'
}

status_service() {
  if managed_pid=$(read_managed_pid); then
    printf '服务运行中，PID: %s\n' "$managed_pid"
    printf '地址: http://127.0.0.1:8000\n'
  else
    printf '服务未运行。\n'
  fi
}

case "${1:-}" in
  start)
    start_service
    ;;
  stop)
    stop_service
    ;;
  restart)
    stop_service && start_service
    ;;
  status)
    status_service
    ;;
  *)
    usage
    exit 2
    ;;
esac
