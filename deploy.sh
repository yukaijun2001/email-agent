#!/bin/sh

set -eu

ROOT_DIR=$(CDPATH= cd "$(dirname "$0")" && pwd) || exit 1
cd "$ROOT_DIR"

export INBOX_UID=$(id -u)
export INBOX_GID=$(id -g)
ENV_FILE="$ROOT_DIR/.docker.env"

usage() {
  cat <<EOF
用法: $0 <命令>

命令:
  start       构建（如有需要）并后台启动应用和 PostgreSQL
  stop        停止并移除应用和 PostgreSQL 容器（保留数据库卷）
  restart     重新构建并启动服务
  status      查看容器状态
  logs        查看应用实时日志（Ctrl+C 退出日志查看）
  build       仅构建应用镜像，不启动容器
  help        显示此帮助

兼容别名:
  up=start, down=stop

示例:
  $0 start
  $0 stop
EOF
}

if ! command -v docker >/dev/null 2>&1; then
  printf '%s\n' '错误: 未找到 Docker，请先安装并启动 Docker Desktop 或 Docker Engine。' >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE_KIND=plugin
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_KIND=standalone
else
  printf '%s\n' '错误: 未找到 docker compose 或 docker-compose。' >&2
  exit 1
fi

compose() {
  if [ -f "$ENV_FILE" ]; then
    if [ "$COMPOSE_KIND" = plugin ]; then
      docker compose --env-file "$ENV_FILE" "$@"
    else
      docker-compose --env-file "$ENV_FILE" "$@"
    fi
  elif [ "$COMPOSE_KIND" = plugin ]; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

require_docker() {
  if ! docker info >/dev/null 2>&1; then
    printf '%s\n' '错误: Docker 引擎未运行，请先启动 Docker Desktop 或 Docker Engine。' >&2
    exit 1
  fi
}

prepare_data() {
  mkdir -p data
  if [ -f inbox_config.json ] && [ ! -e data/inbox_config.json ]; then
    cp inbox_config.json data/inbox_config.json
    chmod 600 data/inbox_config.json
    printf '%s\n' '已迁移现有邮箱配置到 data/inbox_config.json。'
  fi
}

prepare_env() {
  if [ -f "$ENV_FILE" ]; then
    return
  fi
  umask 077
  postgres_password=$(od -An -N24 -tx1 /dev/urandom | tr -d ' \n')
  if [ -z "$postgres_password" ]; then
    printf '%s\n' '错误: 无法生成 PostgreSQL 密码。' >&2
    exit 1
  fi
  inbox_port=${INBOX_PORT:-8000}
  cat >"$ENV_FILE" <<EOF
POSTGRES_DB=email_agent
POSTGRES_USER=email_agent
POSTGRES_PASSWORD=$postgres_password
INBOX_PORT=$inbox_port
EOF
  printf '%s\n' '已生成 .docker.env，PostgreSQL 密码仅保存在本机该文件中。'
}

read_host_port() {
  if [ -f "$ENV_FILE" ]; then
    sed -n 's/^INBOX_PORT=//p' "$ENV_FILE" | head -n 1
  fi
}

wait_for_service() {
  attempts=0
  while [ "$attempts" -lt 30 ]; do
    container_id=$(compose ps -q inbox 2>/dev/null || true)
    if [ -n "$container_id" ]; then
      container_state=$(docker inspect -f '{{.State.Status}}' "$container_id" 2>/dev/null || true)
      container_health=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container_id" 2>/dev/null || true)
      if [ "$container_state" = running ] && [ "$container_health" = healthy ]; then
        printf '%s\n' '应用健康检查已通过。'
        return 0
      fi
      if [ "$container_state" = exited ] || [ "$container_state" = dead ]; then
        break
      fi
    fi
    sleep 1
    attempts=$((attempts + 1))
  done

  printf '%s\n' '错误: 应用容器未能在 30 秒内启动，最近日志如下:' >&2
  compose logs --no-color --tail=40 inbox >&2 || true
  return 1
}

start_services() {
  require_docker
  prepare_env
  prepare_data
  compose up --build -d --remove-orphans
  wait_for_service
  host_port=$(read_host_port)
  host_port=${host_port:-8000}
  printf '%s\n' '部署完成。'
  printf '访问地址: http://127.0.0.1:%s\n' "$host_port"
  compose ps
}

stop_services() {
  require_docker
  compose down --remove-orphans
  printf '%s\n' '服务已停止，PostgreSQL 数据卷已保留。'
}

restart_services() {
  start_services
}

status_services() {
  require_docker
  compose ps
}

logs_services() {
  require_docker
  compose logs -f --tail=100 inbox
}

build_image() {
  require_docker
  prepare_env
  compose build inbox
  printf '%s\n' '应用镜像构建完成。'
}

case "${1:-start}" in
  up|start)
    start_services
    ;;
  down|stop)
    stop_services
    ;;
  restart)
    restart_services
    ;;
  logs)
    logs_services
    ;;
  status)
    status_services
    ;;
  build)
    build_image
    ;;
  help|-h|--help)
    usage
    ;;
  *)
    printf '错误: 未知命令 "%s"。\n\n' "$1" >&2
    usage >&2
    exit 2
    ;;
esac
