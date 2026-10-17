#!/bin/sh

set -eu

usage() {
  cat <<EOF
用法:
  curl -fsSL https://raw.githubusercontent.com/yukaijun2001/email-agent/main/install.sh | \\
    sh -s -- --repo yukaijun2001/email-agent [选项]

选项:
  --repo REPO       GitHub 仓库，格式为 owner/name（必填）
  --ref REF         分支、标签或提交引用（默认: main）
  --dir DIRECTORY   安装目录（默认: /opt/email-agent）
  --port PORT       对外 HTTP 端口（默认保留已有配置，否则为 8000）
  --help            显示此帮助

示例:
  curl -fsSL https://raw.githubusercontent.com/yukaijun2001/email-agent/main/install.sh | \\
    sh -s -- --repo yukaijun2001/email-agent --ref v1.0.0 --dir /opt/email-agent --port 8080
EOF
}

fail() {
  printf '错误: %s\n' "$1" >&2
  exit 1
}

require_command() {
  command -v "$1" >/dev/null 2>&1 || fail "未找到 $1，请先安装。"
}

repo=
ref=main
install_dir=/opt/email-agent
port=

while [ "$#" -gt 0 ]; do
  case "$1" in
    --repo)
      [ "$#" -ge 2 ] || fail "--repo 缺少参数。"
      repo=$2
      shift 2
      ;;
    --ref)
      [ "$#" -ge 2 ] || fail "--ref 缺少参数。"
      ref=$2
      shift 2
      ;;
    --dir)
      [ "$#" -ge 2 ] || fail "--dir 缺少参数。"
      install_dir=$2
      shift 2
      ;;
    --port)
      [ "$#" -ge 2 ] || fail "--port 缺少参数。"
      port=$2
      shift 2
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

[ -n "$repo" ] || { usage >&2; fail "必须提供 --repo owner/name。"; }
repo_owner=${repo%%/*}
repo_name=${repo#*/}
[ "$repo_owner" != "$repo" ] || fail "--repo 必须是 owner/name 格式。"
[ -n "$repo_owner" ] && [ -n "$repo_name" ] || fail "--repo 必须是 owner/name 格式。"
[ "$repo_name" = "${repo_name#*/}" ] || fail "--repo 必须是 owner/name 格式。"
case "$repo_owner$repo_name" in
  *[!A-Za-z0-9_.-]*) fail "--repo 只能包含 GitHub owner/name 字符。" ;;
esac
case "$ref" in
  ''|*[!A-Za-z0-9._/-]*) fail "--ref 只能包含字母、数字、点、下划线、短横线和斜杠。" ;;
esac

if [ -n "$port" ]; then
  case "$port" in
    ''|*[!0-9]*) fail "--port 必须是 1-65535 的数字。" ;;
  esac
  [ "$port" -ge 1 ] 2>/dev/null && [ "$port" -le 65535 ] 2>/dev/null || fail "--port 必须是 1-65535 的数字。"
fi

case "$install_dir" in
  /*) ;;
  *) install_dir=$(pwd)/$install_dir ;;
esac

require_command curl
require_command tar
require_command docker

if ! docker info >/dev/null 2>&1; then
  fail 'Docker 引擎未运行，请先启动 Docker Desktop 或 Docker Engine。'
fi

case "$install_dir" in
  /|/.*) ;;
  *) mkdir -p "$(dirname "$install_dir")" || fail "无法创建安装目录的父目录。" ;;
esac

work_dir=$(mktemp -d "${TMPDIR:-/tmp}/email-agent-install.XXXXXX") || fail '无法创建临时目录。'
cleanup() {
  rm -rf "$work_dir"
}
trap cleanup EXIT HUP INT TERM

archive="$work_dir/source.tar.gz"
extract_dir="$work_dir/extracted"
mkdir -p "$extract_dir"
archive_url="https://codeload.github.com/$repo/tar.gz/$ref"
printf '正在下载 %s (%s) …\n' "$repo" "$ref"
curl --fail --location --silent --show-error --retry 3 --connect-timeout 15 --proto '=https' --tlsv1.2 \
  "$archive_url" -o "$archive" || fail 'GitHub 源码下载失败，请检查仓库、引用和网络连接。'

archive_root=$(tar -tzf "$archive" | awk -F/ 'NF { print $1; exit }')
[ -n "$archive_root" ] || fail '下载的源码压缩包为空。'
tar -xzf "$archive" -C "$extract_dir" || fail '源码压缩包解压失败。'
release_dir="$extract_dir/$archive_root"
[ -f "$release_dir/docker-compose.yml" ] || fail '下载内容不是有效的 email-agent 项目。'
[ -f "$release_dir/deploy.sh" ] || fail '下载内容缺少 deploy.sh。'

# GitHub 源码包不应携带这些运行时文件；即使仓库后来误提交，也不能覆盖服务器数据。
rm -rf "$release_dir/data" "$release_dir/.runtime" "$release_dir/.docker.env"
rm -f "$release_dir/inbox_config.json"

mkdir -p "$install_dir" || fail "无法创建安装目录: $install_dir"
cp -R "$release_dir"/. "$install_dir"/ || fail '无法更新安装目录。'
chmod +x "$install_dir/deploy.sh"

if [ -n "$port" ]; then
  if [ -f "$install_dir/.docker.env" ]; then
    env_tmp="$work_dir/docker.env"
    awk -v value="$port" '
      BEGIN { found = 0 }
      /^INBOX_PORT=/ {
        if (!found) { print "INBOX_PORT=" value; found = 1 }
        next
      }
      { print }
      END { if (!found) print "INBOX_PORT=" value }
    ' "$install_dir/.docker.env" >"$env_tmp"
    chmod 600 "$env_tmp"
    mv "$env_tmp" "$install_dir/.docker.env"
  else
    export INBOX_PORT=$port
  fi
fi

cd "$install_dir"
printf '正在构建并启动 Docker 服务…\n'
./deploy.sh start
printf '\n安装完成，项目目录: %s\n' "$install_dir"
