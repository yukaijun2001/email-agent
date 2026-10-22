"""Run the local read-only inbox web interface."""

from __future__ import annotations

import json
import re
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import ip_address
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

try:
    from . import config, realtime
    from .imap_reader import InboxReader, MailboxError
except ImportError:  # Running scripts/main.py directly.
    import config
    import realtime
    from imap_reader import InboxReader, MailboxError


STATIC_DIR = config.ROOT_DIR / "static"
ACCOUNT_PATH = re.compile(r"/api/accounts/([0-9a-f]{8})")
MESSAGE_PATH = re.compile(r"/api/accounts/([0-9a-f]{8})/emails/([A-Za-z0-9_-]{1,800})")
MESSAGE_HTML_PATH = re.compile(r"/api/accounts/([0-9a-f]{8})/emails/([A-Za-z0-9_-]{1,800})/html")
PAGE_CSP = (
    "default-src 'self'; img-src 'self' data:; style-src 'self'; frame-src 'self'; "
    "script-src 'self'; connect-src 'self'; base-uri 'none'; "
    "frame-ancestors 'none'; form-action 'none'"
)
# Mail documents: load every resource the mail references (images, styles,
# fonts, media, frames), but never run scripts or submit forms, and only
# inside this app.
MAIL_CSP = (
    "default-src * data: blob: 'unsafe-inline'; script-src 'none'; object-src 'none'; "
    "form-action 'none'; frame-ancestors 'self'"
)


def _parse_authority(value: str) -> tuple[str, int | None] | None:
    """Parse a Host authority without accepting URLs or header/CSP delimiters."""
    if not re.fullmatch(r"(?:[a-z0-9.-]+|\[[0-9a-f:.]+\])(?::[0-9]{1,5})?", value):
        return None
    try:
        parsed = urlsplit(f"//{value}")
        hostname, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if not hostname or port == 0:
        return None
    if ":" not in hostname:
        labels = hostname.rstrip(".").split(".")
        if len(hostname) > 254 or any(
            not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) for label in labels
        ):
            return None
    return hostname, port


class InboxHandler(BaseHTTPRequestHandler):
    reader = InboxReader()
    hub = realtime.Hub()
    watcher: realtime.MailWatcher | None = None
    server_version = "InboxReader/1.0"
    sys_version = ""

    def _respond(self, status: int, body: bytes, content_type: str, csp: str | None = None) -> None:
        if csp is None:
            csp = PAGE_CSP
            authority = self._request_authority()
            if authority:
                # Browser 'self' handling for WebSockets varies. Authorize the
                # actual public address, including a Docker-mapped port or TLS proxy.
                csp = csp.replace(
                    "connect-src 'self';",
                    f"connect-src 'self' ws://{authority} wss://{authority};",
                )
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", csp)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, value: dict | list) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self._respond(status, body, "application/json; charset=utf-8")

    def _request_json(self, max_size: int = 64 * 1024) -> dict:
        try:
            size = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("请求内容无效。") from exc
        content_type = self.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type != "application/json":
            raise ValueError("请求内容必须是 JSON。")
        if size < 1 or size > max_size:
            raise ValueError("请求内容无效。")
        try:
            payload = json.loads(self.rfile.read(size).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("请求内容无效。") from exc
        if not isinstance(payload, dict):
            raise ValueError("请求内容无效。")
        return payload

    def _static(self, name: str) -> None:
        files = {"/": ("index.html", "text/html; charset=utf-8")}
        if name == "/styles.css":
            files[name] = ("styles.css", "text/css; charset=utf-8")
        elif name == "/app.js":
            files[name] = ("app.js", "text/javascript; charset=utf-8")
        entry = files.get(name)
        if not entry:
            self._json(404, {"error": "Not found"})
            return
        filename, content_type = entry
        try:
            body = (STATIC_DIR / filename).read_bytes()
        except OSError:
            self._json(500, {"error": "Web assets are unavailable."})
            return
        self._respond(200, body, content_type)

    # ------------------------------------------------------------------ guards
    def _request_authority(self) -> str | None:
        hosts = self.headers.get_all("Host", [])
        if len(hosts) != 1:
            return None
        authority = hosts[0].strip().lower()
        return authority if _parse_authority(authority) else None

    def _host_ok(self) -> bool:
        """Keep local binding restricted; discover public addresses from Host."""
        authority = self._request_authority()
        if not authority:
            return False
        hostname, port = _parse_authority(authority)
        try:
            local_binding = ip_address(config.APP_HOST).is_loopback
        except ValueError:
            local_binding = config.APP_HOST.lower() == "localhost"
        if not local_binding:
            # The container cannot discover a NAT address or an external proxy
            # port from its interfaces. Use the address the browser actually used.
            return True
        return hostname in {"localhost", "127.0.0.1", "::1", config.APP_HOST.lower()} and (
            port if port is not None else 80
        ) == config.APP_PORT

    def _origin_ok(self) -> bool:
        """Match the browser origin to Host, including default HTTP(S) ports."""
        authority = self._request_authority()
        origins = self.headers.get_all("Origin", [])
        if not authority or len(origins) != 1:
            return False
        try:
            origin = urlsplit(origins[0].strip().lower())
        except ValueError:
            return False
        if origin.scheme not in {"http", "https"} or origin.path or origin.query or origin.fragment:
            return False
        origin_host = _parse_authority(origin.netloc)
        if not origin_host:
            return False
        request_host = _parse_authority(authority)
        default_port = 443 if origin.scheme == "https" else 80
        return (origin_host[0], origin_host[1] or default_port) == (
            request_host[0], request_host[1] or default_port,
        )

    def _request_allowed(self, *, write: bool = False) -> bool:
        if not self._host_ok():
            self._json(403, {"error": "访问地址未被允许，请使用部署服务器的有效 IP 或域名访问。"})
            return False
        # CLI clients may omit Origin. Browser writes must come from this app;
        # never trust client-supplied X-Forwarded-Host to bypass this comparison.
        if write and (
            ("Origin" in self.headers and not self._origin_ok())
            or self.headers.get("Sec-Fetch-Site", "").lower() == "cross-site"
        ):
            self._json(403, {"error": "请求来源与当前访问地址不一致，请从同一地址重新打开页面。"})
            return False
        return True

    def _notify_accounts_changed(self) -> None:
        self.hub.broadcast({"type": "accounts_changed"})
        if self.watcher:
            self.watcher.wake()

    # ------------------------------------------------------------------ websocket
    def _websocket(self) -> None:
        upgrade = self.headers.get("Upgrade", "").lower()
        connection_tokens = {t.strip().lower() for t in self.headers.get("Connection", "").split(",")}
        key = self.headers.get("Sec-WebSocket-Key")
        if upgrade != "websocket" or "upgrade" not in connection_tokens:
            self._json(426, {"error": "需要 WebSocket 升级请求。"})
            return
        if self.headers.get("Sec-WebSocket-Version", "") != "13" or not realtime.valid_client_key(key):
            self.send_response(400)
            self.send_header("Sec-WebSocket-Version", "13")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if not self._origin_ok():
            self._json(403, {"error": "实时连接来源与当前访问地址不一致。"})
            return
        self.send_response(101, "Switching Protocols")
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", realtime.accept_key(key.strip()))
        self.end_headers()
        self.wfile.flush()
        self.close_connection = True
        client = realtime.Client(self.wfile, self.address_string())
        realtime.serve_client(self.hub, client, self.rfile)

    # ------------------------------------------------------------------ GET
    def do_GET(self) -> None:
        request = urlsplit(self.path)
        if request.path in {"/", "/index.html", "/styles.css", "/app.js"}:
            self._static("/" if request.path == "/index.html" else request.path)
            return
        if not self._request_allowed():
            return
        if request.path == "/ws":
            self._websocket()
            return
        if request.path in {"/api/status", "/api/config", "/api/accounts"}:
            self._json(200, config.Config.snapshot())
            return
        if request.path == "/api/emails":
            self._list_emails(request.query)
            return
        match = MESSAGE_PATH.fullmatch(request.path)
        if match:
            self._get_message(match.group(1), match.group(2))
            return
        match = MESSAGE_HTML_PATH.fullmatch(request.path)
        if match:
            self._get_message_html(match.group(1), match.group(2))
            return
        self._json(404, {"error": "Not found"})

    def _list_emails(self, query: str) -> None:
        params = parse_qs(query, keep_blank_values=False)
        try:
            limit = int(params.get("limit", ["50"])[0])
            if not 1 <= limit <= 100:
                raise ValueError
        except ValueError:
            self._json(400, {"error": "邮件数量必须是 1 到 100 之间的数字。"})
            return
        selected = params.get("account", ["all"])[0]
        known = config.Config.account_ids()
        if selected == "all":
            account_ids = known
        elif selected in known:
            account_ids = [selected]
        else:
            self._json(404, {"error": "邮箱不存在或已被删除。"})
            return
        if not account_ids:
            self._json(503, {"error": "请先在浏览器中完成邮箱连接设置。"})
            return
        try:
            messages, errors = self.reader.list_all(account_ids, limit)
        except Exception as exc:
            self.log_error("Inbox read failed: %s", exc)
            self._json(502, {"error": "读取收件箱失败，请检查 IMAP 配置与邮箱服务状态。"})
            return
        if errors and len(errors) == len(account_ids):
            # Every selected mailbox failed: surface it as an error response.
            message = errors[0]["error"] if len(errors) == 1 else "所有邮箱均读取失败，请检查连接设置。"
            self._json(503, {"error": message, "errors": errors})
            return
        self._json(200, {"emails": messages, "errors": errors})

    def _get_message(self, account_id: str, uid: str) -> None:
        try:
            message = self.reader.get_message(account_id, uid)
        except MailboxError as exc:
            self._json(503, {"error": str(exc)})
        except Exception as exc:
            self.log_error("Message read failed: %s", exc)
            self._json(502, {"error": "读取邮件失败，请检查 IMAP 配置与邮箱服务状态。"})
        else:
            self._json(200, message) if message else self._json(404, {"error": "邮件不存在。"})

    def _get_message_html(self, account_id: str, uid: str) -> None:
        """Serve the rendered message as its own document for a sandboxed iframe."""
        try:
            document = self.reader.get_message_html(account_id, uid)
        except MailboxError as exc:
            document, status = None, 503
            self.log_error("Message HTML read failed: %s", exc)
        except Exception as exc:
            document, status = None, 502
            self.log_error("Message HTML read failed: %s", exc)
        else:
            status = 200 if document else 404
        if document is None:
            notice = "无法读取邮件内容。" if status != 404 else "邮件不存在。"
            document = f'<!doctype html><meta charset="utf-8"><p style="color:#758078;font:13px sans-serif">{notice}</p>'
        self._respond(status, document.encode("utf-8"), "text/html; charset=utf-8", MAIL_CSP)

    # ------------------------------------------------------------------ writes
    def _create_account(self, payload: dict) -> dict:
        protocol = str(payload.get("protocol") or "imap").strip().lower()
        if protocol == "graph":
            return config.Config.add_graph_account(
                payload.get("account", ""),
                payload.get("password"),
                payload.get("client_id", ""),
                payload.get("refresh_token", ""),
            )
        if protocol == "outlook_imap":
            return config.Config.add_outlook_account(
                payload.get("account", ""),
                payload.get("client_id", ""),
                payload.get("refresh_token", ""),
            )
        return config.Config.add_account(
            payload.get("imap_server", ""),
            payload.get("imap_port", 993),
            payload.get("account", ""),
            payload.get("password"),
        )

    def _update_account(self, account_id: str, payload: dict) -> dict:
        return config.Config.update_account(
            account_id,
            payload.get("imap_server", ""),
            payload.get("imap_port", 993),
            payload.get("account", ""),
            payload.get("password"),
            payload.get("protocol"),
            payload.get("client_id") if "client_id" in payload else None,
            payload.get("refresh_token") if "refresh_token" in payload else None,
        )

    def _import_graph_accounts(self) -> None:
        try:
            result = config.Config.import_graph_accounts(str(self._request_json(512 * 1024).get("text") or ""))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except OSError:
            self._json(500, {"error": "无法保存邮箱设置。"})
            return
        if not result["added"]:
            message = result["errors"][0] if len(result["errors"]) == 1 else "没有导入成功的邮箱。"
            self._json(400, {"error": message, "errors": result["errors"]})
            return
        self._json(201, result)
        self._notify_accounts_changed()

    def _start_outlook_auth(self) -> None:
        try:
            payload = self._request_json()
            result = self.reader.outlook_oauth().start_device_flow(
                payload.get("account", ""),
                payload.get("client_id", ""),
                payload.get("account_id") or None,
            )
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except MailboxError as exc:
            self._json(503, {"error": str(exc)})
        else:
            self._json(200, result)

    def _outlook_import(self, *, preview: bool = False) -> None:
        try:
            text = str(self._request_json(512 * 1024).get("text") or "")
            result = (config.Config.preview_outlook_accounts(text) if preview
                      else config.Config.import_outlook_accounts(text))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
            return
        except OSError:
            self._json(500, {"error": "无法保存邮箱设置。"})
            return
        self._json(200 if preview or not result["added"] else 201, result)
        if not preview and result["added"]:
            self._notify_accounts_changed()

    def _poll_outlook_auth(self) -> None:
        try:
            payload = self._request_json()
            result = self.reader.outlook_oauth().poll_device_flow(payload.get("flow_id", ""))
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except MailboxError as exc:
            self._json(503, {"error": str(exc)})
        else:
            self._json(200, result)
            if result.get("status") == "complete":
                self._notify_accounts_changed()

    def do_POST(self) -> None:
        request = urlsplit(self.path)
        if not self._request_allowed(write=True):
            return
        if request.path == "/api/accounts/import":
            self._import_graph_accounts()
            return
        if request.path in {"/api/outlook/preview", "/api/outlook/import"}:
            self._outlook_import(preview=request.path.endswith("/preview"))
            return
        if request.path == "/api/outlook/device/start":
            self._start_outlook_auth()
            return
        if request.path == "/api/outlook/device/poll":
            self._poll_outlook_auth()
            return
        if request.path != "/api/accounts":
            self._json(404, {"error": "Not found"})
            return
        try:
            created = self._create_account(self._request_json())
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except OSError:
            self._json(500, {"error": "无法保存邮箱设置。"})
        else:
            self._json(201, created)
            self._notify_accounts_changed()

    def do_PUT(self) -> None:
        request = urlsplit(self.path)
        if not self._request_allowed(write=True):
            return
        match = ACCOUNT_PATH.fullmatch(request.path)
        if not match:
            self._json(404, {"error": "Not found"})
            return
        try:
            updated = self._update_account(match.group(1), self._request_json())
        except config.AccountNotFound:
            self._json(404, {"error": "邮箱不存在或已被删除。"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except OSError:
            self._json(500, {"error": "无法保存邮箱设置。"})
        else:
            self._json(200, updated)
            self._notify_accounts_changed()

    def do_DELETE(self) -> None:
        request = urlsplit(self.path)
        if not self._request_allowed(write=True):
            return
        match = ACCOUNT_PATH.fullmatch(request.path)
        if not match:
            self._json(404, {"error": "Not found"})
            return
        try:
            config.Config.remove_account(match.group(1))
        except config.AccountNotFound:
            self._json(404, {"error": "邮箱不存在或已被删除。"})
        except OSError:
            self._json(500, {"error": "无法保存邮箱设置。"})
        else:
            self._json(200, config.Config.snapshot())
            self._notify_accounts_changed()

    def log_message(self, format: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def main() -> None:
    config.Config.initialize()
    server = ThreadingHTTPServer((config.APP_HOST, config.APP_PORT), InboxHandler)
    server.daemon_threads = True
    watcher = realtime.MailWatcher(InboxHandler.hub, InboxHandler.reader)
    InboxHandler.watcher = watcher
    InboxHandler.hub.on_first_client = watcher.wake
    watcher.start()
    print("只读收件箱已启动: http://%s:%s" % (config.APP_HOST, config.APP_PORT))
    print("WebSocket 推送: ws://%s:%s/ws（每 %s 秒检查新邮件）" % (config.APP_HOST, config.APP_PORT, config.POLL_INTERVAL_SECONDS))
    print("IMAP 与 Outlook Graph 均只读取 INBOX，按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        watcher.stop()
        server.server_close()
        InboxHandler.reader.close()


if __name__ == "__main__":
    main()
