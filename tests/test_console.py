"""BotApplication.execute 控制台命令；用 object.__new__ 注入伪造成员与插件。"""

import json
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from wxbot_framework import runtime
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import CapabilityError

ROOM = "12345678@chatroom"
MEMBER = SimpleNamespace(
    name="测试成员", display_name="测试成员", wxid="amy123", room=ROOM
)


class ConsoleCommandTests(unittest.TestCase):
    def make_app(
        self,
        *,
        members=Mock(),
        plugins=None,
        backend=None,
        connection_ready=False,
        admin=None,
    ):
        app = object.__new__(BotApplication)
        app.stop_event = threading.Event()
        app.members = members
        app.plugins = (
            plugins if plugins is not None else Mock(status=Mock(return_value=[]))
        )
        app.backend = backend
        app._connection_ready = connection_ready
        app.admin = admin
        return app

    def logged(self, app, command):
        with patch.object(runtime.logger, "info") as info:
            app.execute(command)
        return info

    def test_quit_exit_and_stop_set_stop_event(self):
        for command in ("quit", "exit", "stop"):
            app = self.make_app()
            with self.subTest(command=command):
                app.execute(command)
                self.assertTrue(app.stop_event.is_set())

    def test_empty_and_whitespace_commands_are_ignored(self):
        for command in ("", "   "):
            app = self.make_app()
            with self.subTest(command=command):
                app.execute(command)
                self.assertFalse(app.stop_event.is_set())

    def test_help_logs_command_list(self):
        info = self.logged(self.make_app(), "help")
        self.assertEqual(info.call_count, 1)
        self.assertIn("命令：status", info.call_args.args[0])
        self.assertIn("enable 名称", info.call_args.args[0])

    def test_status_reports_connection_pid_native_and_plugins(self):
        backend = SimpleNamespace(pid=123, diagnostics=lambda: {"account_ready": True})
        plugins = Mock(
            status=Mock(return_value=[{"name": "personality", "active": True}])
        )
        app = self.make_app(plugins=plugins, backend=backend, connection_ready=True)
        info = self.logged(app, "status")
        payload = json.loads(info.call_args.args[1])
        self.assertEqual(payload["connected"], True)
        self.assertEqual(payload["pid"], 123)
        self.assertEqual(payload["native"], {"account_ready": True})
        self.assertEqual(payload["plugins"], [{"name": "personality", "active": True}])
        plugins.status.assert_called_once_with()

    def test_status_without_backend_reports_disconnected_and_empty_native(self):
        info = self.logged(self.make_app(), "status")
        payload = json.loads(info.call_args.args[1])
        self.assertEqual(
            payload, {"connected": False, "pid": None, "native": {}, "plugins": []}
        )

    def test_chats_lists_backend_sessions_or_empty(self):
        backend = SimpleNamespace(chats=lambda: [{"room_wxid": ROOM}])
        info = self.logged(self.make_app(backend=backend), "chats")
        self.assertEqual(json.loads(info.call_args.args[1]), [{"room_wxid": ROOM}])
        info = self.logged(self.make_app(), "chats")
        self.assertEqual(json.loads(info.call_args.args[1]), [])

    def test_members_lists_all_or_filters_by_room(self):
        members = Mock(list_members=Mock(return_value=[MEMBER]))
        info = self.logged(self.make_app(members=members), "members")
        members.list_members.assert_called_once_with(None)
        rows = json.loads(info.call_args.args[1])
        self.assertEqual(
            rows,
            [
                {
                    "name": "测试成员",
                    "display_name": "测试成员",
                    "wxid": "amy123",
                    "room": ROOM,
                    "bound": True,
                }
            ],
        )
        info = self.logged(self.make_app(members=members), "members " + ROOM)
        members.list_members.assert_called_with(ROOM)
        json.loads(info.call_args.args[1])

    def test_members_and_history_without_store_raise(self):
        app = self.make_app(members=None)
        for command in ("members", "history 测试成员"):
            with (
                self.subTest(command=command),
                self.assertRaisesRegex(ValueError, "尚未配置成员目录"),
            ):
                app.execute(command)

    def test_history_queries_store_with_default_or_given_limit(self):
        members = Mock(history=Mock(return_value=[{"text": "旧话"}]))
        info = self.logged(self.make_app(members=members), "history 测试成员")
        members.history.assert_called_once_with("测试成员", limit=10)
        self.assertEqual(json.loads(info.call_args.args[1]), [{"text": "旧话"}])
        info = self.logged(self.make_app(members=members), "history 测试成员 5")
        members.history.assert_called_with("测试成员", limit=5)
        json.loads(info.call_args.args[1])

    def test_plugins_command_logs_state_per_plugin(self):
        plugins = Mock(
            status=Mock(
                return_value=[
                    {
                        "name": "personality",
                        "active": True,
                        "handled": 3,
                        "errors": 0,
                        "dropped": 1,
                        "error": "",
                    },
                    {
                        "name": "echo",
                        "active": False,
                        "handled": 0,
                        "errors": 1,
                        "dropped": 0,
                        "error": "加载失败",
                    },
                ]
            )
        )
        info = self.logged(self.make_app(plugins=plugins), "plugins")
        texts = [call.args[0] % call.args[1:] for call in info.call_args_list]
        self.assertEqual(len(texts), 2)
        self.assertIn("personality [运行] 已处理=3 异常=0 丢弃=1 ", texts[0])
        self.assertIn("echo [停用] 已处理=0 异常=1 丢弃=0 加载失败", texts[1])

    def test_enable_disable_and_reload_delegate_to_plugins(self):
        app = self.make_app()
        app.execute("enable personality")
        app.plugins.enable.assert_called_once_with("personality")
        app.execute("disable personality")
        app.plugins.disable.assert_called_once_with("personality")
        app.execute("reload")
        app.plugins.reload.assert_called_once_with(None)
        app.execute("reload mention_chat")
        app.plugins.reload.assert_called_with("mention_chat")

    def test_enable_missing_capability_logs_warning_without_traceback(self):
        app = self.make_app()
        app.plugins.enable.side_effect = CapabilityError("尚未配置框架成员目录")
        with patch.object(runtime.logger, "warning") as warning:
            app.execute("enable personality")
        warning.assert_called_once_with("命令未执行：%s", "尚未配置框架成员目录")

    def test_admin_command_logs_url(self):
        admin = SimpleNamespace(url="http://127.0.0.1:8787/")
        info = self.logged(self.make_app(admin=admin), "admin")
        self.assertEqual(info.call_count, 1)
        self.assertIn("管理台", info.call_args.args[0])
        self.assertEqual(info.call_args.args[1], "http://127.0.0.1:8787/")

    def test_admin_command_reports_disabled(self):
        info = self.logged(self.make_app(admin=None), "admin")
        self.assertEqual(info.call_count, 1)
        self.assertIn("管理台未启用", info.call_args.args[0])

    def test_admin_command_rejects_extra_arguments(self):
        with self.assertRaisesRegex(ValueError, "未知命令"):
            self.make_app(admin=SimpleNamespace(url="http://127.0.0.1:8787/")).execute(
                "admin now"
            )

    def test_unknown_or_incomplete_commands_raise(self):
        for command in (
            "frobnicate",
            "status extra",
            "plugins extra",
            "enable",
            "disable",
            "quit now",
            "help extra",
        ):
            app = self.make_app()
            with (
                self.subTest(command=command),
                self.assertRaisesRegex(ValueError, "未知命令"),
            ):
                app.execute(command)


if __name__ == "__main__":
    unittest.main()
