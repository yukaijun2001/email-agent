# 只读收件箱

一个本地运行的邮箱收件箱查看器。通过 IMAP SSL 同时读取多个邮箱的 `INBOX`，并通过 WebSocket 把新邮件实时推送到浏览器。不会发送、回复、删除、移动邮件，也不会修改已读状态（所有读取都使用 `BODY.PEEK` 和只读 `SELECT`）。

## 启动

```bash
./inbox.sh start
```

浏览器打开 <http://127.0.0.1:8000>。首次打开会显示“邮箱管理”，填写 IMAP 服务器、端口、邮箱账号和授权码/应用密码后点击“保存并连接”。配置保存在项目目录的本地 `inbox_config.json`（仅当前用户可读），授权码不会通过任何接口返回给网页。

管理服务：

```bash
./inbox.sh status   # 查看运行状态
./inbox.sh stop     # 停止服务
./inbox.sh restart  # 重启服务
```

服务在后台运行，PID 文件和日志分别保存在 `.runtime/inbox.pid`、`.runtime/inbox.log`。停止服务不会删除邮箱配置。

说明：直接使用 `inbox.sh` 启动时保留旧版 JSON 配置作为本地开发兼容模式；Docker 部署完全使用 PostgreSQL，不使用 SQLite。

## Docker 部署

需要 Docker Engine 或 Docker Desktop，以及 `docker compose`（或独立的 `docker-compose`）。在项目目录执行：

```bash
./deploy.sh start
```

脚本会自动构建应用镜像，并以两个容器启动应用和 PostgreSQL。首次运行会生成仅当前用户可读的 `.docker.env`，其中包含数据库连接参数；如果项目根目录已有 `inbox_config.json`，首次部署会导入 PostgreSQL。数据库数据保存在 Docker volume `email-agent_postgres_data` 中，应用容器重建不会丢失。

浏览器打开 <http://127.0.0.1:8000>。常用管理命令：

```bash
./deploy.sh start    # 构建并后台启动应用和 PostgreSQL
./deploy.sh stop     # 停止并移除容器，保留 PostgreSQL 数据卷
./deploy.sh status   # 查看容器状态
./deploy.sh logs     # 查看应用实时日志，Ctrl+C 退出
./deploy.sh restart  # 重新构建并启动
./deploy.sh build    # 仅构建应用镜像
./deploy.sh help     # 查看命令帮助
```

不带参数时等同于 `./deploy.sh start`。旧命令 `up`、`down` 仍分别兼容为 `start`、`stop`。执行 `stop` 不会删除 PostgreSQL 数据卷；如需彻底删除数据库数据，请单独执行 `docker compose down -v`（或 `docker-compose down -v`）。

可在 `.docker.env` 中修改 `INBOX_PORT`、`POSTGRES_DB` 或 `POSTGRES_USER` 后执行 `./deploy.sh restart`。容器内应用仍监听 8000 端口；`POSTGRES_PASSWORD` 修改后只有新建数据库 volume 时才会生效。

应用容器通过 `INBOX_DATABASE_URL` 连接 PostgreSQL。邮箱账号和授权码存储在 PostgreSQL 的 `mail_accounts` 表中，前端接口不会返回授权码。

## 服务器一键部署

项目上传到 GitHub 后，可以在安装了 Docker 的服务器上直接用 `curl` 下载源码压缩包并部署，不需要执行 `git clone`。当前仓库为 `yukaijun2001/email-agent`：

```bash
curl -fsSL https://raw.githubusercontent.com/yukaijun2001/email-agent/main/install.sh | \
  sudo sh -s -- --repo yukaijun2001/email-agent --ref main
```

安装脚本会：

1. 从 GitHub 下载指定分支、标签或提交的 tarball。
2. 解压到安装目录，保留已有 `.docker.env`、`data/` 和 PostgreSQL 数据卷。
3. 自动构建应用镜像并启动 PostgreSQL 与应用容器。

默认安装目录为 `/opt/email-agent`。也可以指定服务器端口、固定发布标签或自定义目录：

```bash
curl -fsSL https://raw.githubusercontent.com/yukaijun2001/email-agent/main/install.sh | \
  sudo sh -s -- --repo yukaijun2001/email-agent --ref v1.0.0 --dir /opt/email-agent --port 8080
```

安装完成后进入安装目录管理服务：

```bash
cd /opt/email-agent
./deploy.sh status
./deploy.sh logs
./deploy.sh stop
./deploy.sh start
```

## 一键卸载

卸载命令会删除本项目的容器、网络、应用镜像和 PostgreSQL 数据卷；仍被其他容器使用的镜像会保留。默认保留 `/opt/email-agent` 安装目录（包括 `.docker.env`、`data/` 等本地文件）：

```bash
curl -fsSL https://raw.githubusercontent.com/yukaijun2001/email-agent/main/uninstall.sh | \
  sudo sh -s -- --yes
```

连同安装目录一起删除：

```bash
curl -fsSL https://raw.githubusercontent.com/yukaijun2001/email-agent/main/uninstall.sh | \
  sudo sh -s -- --yes --remove-dir
```

默认 Compose 项目名为 `email-agent`。如果部署时使用自定义目录或项目名，请通过 `--project 项目名` 指定实际名称；连同自定义目录删除时再加 `--remove-dir --dir /实际安装目录`。脚本无需本地源码或 Compose 插件，不会执行全局 Docker 清理；删除失败会返回非零状态并保留安装目录，可以修复后重新运行。

## 多邮箱

点击右上角“邮箱管理”可以添加、编辑、删除邮箱，数量不限。修改立即生效，不需要重启服务。

收件箱默认合并显示所有邮箱最近 50 封邮件，按日期排序，每封邮件标注所属邮箱地址；左上角下拉框可以只看某一个邮箱。多个邮箱并行读取，先读完的邮箱先显示；某个邮箱连接失败时只在页面顶部提示该邮箱的错误，其他邮箱照常显示。

## 正文显示

所有邮件都以富文本完整显示，不提供纯文本模式：

- HTML 邮件按原样渲染，远程图片、外部样式表、网页字体、背景图、视频和嵌入内容都会加载。
- 邮件内嵌图片（`cid:`）直接显示；作为附件发送的图片显示在正文末尾，其他附件列出文件名和大小（只读模式不提供下载）。
- 只有纯文本版本的邮件也以富文本显示，保留换行，网址可点击。
- 转发的邮件（`message/rfc822`）会连同原邮件的发件人、主题一起显示在正文中。

唯一的处理是去掉会“运行”的内容：`<script>`、`on*` 事件属性、`javascript:` 链接等（`scripts/html_sanitizer.py`）。正文在不允许脚本的 sandbox iframe 中显示，并带有 `script-src 'none'` 的 CSP，链接在新标签页打开。这样邮件无法操作本机服务或读取其他邮箱的数据。

## 读取速度

服务会为每个邮箱保留已登录的 IMAP 连接（空闲 4 分钟后关闭），后续同步、打开邮件和新邮件检查都不需要重新建立 TLS 连接和登录。列表只抓取最新 50 封邮件的标题，不再下载整个邮箱的 UID 列表；没有新邮件时，每次检查只需一次往返。最近打开的 100 封邮件正文会缓存在内存中，再次打开时立即显示。修改邮箱设置后旧连接会自动作废。

旧版单邮箱配置文件会在首次启动时自动迁移为新格式，无需重新填写。

## 实时推送

页面打开后会连接 `ws://127.0.0.1:8000/ws`。只要有浏览器连着，后台线程每 30 秒（`scripts/config.py` 中的 `POLL_INTERVAL_SECONDS`）检查一次各邮箱，发现 UID 更大的新邮件就推送给所有打开的页面：新邮件会出现在列表顶部并高亮，页面顶部显示提示。没有浏览器连接时不会轮询 IMAP。断线后页面会自动重连。

推送事件格式（JSON 文本帧）：

```json
{"type": "new_emails", "account_id": "1a2b3c4d", "emails": [{"uid": "42", "from": "...", "subject": "...", "date": "...", "timestamp": 1760000000}]}
{"type": "account_status", "account_id": "1a2b3c4d", "ok": false, "error": "..."}
{"type": "accounts_changed"}
```

## HTTP 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/accounts` | 邮箱列表（不含授权码） |
| POST | `/api/accounts` | 添加邮箱 |
| PUT | `/api/accounts/{id}` | 修改邮箱，`password` 留空则保留原授权码 |
| DELETE | `/api/accounts/{id}` | 删除邮箱 |
| GET | `/api/emails?account=all\|{id}&limit=50` | 合并或单个邮箱的邮件列表，附带各邮箱的 `errors` |
| GET | `/api/accounts/{id}/emails/{uid}` | 邮件信息（发件人、收件人、抄送、主题、日期） |
| GET | `/api/accounts/{id}/emails/{uid}/html` | 完整的富文本正文（独立 HTML 文档，供 iframe 显示） |

服务只监听 `127.0.0.1`，没有登录鉴权：任何能访问本机的进程都能调用这些接口。接口会拒绝 `Host` 不是本机地址的请求（防 DNS 重绑定），WebSocket 只接受本页面来源（`Origin`）的连接。请不要把端口转发或暴露到局域网/公网。

## 验证

```bash
python3 -m unittest discover -s tests
```
