"""管理台本地 HTTP 服务：绑定与端口顺延、/api 转发、静态资源与穿越防护。

用例一律用动态端口（port=0 或先取一个空闲端口），避免本机 8787 被其它程序占用造成随机失败。
"""
import http.client
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from wxbot_framework.admin.server import AdminServer

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"


class FakeApi:
    """被注入的假 API：记录收到的请求，返回固定响应，用于验证传输层契约。"""

    def __init__(self):
        self.calls = []
        self.response = (200, {"ok": True})

    def handle(self, method, path, query, body):
        self.calls.append({"method": method, "path": path, "query": query, "body": body})
        return self.response


def raw_request(port, method, path, body=None, headers=None):
    """按原始请求行发送，避免客户端把 ../ 这类路径段规范化掉。"""
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        return response.status, response.getheaders(), response.read()
    finally:
        connection.close()


class AdminServerTests(unittest.TestCase):
    def setUp(self):
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.static = self.root / "web"
        self.static.mkdir()
        self.index = "<h1>管理台</h1>"
        (self.static / "index.html").write_text(self.index, encoding="utf-8")
        self.api = FakeApi()
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.stop()
        self.temporary.cleanup()

    def start(self, **kwargs):
        server = AdminServer(self.api, port=0, static_dir=self.static, **kwargs)
        self.servers.append(server)
        server.start()
        return server

    def free_port(self):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", 0))
            return probe.getsockname()[1]
        finally:
            probe.close()

    def port_is_free(self, port):
        probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            probe.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False
        finally:
            probe.close()

    def occupy_port_with_free_next(self):
        """占用一个端口并保证下一个端口空闲；不设 SO_REUSEADDR，否则 Windows 上允许重复绑定。"""
        for _ in range(30):
            occupant = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            occupant.bind(("127.0.0.1", 0))
            occupant.listen(1)
            port = occupant.getsockname()[1]
            if self.port_is_free(port + 1):
                return occupant, port
            occupant.close()
        self.fail("找不到相邻两个空闲端口")

    def assert_404(self, port, path):
        status, _, body = raw_request(port, "GET", path)
        self.assertEqual(status, 404, f"{path} 应返回 404")
        self.assertEqual(json.loads(body.decode("utf-8"))["error"], "资源不存在")

    def test_start_returns_loopback_url_on_requested_port(self):
        port = self.free_port()
        server = AdminServer(self.api, port=port, static_dir=self.static)
        self.servers.append(server)
        url = server.start()
        self.assertEqual(server.port, port)
        self.assertEqual(url, f"http://127.0.0.1:{port}/")
        self.assertEqual(server.url, url)

    def test_occupied_port_falls_through_to_next(self):
        occupant, port = self.occupy_port_with_free_next()
        self.addCleanup(occupant.close)
        server = AdminServer(self.api, port=port, static_dir=self.static)
        self.servers.append(server)
        url = server.start()
        self.assertEqual(server.port, port + 1)
        self.assertEqual(url, f"http://127.0.0.1:{port + 1}/")

    def test_get_static_file_is_served_with_utf8(self):
        server = self.start()
        status, headers, body = raw_request(server.port, "GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(dict(headers)["Content-Type"], "text/html; charset=utf-8")
        self.assertEqual(body.decode("utf-8"), self.index)

    def test_unknown_path_returns_404_json(self):
        server = self.start()
        self.assert_404(server.port, "/nope.html")
        self.assert_404(server.port, "/assets/%E9%9D%A2%E6%9D%BF.html")

    def test_post_json_body_is_passed_to_api(self):
        server = self.start()
        payload = {"plugin": "echo", "text": "你好"}
        status, _, body = raw_request(
            server.port, "POST", "/api/debug?dry=1",
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body.decode("utf-8")), {"ok": True})
        self.assertEqual(self.api.calls, [{
            "method": "POST",
            "path": "/api/debug",
            "query": {"dry": "1"},
            "body": payload,
        }])

    def test_bad_json_body_returns_400(self):
        server = self.start()
        status, _, body = raw_request(
            server.port, "POST", "/api/debug",
            body=b"{not json",
            headers={"Content-Type": "application/json"},
        )
        self.assertEqual(status, 400)
        self.assertIn("error", json.loads(body.decode("utf-8")))
        self.assertEqual(self.api.calls, [])

    def test_static_path_traversal_is_rejected(self):
        (self.root / "bot_config.json").write_text('{"secret": "不该被读到"}', encoding="utf-8")
        server = self.start()
        self.assert_404(server.port, "/../bot_config.json")
        self.assert_404(server.port, "/%2e%2e/bot_config.json")
        self.assert_404(server.port, "/assets/../../bot_config.json")

    def test_plugin_static_files_are_served_from_plugin_dir(self):
        plugin_dir = self.root / "plugins"
        panel = plugin_dir / "echo" / "web" / "panel.html"
        panel.parent.mkdir(parents=True)
        panel.write_text("<p>回显面板</p>", encoding="utf-8")
        server = self.start(plugin_dir=plugin_dir)
        status, _, body = raw_request(server.port, "GET", "/plugin/echo/panel.html")
        self.assertEqual(status, 200)
        self.assertEqual(body.decode("utf-8"), "<p>回显面板</p>")

    def test_plugin_static_rejects_bad_plugin_name_and_traversal(self):
        plugin_dir = self.root / "plugins"
        panel = plugin_dir / "echo" / "web" / "panel.html"
        panel.parent.mkdir(parents=True)
        panel.write_text("<p>回显面板</p>", encoding="utf-8")
        server = self.start(plugin_dir=plugin_dir)
        self.assert_404(server.port, "/plugin/../bot_config.json")
        self.assert_404(server.port, "/plugin/Bad%20Name/x")
        self.assert_404(server.port, "/plugin/echo/../../bot_config.json")
        no_plugin_dir = self.start()
        self.assert_404(no_plugin_dir.port, "/plugin/echo/panel.html")

    def test_stop_is_idempotent_and_frees_port(self):
        server = AdminServer(self.api, port=0, static_dir=self.static)
        self.servers.append(server)
        server.start()
        port = server.port
        server.stop()
        server.stop()
        again = AdminServer(self.api, port=port, static_dir=self.static)
        self.servers.append(again)
        again.start()
        self.assertEqual(again.port, port)

    def test_stop_with_inflight_request_returns_without_waiting(self):
        started = threading.Event()

        class BlockingApi(FakeApi):
            def handle(self, method, path, query, body):
                started.set()
                time.sleep(3)
                return super().handle(method, path, query, body)

        server = AdminServer(BlockingApi(), port=0, static_dir=self.static)
        self.servers.append(server)
        server.start()
        port = server.port

        def blocked_call():
            try:
                raw_request(port, "GET", "/api/slow")
            except OSError:
                pass

        worker = threading.Thread(target=blocked_call, daemon=True)
        worker.start()
        self.assertTrue(started.wait(5))
        began = time.monotonic()
        server.stop()
        elapsed = time.monotonic() - began
        # 在途请求还要 sleep 3 秒：停监听必须远快于它，才不至于把 quit 拖住几十秒。
        self.assertLess(elapsed, 1.0)
        self.assertTrue(self.port_is_free(port))
        worker.join(5)
        self.assertFalse(worker.is_alive())

    def test_head_request_and_missing_static_dir_do_not_crash(self):
        missing = AdminServer(self.api, port=0, static_dir=self.root / "不存在")
        self.servers.append(missing)
        missing.start()
        status, _, body = raw_request(missing.port, "HEAD", "/")
        self.assertEqual(status, 404)
        self.assertEqual(body, b"")
        self.assert_404(missing.port, "/")

        server = self.start()
        status, headers, body = raw_request(server.port, "HEAD", "/index.html")
        self.assertEqual(status, 200)
        self.assertEqual(body, b"")
        self.assertEqual(int(dict(headers)["Content-Length"]), len(self.index.encode("utf-8")))


if __name__ == "__main__":
    unittest.main()
