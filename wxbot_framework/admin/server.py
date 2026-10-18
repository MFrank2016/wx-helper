"""管理台本地 HTTP 服务：只绑回环地址，`/api/` 转给注入的 API，其余走静态目录。

传输层只有一条契约：`api.handle(method, path, query, body) -> (状态码, 响应字典)`。
本模块不实现任何业务路由，只负责绑定端口、解析请求、按前缀分派与静态资源读取。

约定：
- `query` 是 `dict[str, str]`（同名参数取最后一个值，空值保留成空串），`body` 是解析后的 JSON 对象；
- 分派给 API 的 `path` 去掉查询串，保留前导 `/`；
- 静态资源有两个根：管理台自身的 `static_dir` 与插件自带的 `plugin_dir/<插件名>/web/`；
  两者都要求解析后仍在根目录内，否则按 404 处理。
"""
from __future__ import annotations

import hmac
import json
import logging
import secrets
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

from ..config import validate_name

LOGGER = logging.getLogger("Admin")

JSON_TYPE = "application/json; charset=utf-8"
API_PREFIX = "/api/"
PLUGIN_PREFIX = "/plugin/"
DEFAULT_PAGE = "index.html"

# 只接受本机回环访问：Host 头是外部域名时拒绝，防 DNS rebinding 让外部页面读到接口。
ALLOWED_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
JSON_METHODS = frozenset({"POST", "PUT", "DELETE", "PATCH"})


def hostname_of(value):
    """从 Host 头里取出主机名，丢掉端口；兼容 `[::1]:8787` 这种 IPv6 写法。"""
    value = (value or "").strip()
    if value.startswith("["):
        end = value.find("]")
        return value[1:end] if end > 0 else value
    # 不带方括号的裸 IPv6（::1）本身就有多个冒号，不能当「主机:端口」拆。
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
    ".woff2": "font/woff2",
}


def content_type(path):
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def resolve_within(root, relative):
    """把 URL 相对路径映射成 root 内的真实文件；越出 root、非文件、路径非法一律返回 None。"""
    root = Path(root)
    if not relative:
        relative = DEFAULT_PAGE
    try:
        candidate = (root / relative).resolve()
        candidate.relative_to(root.resolve())
    except (OSError, ValueError):
        return None
    return candidate if candidate.is_file() else None


class _RequestHandler(BaseHTTPRequestHandler):
    server_version = "WxBotAdmin"
    sys_version = ""
    protocol_version = "HTTP/1.1"
    # keep-alive 下客户端长时间不发下一个请求时，让连接自行超时，避免 stop() 被挂住的线程拖住。
    timeout = 5

    def log_message(self, format, *args):
        # 默认实现直接写 stderr，会把每条请求都打到控制台，这里降到 DEBUG。
        LOGGER.debug("%s %s", self.address_string(), format % args)

    def do_GET(self):
        self._serve("GET")

    def do_HEAD(self):
        self._serve("HEAD")

    def do_POST(self):
        self._serve("POST")

    def _serve(self, method):
        try:
            self._dispatch(method)
        except Exception:
            LOGGER.exception("处理 %s %s 时出错", method, self.path)
            try:
                self._respond_json(500, {"error": "服务内部错误"}, send_body=method != "HEAD")
            except OSError:
                # 客户端先断开（浏览器关标签页等）会导致写入失败，不再往控制台抛栈。
                LOGGER.debug("客户端已断开，错误响应未能写出")

    def _dispatch(self, method):
        request = urlsplit(self.path)
        if hostname_of(self.headers.get("Host", "")) not in ALLOWED_HOSTS:
            self._respond_json(403, {"error": "不接受的主机名"}, send_body=method != "HEAD")
            return
        # 先读掉请求体再判错：没读的 body 留在连接里，会把 keep-alive 的下一个请求顶歪。
        raw_body = self._read_body()
        path = unquote(request.path)
        is_api = path.startswith(API_PREFIX) or path == "/api"
        if is_api and not self._token_ok(request.query):
            self._respond_json(401, {"error": "缺少或错误的管理台令牌"}, send_body=method != "HEAD")
            return
        if is_api and method in JSON_METHODS:
            if not self.headers.get("Content-Type", "").startswith("application/json"):
                self._respond_json(415, {"error": "请求体必须是 application/json"},
                                   send_body=method != "HEAD")
                return
        if is_api:
            body, error = self._parse_json(raw_body)
            if error is not None:
                self._respond_json(400, {"error": error}, send_body=method != "HEAD")
                return
            self._forward_to_api(method, path, request.query, body)
            return
        self._serve_static(method, path)

    def _token_ok(self, raw_query):
        """`/api` 一律要令牌：请求头优先，其次查询参数（插件页面里的 fetch 带不了自定义头）。"""
        given = self.headers.get("X-Admin-Token", "")
        if not given:
            given = parse_qs(raw_query, keep_blank_values=True).get("token", [""])[-1]
        expected = self.server.token
        if not given or not expected:
            return False
        return hmac.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))

    def _read_body(self):
        """按 Content-Length 读掉请求体，避免 keep-alive 下请求串位。"""
        length = self.headers.get("Content-Length")
        if not length:
            return b""
        try:
            size = int(length)
        except ValueError:
            return b""
        return self.rfile.read(size) if size > 0 else b""

    @staticmethod
    def _parse_json(raw):
        if not raw.strip():
            return {}, None
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None, "请求体不是合法的 UTF-8 JSON"
        if not isinstance(parsed, dict):
            return None, "请求体必须是 JSON 对象"
        return parsed, None

    def _forward_to_api(self, method, path, raw_query, body):
        query = {key: values[-1] for key, values in parse_qs(raw_query, keep_blank_values=True).items()}
        try:
            status, payload = self.server.api.handle(method, path, query, body)
        except Exception:
            LOGGER.exception("API 处理 %s %s 失败", method, path)
            self._respond_json(500, {"error": "服务内部错误"}, send_body=method != "HEAD")
            return
        self._respond_json(status, payload, send_body=method != "HEAD")

    def _serve_static(self, method, path):
        target = self._resolve_static(path)
        if target is None:
            self._respond_json(404, {"error": "资源不存在"}, send_body=method != "HEAD")
            return
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type(target))
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if method != "HEAD":
            self.wfile.write(data)

    def _resolve_static(self, path):
        if path.startswith(PLUGIN_PREFIX):
            # /plugin/<插件名>/<插件内路径> 落到 plugin_dir/<插件名>/web/<插件内路径>。
            name, _, rest = path[len(PLUGIN_PREFIX):].partition("/")
            if self.server.plugin_dir is None:
                return None
            try:
                validate_name(name)
            except ValueError:
                return None
            return resolve_within(self.server.plugin_dir / name / "web", rest)
        return resolve_within(self.server.static_dir, path.lstrip("/"))

    def _respond_json(self, status, payload, *, send_body):
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", JSON_TYPE)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        if send_body:
            self.wfile.write(data)


class _AdminHTTPServer(ThreadingHTTPServer):
    # 关停必须是「有界」的：不等待在途请求线程，也不让它们拖住进程退出。
    # 在途请求最长会等插件回调（调试页的 /api/debug 上限 30 秒，还可能等大模型），
    # 若在 stop() 里 join 它们，机器人收到 quit 后会空等几十秒；而关机窗口里的请求
    # 因此可能拿到错误响应（例如统计库已关闭的 503），这是关停时允许的行为。
    daemon_threads = True
    block_on_close = False

    def handle_error(self, request, client_address):
        """客户端在请求中途断开（刷新页面、关标签、取消轮询）不打印 traceback。

        `socketserver` 默认把处理请求时的任何异常整段打到 stderr；但这类
        ConnectionReset/BrokenPipe 是浏览器侧主动断连，不是服务端错误，留着只会
        淹掉真正的故障。其它异常照旧交给默认处理（含完整栈）。
        """
        error = sys.exc_info()[1]
        if isinstance(error, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            LOGGER.debug("管理台客户端断开：%s", client_address)
            return
        super().handle_error(request, client_address)


class AdminServer:
    def __init__(self, api, *, host="127.0.0.1", port=8787, static_dir=None, plugin_dir=None, port_retries=10,
                 token=None):
        self.api = api
        self.host = host
        self.static_dir = Path(static_dir) if static_dir is not None else Path(__file__).parent / "web"
        self.plugin_dir = Path(plugin_dir) if plugin_dir is not None else None
        # 每次启动换一个令牌：不落盘、不进日志，只出现在打印出来的访问地址里。
        self.token = token or secrets.token_urlsafe(24)
        self.port = port
        self.url = None
        self.port_retries = port_retries
        self._requested_port = port
        self._httpd = None
        self._thread = None
        self._lock = threading.Lock()

    def start(self):
        """绑定并开始监听；端口被占用则依次顺延，全部失败抛 OSError。返回实际访问地址。"""
        with self._lock:
            if self._httpd is not None:
                return self.url
            attempts = self.port_retries + 1 if self._requested_port else 1
            last_error = None
            for offset in range(attempts):
                candidate = self._requested_port + offset
                try:
                    httpd = _AdminHTTPServer((self.host, candidate), _RequestHandler)
                except OSError as error:
                    last_error = error
                    continue
                httpd.api = self.api
                httpd.static_dir = self.static_dir
                httpd.plugin_dir = self.plugin_dir
                httpd.token = self.token
                self._httpd = httpd
                self.port = httpd.server_address[1]
                self.url = f"http://{self.host}:{self.port}/?token={quote(self.token, safe='')}"
                self._thread = threading.Thread(target=httpd.serve_forever, name="admin-server", daemon=True)
                self._thread.start()
                # 地址里的 token 是访问凭据，只打印到控制台，不能进日志文件。
                LOGGER.info("管理台已启动：http://%s:%s/", self.host, self.port)
                return self.url
            raise OSError(f"端口 {self._requested_port} 起连续 {attempts} 个端口都被占用") from last_error

    def stop(self):
        """关闭监听并立即返回，不等待在途请求结束；重复调用不报错。

        在途请求线程是 daemon（`daemon_threads=True`、`block_on_close=False`），
        它们最长可能还在等插件回调或大模型，不能拖住关机，所以这里只 join 监听线程本身。
        """
        with self._lock:
            httpd, self._httpd = self._httpd, None
            thread, self._thread = self._thread, None
        if httpd is None:
            return
        httpd.shutdown()
        httpd.server_close()
        if thread is not None:
            thread.join(timeout=5)
        LOGGER.info("管理台已停止：%s", self.url)
