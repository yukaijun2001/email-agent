# 只读收件箱

一个本地运行的邮箱收件箱查看器。普通邮箱通过 IMAP SSL 读取 `INBOX`；Outlook 使用 OAuth2 + SASL XOAUTH2 连接 `outlook.office365.com:993`。新邮件经 WebSocket 推送到浏览器。不会发送、回复、删除、移动邮件，也不会修改已读状态。

## 启动

```bash
./inbox.sh start
```

浏览器打开 <http://127.0.0.1:8000>。首次打开会显示“邮箱管理”。普通 IMAP 邮箱填写服务器、端口、账号和授权码。Outlook 填写邮箱和 Microsoft 应用的 Client ID，然后按页面提示完成设备代码授权；服务使用 access token 执行 IMAP `AUTHENTICATE XOAUTH2`。配置保存在本地 `inbox_config.json`（仅当前用户可读），密码和 refresh_token 不会通过接口返回给网页。令牌轮换后新的 refresh_token 会写回原账号。

### Outlook IMAP OAuth2 准备

1. 在 Outlook 网页设置的“邮件 → 转发和 IMAP”中允许设备和应用使用 IMAP。
2. 在 Microsoft Entra 注册应用；受支持账号类型需包含“个人 Microsoft 账号”。
3. 在“身份验证”中启用“允许公共客户端流”。设备代码流不需要 client secret 或回调地址。
4. 在邮箱管理中点击“添加 Outlook IMAP”，填写邮箱和应用程序（客户端）ID。
5. 点击“开始 Microsoft 授权”，打开显示的设备登录页并输入代码。应用请求 `offline_access` 与 `https://outlook.office.com/IMAP.AccessAsUser.All`。

Outlook 的普通密码和 16 位应用密码不能用于这条链路；服务器明确返回 `LOGINDISABLED` 时必须使用 OAuth2。设备代码和 access token 只保存在进程内存，持久化存储中只保存 refresh token。

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
./deploy.sh repair-db # 修复 PostgreSQL 角色密码，保留数据卷
./deploy.sh build    # 仅构建应用镜像
./deploy.sh help     # 查看命令帮助
```

不带参数时等同于 `./deploy.sh start`。旧命令 `up`、`down` 仍分别兼容为 `start`、`stop`。执行 `stop` 不会删除 PostgreSQL 数据卷；如需彻底删除数据库数据，请单独执行 `docker compose down -v`（或 `docker-compose down -v`）。

可在 `.docker.env` 中修改 `INBOX_PORT`、`POSTGRES_DB` 或 `POSTGRES_USER` 后执行 `./deploy.sh restart`。容器内应用仍监听 8000 端口。每次 `start` / `restart` 会在启动应用前通过 PostgreSQL 容器内的管理员连接同步 `POSTGRES_PASSWORD` 对应角色的密码，不会删除或重建数据卷；也可以单独执行 `./deploy.sh repair-db`。`POSTGRES_USER` 必须与现有数据库卷中已经存在的角色一致。

如果旧版本已经出现 `password authentication failed for user "email_agent"`，更新代码后在项目目录执行：

```bash
./deploy.sh repair-db
./deploy.sh start
```

该修复只更新 PostgreSQL 登录角色密码，不会清空邮箱配置或邮件数据。

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

部署后访问 `http://服务器IP:8000`（或指定端口）。应用会从浏览器实际访问地址自动识别服务器 IP、域名和外部端口，无需手动配置地址白名单；支持 Docker 端口映射及 HTTPS 域名访问，WebSocket 跟随页面地址连接。邮箱及授权码保存在部署服务器的 PostgreSQL 中，“保存在服务器”是说明文字，不是邮箱连接错误。

如果通过 Nginx 等反向代理访问，请保留原始 `Host`（例如 `proxy_set_header Host $http_host;`），并转发 WebSocket 的 `Upgrade` / `Connection` 头。代理改写为容器内部 Host 时，会与浏览器 Origin 不一致而被拒绝。

安装完成后进入安装目录管理服务：

```bash
cd /opt/email-agent
./deploy.sh status
./deploy.sh logs
./deploy.sh stop
./deploy.sh start
```

更新已部署的服务器时，推送修复代码后重新执行上方 `--ref main` 的一键部署命令即可。安装脚本会保留 `.docker.env`、`data/` 和 PostgreSQL 数据卷，并重新构建、启动应用。

## 一键卸载

卸载命令会删除本项目的容器、网络和应用镜像，并保留 PostgreSQL 数据卷，方便卸掉旧容器后重新部署且不丢数据；仍被其他容器使用的镜像会保留。默认也保留 `/opt/email-agent` 安装目录（包括 `.docker.env`、`data/` 等本地文件）：

```bash
curl -fsSL https://raw.githubusercontent.com/yukaijun2001/email-agent/main/uninstall.sh | \
  sudo sh -s -- --yes
```

连同安装目录一起删除（仍保留 PostgreSQL 数据卷）：

```bash
curl -fsSL https://raw.githubusercontent.com/yukaijun2001/email-agent/main/uninstall.sh | \
  sudo sh -s -- --yes --remove-dir
```

只有需要清空数据库时才额外加上 `--remove-volumes`。默认 Compose 项目名为 `email-agent`。如果部署时使用自定义目录或项目名，请通过 `--project 项目名` 指定实际名称；连同自定义目录删除时再加 `--remove-dir --dir /实际安装目录`。脚本无需本地源码或 Compose 插件，不会执行全局 Docker 清理；删除失败会返回非零状态并保留安装目录，可以修复后重新运行。

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

直接用 `inbox.sh` 启动时，服务默认只监听 `127.0.0.1`，保留本机 Host 校验；Docker 部署监听 `0.0.0.0`，自动适配有效的访问 IP、域名和端口。浏览器的添加、修改、删除请求及 WebSocket 连接会校验 `Origin` 与当前访问地址一致。项目本身没有登录鉴权，访问权限应由部署环境或反向代理控制。

## 验证

```bash
python3 -m unittest discover -s tests
```
