#!/bin/sh

set -efu

usage() {
  cat <<'EOF'
用法:
  curl -fsSL https://raw.githubusercontent.com/yukaijun2001/email-agent/main/uninstall.sh | \
    sudo sh -s -- --yes

选项:
  --project NAME    Compose 项目名（默认: email-agent）
  --dir DIRECTORY   安装目录（仅配合 --remove-dir 使用，默认: /opt/email-agent）
  --remove-dir      同时删除安装目录中的源码和配置文件
  --remove-volumes  同时删除项目数据卷（默认保留，重新部署后继续使用原数据）
  --yes             确认删除容器、镜像和网络
  --help            显示此帮助
EOF
}

fail() {
  printf '错误: %s\n' "$1" >&2
  exit 1
}

project=email-agent
install_dir=/opt/email-agent
remove_dir=0
remove_volumes=0
confirmed=0
errors=0

record_error() {
  printf '错误: %s\n' "$1" >&2
  errors=$((errors + 1))
}

list_contains() (
  target=$1
  shift
  for entry do
    [ "$entry" != "$target" ] || return 0
  done
  return 1
)

while [ "$#" -gt 0 ]; do
  case "$1" in
    --project)
      [ "$#" -ge 2 ] || fail "--project 缺少参数。"
      project=$2
      shift 2
      ;;
    --dir)
      [ "$#" -ge 2 ] || fail "--dir 缺少参数。"
      install_dir=$2
      shift 2
      ;;
    --remove-dir)
      remove_dir=1
      shift
      ;;
    --remove-volumes)
      remove_volumes=1
      shift
      ;;
    --yes)
      confirmed=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      fail "未知参数: $1"
      ;;
  esac
done

[ "$confirmed" -eq 1 ] || { usage >&2; fail '这是破坏性操作，请显式传入 --yes。'; }
case "$project" in
  ''|[!a-z0-9]*|*[!a-z0-9_-]*) fail '--project 必须以小写字母或数字开头，且只能包含小写字母、数字、下划线和短横线。' ;;
esac

if [ "$remove_dir" -eq 1 ]; then
  [ -n "$install_dir" ] || fail '--dir 不能为空。'
  case "$install_dir" in
    /*) ;;
    *) install_dir=$(pwd -P)/$install_dir ;;
  esac
  while [ "$install_dir" != / ] && [ "${install_dir%/}" != "$install_dir" ]; do
    install_dir=${install_dir%/}
  done
  [ ! -L "$install_dir" ] || fail "拒绝删除符号链接目录: $install_dir"
  if [ -e "$install_dir" ]; then
    [ -d "$install_dir" ] || fail "安装路径不是目录: $install_dir"
    install_dir=$(CDPATH= cd -P "$install_dir" && pwd -P) || fail '无法解析安装目录。'
    case "$install_dir" in
      /*/*) ;;
      *) fail "拒绝删除系统顶层目录: $install_dir" ;;
    esac
    if [ -d "${HOME:-}" ]; then
      protected_home=$(CDPATH= cd -P "$HOME" && pwd -P) || fail '无法解析用户主目录。'
      [ "$install_dir" != "$protected_home" ] || fail '拒绝删除用户主目录。'
    fi
    [ -f "$install_dir/docker-compose.yml" ] &&
      [ -f "$install_dir/deploy.sh" ] &&
      [ -f "$install_dir/scripts/main.py" ] || fail "不是有效的 email-agent 安装目录: $install_dir"
  fi
fi

command -v docker >/dev/null 2>&1 || fail '未找到 Docker。'
docker info >/dev/null 2>&1 || fail 'Docker 引擎未运行，请先启动 Docker。'

# 卸载不依赖本地源码或 Compose 插件，仅定位指定项目的资源。
containers=$(docker ps -aq --filter "label=com.docker.compose.project=$project") || fail '无法查询项目容器。'
networks=$(docker network ls -q --filter "label=com.docker.compose.project=$project") || fail '无法查询项目网络。'
volumes=$(docker volume ls -q --filter "label=com.docker.compose.project=$project") || fail '无法查询项目数据卷。'
project_images=$(docker image ls -q --no-trunc --filter "label=com.docker.compose.project=$project") || fail '无法查询项目镜像。'
image_targets='email-agent-inbox:local postgres:16-alpine'
for container in $containers; do
  container_images=$(docker inspect --format '{{.Config.Image}} {{.Image}}' "$container") || fail "无法读取容器镜像: $container"
  image_targets="$image_targets $container_images"
done
image_targets="$image_targets $project_images"

# 兼容没有 Compose 标签的旧数据库卷，但保留明确属于其他项目的卷。
legacy_volume="${project}_postgres_data"
legacy_candidates=$(docker volume ls -q --filter "name=$legacy_volume") || fail '无法查询旧数据库卷。'
for volume in $legacy_candidates; do
  [ "$volume" = "$legacy_volume" ] || continue
  if list_contains "$volume" $volumes; then
    continue
  fi
  owner=$(docker volume inspect --format '{{with .Labels}}{{index . "com.docker.compose.project"}}{{end}}' "$volume") || fail "无法读取数据卷标签: $volume"
  case "$owner" in
    ''|"$project") volumes="$volumes $volume" ;;
    *) printf '保留数据卷（属于其他项目）: %s\n' "$volume" ;;
  esac
done

remove_containers() {
  for container in $containers; do
    if docker rm -f "$container" >/dev/null; then
      printf '已删除容器: %s\n' "$container"
    else
      record_error "未能删除容器: $container"
    fi
  done
}

remove_networks() {
  for network in $networks; do
    if docker network rm "$network" >/dev/null; then
      printf '已删除网络: %s\n' "$network"
    else
      record_error "未能删除网络: $network"
    fi
  done
}

remove_volumes() {
  for volume in $volumes; do
    if docker volume rm "$volume" >/dev/null; then
      printf '已删除数据卷: %s\n' "$volume"
    else
      record_error "未能删除数据卷: $volume"
    fi
  done
}

retain_volumes() {
  found=0
  for volume in $volumes; do
    printf '保留数据卷: %s\n' "$volume"
    found=1
  done
  if [ "$found" -eq 0 ]; then
    printf '%s\n' '未发现项目数据卷。'
  else
    printf '%s\n' '数据卷已保留，重新部署后会继续使用原有数据。'
  fi
}

remove_image_if_unused() {
  image=$1
  inventory=$(docker image ls --all --no-trunc --format '{{.ID}} {{.Repository}}:{{.Tag}}') || fail '无法查询镜像列表。'
  image_id=
  while read -r candidate_id candidate_ref; do
    if [ "$image" = "$candidate_id" ] || [ "$image" = "$candidate_ref" ]; then
      image_id=$candidate_id
      break
    fi
  done <<EOF
$inventory
EOF
  [ -n "$image_id" ] || return 0
  users=$(docker ps -aq --filter "ancestor=$image_id") || fail "无法查询镜像使用情况: $image"
  if [ -n "$users" ]; then
    if ! list_contains "$image_id" $retained_images; then
      printf '保留镜像（仍被其他容器使用）: %s\n' "$image"
      retained_images="$retained_images $image_id"
    fi
    return 0
  fi
  if docker image rm "$image" >/dev/null; then
    printf '已删除镜像: %s\n' "$image"
  else
    record_error "未能删除镜像: $image"
  fi
}

printf '开始卸载 Compose 项目: %s\n' "$project"
remove_containers
remove_networks
if [ "$remove_volumes" -eq 1 ]; then
  remove_volumes
else
  retain_volumes
fi
retained_images=
processed_images=
for image_target in $image_targets; do
  if ! list_contains "$image_target" $processed_images; then
    remove_image_if_unused "$image_target"
    processed_images="$processed_images $image_target"
  fi
done

[ "$errors" -eq 0 ] || fail "有 $errors 项资源删除失败，安装目录已保留；修复上述错误后可重新运行。"

if [ "$remove_dir" -eq 1 ]; then
  if [ -d "$install_dir" ]; then
    rm -rf "$install_dir"
    printf '已删除安装目录: %s\n' "$install_dir"
  else
    printf '安装目录不存在: %s\n' "$install_dir"
  fi
fi

printf '%s\n' '卸载完成。'
