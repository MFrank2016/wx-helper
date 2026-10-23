"""不连接微信的框架行为测试；发送结果由内存后端记录。"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import types
import threading
import time
import unittest
import urllib.request
import uuid
from unittest.mock import Mock, patch

from wxbot_framework.admin.server import AdminServer
from wxbot_framework.admin.store import AdminStore
from wxbot_framework.backend import CAPABILITIES
from wxbot_framework.config import ConfigStore
from wxbot_framework.locking import InstanceLock
from wxbot_framework.plugins import PluginManager, PluginRecord, PluginSpec
from wxbot_framework.runtime import BotApplication
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOT = Path(__file__).resolve().parent.parent
SCRATCH = ROOT / ".runtime/tests"


def wait_for(condition, seconds=3):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return
        time.sleep(0.01)
    raise AssertionError("等待异步结果超时")


def admin_rows(store):
    """落盘后按写入顺序取出管理台消息记录，字段只取断言需要的列。"""
    store.flush()
    db = store._connection()
    return [dict(row) for row in db.execute(
        "SELECT kind,chat_id,sender_id,is_group,allowed,summary FROM message_log ORDER BY id")]


class RecordingPlugins:
    """记录分发顺序的假插件管理器：只实现 _emit 用到的两个方法。"""

    def __init__(self):
        self.seen = []

    def is_active(self, name):
        return False

    def dispatch(self, message):
        self.seen.append(message)


class MemoryBackend:
    def __init__(self, fail_start=False):
        self.session_id = uuid.uuid4().hex
        self.pid = 123
        self.last_error = None
        self.sent = []
        self.fail_start = fail_start
        self.started = False
        self.stops = 0

    def start(self, emit):
        if self.fail_start:
            raise RuntimeError("模拟微信尚未启动")
        self.emit = emit
        self.started = True
        return True

    def send_text(self, chat_id, text):
        self.sent.append((chat_id, text))
        return True

    def stop(self):
        self.stops += 1
        self.started = False
        return {"dll_unloaded": True}


class FrameworkTests(unittest.TestCase):
    def setUp(self):
        self.previous_logging = logging.root.manager.disable
        logging.disable(logging.CRITICAL)
        # setUp 抛错时 tearDown 不执行；addCleanup 保证日志级别一定恢复，避免污染其他测试文件。
        self.addCleanup(logging.disable, self.previous_logging)
        SCRATCH.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=SCRATCH)
        self.root = Path(self.temporary.name).resolve()
        self.assertTrue(self.root.is_relative_to(SCRATCH.resolve()))
        shutil.copytree(ROOT / "plugins", self.root / "plugins")
        self.path = self.root / "bot_config.json"
        self.config = json.loads((ROOT / "bot_config.example.json").read_text(encoding="utf-8"))
        self.config.update(console=False, reconnect_seconds=0.1, text_send_delay={"enabled": False})
        # 框架测试不调用外部大模型；玩法单独用假客户端验证。
        self.config["plugins"]["idiom_chain"]["enabled"] = False
        self.config["plugins"]["member_identity"]["enabled"] = False
        self.config["plugins"]["chat_summary"]["enabled"] = False
        self.config["plugins"]["chat_records"]["enabled"] = False
        self.config["plugins"]["personality"]["enabled"] = False
        self.config["plugins"]["mention_chat"]["enabled"] = False
        self.config["plugins"].setdefault("voice_reply", {})["enabled"] = False
        self.config["plugins"].setdefault("picture_idiom", {})["enabled"] = False
        self.config["members_config_file"] = None
        self.write_config()
        self.store = ConfigStore(self.path)
        self.backend = MemoryBackend()
        self.service = ServiceFacade(text_delay=self.config["text_send_delay"])
        self.service.bind(self.backend)
        self.manager = PluginManager(self.root / "plugins", self.store, self.service, CAPABILITIES)

    def tearDown(self):
        self.manager.shutdown()
        self.service.close()
        # 递归清理只针对本测试创建、已核对位于工作区的临时目录。
        self.assertTrue(self.root.resolve().is_relative_to(SCRATCH.resolve()))
        self.temporary.cleanup()
        logging.disable(self.previous_logging)

    def write_config(self):
        self.path.write_text(json.dumps(self.config, ensure_ascii=False), encoding="utf-8")

    def create_plugin(self, name, source, *, manifest=None):
        directory = self.root / "plugins" / name
        directory.mkdir(exist_ok=True)
        (directory / "__init__.py").write_text(source, encoding="utf-8")
        if manifest:
            (directory / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
        return directory / "__init__.py"

    def message(self, text, sequence=1):
        return Message.from_native(11046, {"msg": text, "from_wxid": "filehelper", "room_wxid": "",
                                           "native_sender": "self_test", "native_receiver": "filehelper",
                                           "sequence": sequence}, self.backend.session_id)

    @contextlib.contextmanager
    def admin_store(self):
        """临时管理台统计库；退出时先落盘再关闭。"""
        store = AdminStore(self.root / "admin_data.sqlite3")
        store.open()
        try:
            yield store
        finally:
            store.close()

    def test_new_and_legacy_plugins_receive_unicode_snapshot(self):
        self.manager.reload()
        source = {"msg": "echo 中文 🌸\n第二行", "from_wxid": "filehelper"}
        message = Message.from_native(11046, source, self.backend.session_id)
        source["msg"] = "已被上游修改"
        self.manager.dispatch(message)
        self.manager.dispatch(self.message("test", 2))
        wait_for(lambda: len(self.backend.sent) == 2)
        self.assertCountEqual(self.backend.sent, [("filehelper", "中文 🌸\n第二行"), ("filehelper", "filehelper")])

    def test_plugin_failure_does_not_stop_other_plugins_or_next_message(self):
        self.create_plugin("broken", "class Plugin:\n def on_message(self,m,c):\n  raise SystemExit('模拟插件错误')\n")
        self.manager.reload()
        self.manager.dispatch(self.message("test"))
        self.manager.dispatch(self.message("echo 好的", 2))
        wait_for(lambda: len(self.backend.sent) == 2 and self.manager.records["broken"].errors == 2)
        self.assertTrue(self.manager.records["broken"].thread.is_alive())
        self.assertIn(("filehelper", "好的"), self.backend.sent)

    def test_image_plugins_opt_in_without_changing_existing_text_plugins(self):
        self.create_plugin("image_handler", "class Plugin:\n def __init__(self):\n  self.seen=[]\n"
                           " def on_message(self,m,c):\n  self.seen.append((m.type,m.image_path))\n",
                           manifest={"api": "v1", "message_types": [3], "requires": ["image.receive"]})
        self.manager.capabilities = CAPABILITIES | {"image.receive"}
        self.manager.reload()
        self.manager.dispatch(Message.from_native(11046, {"wx_type": 3, "msg": "<msg><img /></msg>",
                               "chat_id": "filehelper", "image_path": "saved.png"}, self.backend.session_id))
        self.manager.dispatch(self.message("echo 文字照常处理"))
        wait_for(lambda: self.manager.records["image_handler"].processed == 1 and
                 self.manager.records["echo"].processed == 1 and self.manager.records["id_responder"].processed == 1)
        self.assertEqual(self.manager.records["image_handler"].instance.seen, [(3, "saved.png")])
        self.assertEqual(self.backend.sent, [("filehelper", "文字照常处理")])

    def test_invalid_plugin_message_types_are_rejected(self):
        for index, types in enumerate(([], [True], [0], ["3"], None)):
            name = f"invalid_images_{index}"
            self.create_plugin(name, "class Plugin: pass\n", manifest={"message_types": types})
            self.assertNotIn(name, self.manager.discover())
            self.assertIn("message_types", self.manager.load_errors[name])

    def test_console_sends_image_with_spaces_in_its_windows_path(self):
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.service.bind(self.backend)
        image = self.root / "图片 sample.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 16)
        self.backend.send_image = Mock(return_value=True)
        app.execute(f'send-image filehelper "{image}"')
        self.backend.send_image.assert_called_once_with("filehelper", str(image))
        self.backend.send_image.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未接受图片"):
            app.execute(f'send-image filehelper "{image}"')

    def test_invalid_image_save_directory_is_rejected_during_config_check(self):
        for value in (None, 123, "", " ", "bad\0path"):
            self.config["image_directory"] = value
            self.write_config()
            with self.subTest(directory=value), self.assertRaisesRegex(ValueError, "image_directory"):
                self.store.load()

    def test_emoticon_console_commands_preserve_paths_and_report_rejection(self):
        from dataclasses import make_dataclass
        from PIL import Image
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.service.bind(self.backend)
        path = self.root / "动态 sticker.gif"
        Image.new("RGB", (4, 4), "green").save(path)
        self.backend.send_emoticon = Mock(return_value=True)
        app.execute(f'send-emoticon filehelper "{path}"')
        self.backend.send_emoticon.assert_called_once_with("filehelper", str(path))
        self.backend.send_emoticon.return_value = False
        with self.assertRaisesRegex(RuntimeError, "未接受表情"):
            app.execute(f'send-emoticon filehelper "{path}"')
        Row = make_dataclass("Row", ["gif_path", "sendable"])
        self.backend.export_emoticons = Mock(return_value=(Row(path, True),))
        app.execute("export-emoticons")
        app.execute("export-emoticons raw")
        self.assertEqual(self.backend.export_emoticons.call_args_list,
                         [unittest.mock.call(convert=True), unittest.mock.call(convert=False)])

    def test_invalid_emoticon_directories_are_rejected_during_config_check(self):
        for field, values in (("emoticon_directory", (None, 123, "", " ", "bad\0path")),
                              ("wechat_data_directory", (123, "", " ", "bad\0path"))):
            for value in values:
                with self.subTest(field=field, value=value):
                    self.config[field] = value
                    self.write_config()
                    with self.assertRaisesRegex(ValueError, field):
                        self.store.load()
            self.config.pop(field, None)

    def test_disabled_and_unsupported_plugins_are_not_imported(self):
        source = "raise RuntimeError('此插件不应被导入')\n"
        self.create_plugin("disabled", source)
        self.create_plugin("groups", source, manifest={"api": "v1", "requires": ["group.at"]})
        self.config["plugins"]["disabled"] = {"enabled": False}
        self.write_config()
        self.manager.reload()
        self.assertNotIn("disabled", self.manager.records)
        self.assertNotIn("groups", self.manager.records)
        groups = next(row for row in self.manager.status() if row["name"] == "groups")
        self.assertEqual(groups["missing_capabilities"], ["group.at"])

    def test_missing_backend_capability_warns_without_traceback(self):
        self.create_plugin("capability_gap", "raise RuntimeError('此插件不应被导入')\n",
                           manifest={"api": "v1", "requires": ["group.at"]})
        with patch("wxbot_framework.plugins.logger.warning") as warning:
            self.manager.reload()
        self.assertNotIn("capability_gap", self.manager.records)
        self.assertEqual(self.manager.load_errors["capability_gap"], "缺少后端能力：group.at")
        warning.assert_any_call("插件 %s 未加载：%s", "capability_gap", "缺少后端能力：group.at")
        self.assertIn("echo", self.manager.records)

    def test_missing_member_store_warns_without_traceback(self):
        self.create_plugin(
            "needs_members",
            "class Plugin:\n def on_load(self, ctx):\n  self.members = ctx.members\n"
            " def on_message(self, message, ctx):\n  pass\n",
        )
        with patch("wxbot_framework.plugins.logger.warning") as warning:
            self.manager.reload()
        self.assertNotIn("needs_members", self.manager.records)
        self.assertEqual(self.manager.load_errors["needs_members"], "尚未配置框架成员目录")
        warning.assert_any_call("插件 %s 未加载：%s", "needs_members", "尚未配置框架成员目录")
        self.assertIn("echo", self.manager.records)

    def test_enable_disable_persist_and_keep_other_plugins_running(self):
        self.manager.reload()
        self.manager.disable("echo")
        saved = self.store.load()["plugins"]["echo"]
        self.assertFalse(saved["enabled"])
        self.assertEqual(saved["prefix"], "echo ")
        self.manager.dispatch(self.message("echo 不应回复"))
        self.manager.dispatch(self.message("test", 2))
        wait_for(lambda: self.manager.records["id_responder"].processed == 2)
        self.assertEqual(self.backend.sent, [("filehelper", "filehelper")])
        self.manager.enable("echo")
        self.assertTrue(self.store.load()["plugins"]["echo"]["enabled"])
        self.manager.dispatch(self.message("echo 恢复了", 3))
        wait_for(lambda: ("filehelper", "恢复了") in self.backend.sent)

    def test_reload_uses_fresh_source_and_failed_reload_keeps_previous(self):
        source = "class Plugin:\n def on_message(self,m,c):\n  c.reply(m,'v1')\n"
        path = self.create_plugin("reloadable", source)
        self.manager.reload()
        self.manager.dispatch(self.message("one"))
        wait_for(lambda: ("filehelper", "v1") in self.backend.sent)
        previous_module = self.manager.records["reloadable"].module_name
        stat = path.stat()
        path.write_text(source.replace("v1", "v2"), encoding="utf-8")
        os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        self.manager.reload("reloadable")
        self.assertNotIn(previous_module, sys.modules)
        self.manager.dispatch(self.message("two", 2))
        wait_for(lambda: ("filehelper", "v2") in self.backend.sent)
        current = self.manager.records["reloadable"]
        path.write_text("class Plugin(:\n", encoding="utf-8")
        self.manager.reload("reloadable")
        self.assertIs(self.manager.records["reloadable"], current)
        self.assertTrue(current.active)
        self.manager.dispatch(self.message("three", 3))
        wait_for(lambda: self.backend.sent.count(("filehelper", "v2")) == 2)

    def test_unload_releases_plugin_resources(self):
        marker = self.root / "lifecycle.txt"
        source = ("class Plugin:\n"
                  " def on_load(self,c):\n  self.file=open(c.config['marker'],'w',encoding='utf-8')\n"
                  " def on_message(self,m,c):\n  pass\n"
                  " def on_unload(self,c):\n  self.file.write('released');self.file.close()\n")
        self.create_plugin("resource", source)
        self.config["plugins"]["resource"] = {"enabled": True, "marker": str(marker)}
        self.write_config()
        self.manager.reload()
        self.manager.disable("resource")
        self.assertEqual(marker.read_text(encoding="utf-8"), "released")

    def test_busy_reload_does_not_replace_active_callback(self):
        source = ("import threading\nclass Plugin:\n"
                  " def on_load(self,c):\n  self.entered=threading.Event();self.release=threading.Event()\n"
                  " def on_message(self,m,c):\n  self.entered.set();self.release.wait(4)\n")
        self.create_plugin("busy", source, manifest={"api": "v1", "concurrency": "serial"})
        self.manager.reload()
        original = self.manager.records["busy"]
        self.manager.dispatch(self.message("work"))
        self.assertTrue(original.instance.entered.wait(1))
        try:
            with self.assertRaises(RuntimeError):
                self.manager.load_one("busy")
            self.assertIs(self.manager.records["busy"], original)
            self.assertTrue(original.active)
        finally:
            original.instance.release.set()

    def test_old_session_reply_is_rejected_after_reconnect(self):
        old = self.message("old")
        self.service.close()
        replacement = MemoryBackend()
        self.service.bind(replacement)
        context = PluginContext("test", {}, self.service, CAPABILITIES)
        with self.assertRaises(RuntimeError):
            context.reply(old, "不应发送")
        self.assertEqual(replacement.sent, [])

    def test_runtime_waits_reconnects_and_stops_cleanly(self):
        created = []

        def factory():
            backend = MemoryBackend(fail_start=not created)
            created.append(backend)
            return backend

        app = BotApplication(self.root, self.store, backend_factory=factory)
        thread = threading.Thread(target=app.run)
        thread.start()
        try:
            wait_for(lambda: len(created) >= 2 and app._connection_ready)
            created[1].last_error = RuntimeError("模拟断线")
            wait_for(lambda: len(created) >= 3 and app._connection_ready)
            created[2].emit(Message.from_native(11046, {"msg": "test", "from_wxid": "filehelper"}, created[2].session_id))
            wait_for(lambda: created[2].sent == [("filehelper", "filehelper")])
            app.commands.put("quit")
            thread.join(3)
            self.assertFalse(thread.is_alive())
            self.assertTrue(all(backend.stops == 1 for backend in created))
            self.assertFalse(app.service.connected)
        finally:
            app.stop_event.set()
            thread.join(3)

    def test_enqueue_after_deactivate_is_ignored(self):
        spec = PluginSpec("probe", self.root / "plugins" / "probe" / "__init__.py", "v1", "", frozenset(),
                          concurrency="serial")
        context = types.SimpleNamespace(logger=logging.getLogger("Probe"))
        record = PluginRecord(spec, types.SimpleNamespace(on_message=lambda *_: None), context, "_probe", 4)
        record.enqueue("first")
        self.assertEqual(record.queue.qsize(), 1)
        record.deactivate(force=True)
        record.enqueue("second")
        self.assertEqual(record.queue.qsize(), 1)

    def test_reload_releases_manager_lock_while_waiting_for_busy_callback(self):
        self.create_plugin("busy", "import time\nclass Plugin:\n    def on_message(self, message, ctx):\n        time.sleep(1.5)\n",
                           manifest={"api": "v1", "concurrency": "serial"})
        self.manager.load_one("busy")
        self.manager.dispatch(self.message("x"))
        time.sleep(0.15)
        reloader = threading.Thread(target=lambda: self.manager.load_one("busy"))
        reloader.start()
        time.sleep(0.25)
        started = time.monotonic()
        self.manager.status()
        self.assertLess(time.monotonic() - started, 0.5)
        reloader.join(4)

    @unittest.skipUnless(sys.platform == "win32", "InstanceLock 依赖 msvcrt")
    def test_single_instance_lock_is_released(self):
        path = self.root / "bot.lock"
        with InstanceLock(path):
            with self.assertRaises(RuntimeError):
                InstanceLock(path).acquire()
        with InstanceLock(path):
            pass

    def test_plugins_reply_to_friend_and_group_conversations(self):
        from wxbot_framework.text import TextRouter
        router = TextRouter()
        router.convert("wxid_self", "filehelper", "init")
        self.manager.reload()
        for sender, text in (("wxid_friend", "echo 好友"),
                             ("12345@chatroom", "wxid_member:\necho 群聊"),
                             ("12345@chatroom", "wxid_member:\ntest")):
            data = router.convert(sender, "wxid_self", text)
            self.manager.dispatch(Message.from_native(11046, data, self.backend.session_id))
        wait_for(lambda: len(self.backend.sent) == 3)
        self.assertCountEqual(self.backend.sent, [("wxid_friend", "好友"),
                                                ("12345@chatroom", "群聊"),
                                                ("12345@chatroom", "12345@chatroom")])

    def test_runtime_allowlist_dispatches_only_selected_chat(self):
        self.config["backend"] = "native_text"
        self.config["chat_allowlist"] = ["filehelper"]
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.service.bind(self.backend)
        app.plugins.reload()
        try:
            app._emit(Message(text="test", chat_id="wxid_other", session_id=self.backend.session_id,
                              data={"msg": "test", "from_wxid": "wxid_other"}))
            app._emit(self.message("echo 允许"))
            wait_for(lambda: app.plugins.records["id_responder"].processed == 1 and
                     app.plugins.records["echo"].processed == 1)
            self.assertEqual(self.backend.sent, [("filehelper", "允许")])
        finally:
            app.plugins.shutdown()

    def test_new_group_is_logged_before_allowlist_without_body_or_member(self):
        self.config["chat_allowlist"] = ["filehelper"]
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.members = Mock()
        room = "87654321@chatroom"
        sender = "wxid_example_member"
        body = "本条群正文不应出现在日志中"
        # 同时覆盖标准消息和仅有会话 ID、缺少群标记的消息，避免误当私聊泄露正文。
        logging.disable(logging.NOTSET)
        with patch.object(app.plugins, "dispatch") as dispatch, self.assertLogs("Bot", "INFO") as captured:
            for is_group in (True, False):
                app._emit(Message(body, room, sender, is_group=is_group))
        self.assertEqual(len(captured.records), 2)
        for record in captured.records:
            self.assertIn(room, record.getMessage())
            self.assertNotIn(body, record.getMessage())
            self.assertNotIn(sender, record.getMessage())
        app.members.record.assert_not_called()
        dispatch.assert_not_called()

    def test_private_log_keeps_sender_but_hides_body_from_info(self):
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        sender = "wxid_example_sender"
        body = "你好 🌸\r\n第二行\t100%"
        # 文件传输助手的会话 ID 与实际作者不同，必须显示作者。
        message = Message.from_native(11046, {"msg": body, "chat_id": "filehelper",
                                             "sender_id": sender, "dispatch_to_plugins": False}, "logging")
        logging.disable(logging.NOTSET)
        with self.assertLogs("Bot", "DEBUG") as captured:
            app._emit(message)
        info = [r.getMessage() for r in captured.records if r.levelno == logging.INFO]
        debug = [r.getMessage() for r in captured.records if r.levelno == logging.DEBUG]
        # 私聊正文可能是管理员 token，只能出现在 DEBUG，INFO 只留发送者。
        self.assertEqual(len(info), 1)
        self.assertIn(sender, info[0])
        self.assertNotIn("你好", info[0])
        self.assertEqual(len(debug), 1)
        self.assertIn(sender, debug[0])
        self.assertNotIn("\n", debug[0])
        self.assertNotIn("\r", debug[0])
        self.assertEqual(json.loads(debug[0].split("内容=", 1)[1]), body)

    @unittest.skipUnless(sys.platform == "win32", "本机原生服务检查")
    def test_native_test_messages_do_not_bypass_group_log_privacy(self):
        native = str(ROOT / "native")
        if native not in sys.path:
            sys.path.insert(0, native)
        from text_service import NativeTextService
        from filehelper_service import ReceivedTextMessage
        service = NativeTextService(pid=123)
        account, sender, room = "wxid_example_self", "wxid_example_member", "87654321@chatroom"
        service.router.set_account(account)
        service._sender_wechat_name = Mock(return_value=None)
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        markers = ("WXBOT_AT_CHAT_PROBE", "WXBOT_GROUP_TEST", "echo WXBOT_GROUP_TEST")
        logging.disable(logging.NOTSET)
        with self.assertLogs(level="INFO") as captured:
            for body in markers:
                raw = ReceivedTextMessage(type=1, sender=room.encode(), receiver=account.encode(),
                                          text=(sender + ":\n" + body).encode())
                app._emit(Message.from_native(11046, service._map_message(raw), "logging"))
        self.assertEqual(len(captured.records), len(markers))
        rendered = "\n".join(captured.output)
        self.assertIn(room, rendered)
        self.assertNotIn(sender, rendered)
        for body in markers:
            self.assertNotIn(body, rendered)

        service._owns_bridge = True
        service._check_account = Mock()
        service._run = Mock(return_value={"success": True})
        with self.assertLogs("NativeText", "INFO") as sent_logs:
            self.assertTrue(service.send_text(room, "WXBOT_GROUP_SEND_TEST"))
        self.assertIn(room, "\n".join(sent_logs.output))
        self.assertNotIn("WXBOT_GROUP_SEND_TEST", "\n".join(sent_logs.output))

    def test_emit_records_receive_with_allowlist_flag(self):
        room = "12345@chatroom"
        self.config["chat_allowlist"] = [room]
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        with self.admin_store() as admin_store:
            app.instruments.attach(admin_store)
            app._emit(Message(text="群里的消息", chat_id=room, sender_id="wxid_member", is_group=True))
            app._emit(Message(text="名单外的消息", chat_id="wxid_other", sender_id="wxid_other"))
            rows = admin_rows(admin_store)
        # 白名单内记 allowed=True，名单外记 allowed=False；两条都留下收到记录。
        self.assertEqual([(row["kind"], row["chat_id"], row["allowed"]) for row in rows],
                         [("receive", room, 1), ("receive", "wxid_other", 0)])
        self.assertEqual(rows[0]["is_group"], 1)
        self.assertEqual(rows[0]["summary"], "群里的消息")

    def test_emit_skips_receive_for_bot_generated_echo(self):
        """机器人自己的回显（bot_generated）不算「收到」，普通消息照记。"""
        room = "12345@chatroom"
        self.config["chat_allowlist"] = [room]
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        with self.admin_store() as admin_store:
            app.instruments.attach(admin_store)
            app._emit(Message(text="机器人回显", chat_id=room, sender_id="wxid_self", is_group=True,
                              data={"msg": "机器人回显", "bot_generated": True,
                                    "dispatch_to_plugins": False}))
            app._emit(Message(text="群里的消息", chat_id=room, sender_id="wxid_member", is_group=True))
            rows = admin_rows(admin_store)
        # 只有普通消息产生收到记录；回显一条都不产生。
        self.assertEqual([(row["kind"], row["summary"]) for row in rows],
                         [("receive", "群里的消息")])

    def test_emit_dispatch_order_and_counts_unchanged(self):
        messages = [self.message(f"echo {index}", index) for index in range(1, 6)]

        def dispatch_order(store):
            app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
            app.plugins = RecordingPlugins()
            if store is not None:
                app.instruments.attach(store)
            for message in messages:
                app._emit(message)
            return app.plugins.seen

        with self.admin_store() as admin_store:
            watched = dispatch_order(admin_store)
            rows = admin_rows(admin_store)
        self.assertEqual([message.text for message in watched],
                         [f"echo {index}" for index in range(1, 6)])
        # 埋点开启前后，插件收到的条数、顺序和消息对象逐一相同。
        self.assertEqual([(message.text, message.chat_id, message.sender_id) for message in dispatch_order(None)],
                         [(message.text, message.chat_id, message.sender_id) for message in watched])
        self.assertEqual([(row["kind"], row["chat_id"]) for row in rows],
                         [("receive", "filehelper")] * 5)

    def test_record_failure_does_not_break_dispatch(self):
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.service.bind(self.backend)
        with self.admin_store() as admin_store:
            app.instruments.attach(admin_store)
            # 统计库写入失败不得冒泡到消息路径：日志只记一条，分发与回复照常。
            admin_store.append = Mock(side_effect=RuntimeError("模拟统计库写入失败"))
            app.plugins.reload()
            try:
                app._emit(self.message("test"))
                wait_for(lambda: self.backend.sent == [("filehelper", "filehelper")])
                self.assertEqual(app.plugins.records["id_responder"].handled, 1)
            finally:
                app.plugins.shutdown()

    def test_disabled_admin_leaves_behavior_identical(self):
        self.config["admin"] = {"enabled": False}
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.service.bind(self.backend)
        # 未启用管理台：埋点为空实现，收发路径与从前一致。
        self.assertFalse(app.instruments.enabled)
        captured = []
        with app.instruments.collect(captured):
            app.plugins.reload()
            try:
                app._emit(self.message("test"))
                wait_for(lambda: self.backend.sent == [("filehelper", "filehelper")])
                self.assertEqual(app.plugins.records["id_responder"].handled, 1)
            finally:
                app.plugins.shutdown()
        self.assertEqual(captured, [])

    def test_admin_disabled_creates_no_store_and_no_server(self):
        self.config["admin"] = {"enabled": False}
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        # 未启用：不建管理台对象、不建库、埋点仍是空实现。
        self.assertIsNone(app.admin)
        self.assertIsNone(app.start_admin())
        self.assertIsNone(app.admin)
        self.assertFalse(app.instruments.enabled)
        self.assertFalse((self.root / ".runtime" / "admin_data.sqlite3").exists())
        app.stop_admin()

    def test_start_admin_opens_store_attaches_instruments_and_returns_url(self):
        self.config["admin"] = {"enabled": True, "port": 0}
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        try:
            url = app.start_admin()
            self.assertTrue(url.startswith("http://127.0.0.1:"))
            self.assertEqual(app.admin.url, url)
            self.assertTrue(app.instruments.enabled)
            self.assertEqual(app.admin.store.path, (self.root / ".runtime" / "admin_data.sqlite3").resolve())
            self.assertTrue(app.admin.store.path.exists())
            self.assertIs(app.admin.server.api, app.admin.api)
            # HTTP 通路可用：概览接口返回 200 的 JSON。
            base, _, query = url.partition("?")
            with urllib.request.urlopen(f"{base}api/stats/summary?{query}", timeout=5) as response:
                self.assertEqual(response.status, 200)
                self.assertIn("today", json.loads(response.read().decode("utf-8")))
        finally:
            app.stop_admin()

    def test_start_admin_failure_is_logged_and_does_not_stop_bot(self):
        self.config["admin"] = {"enabled": True, "port": 0}
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.service.bind(self.backend)
        failed = {}

        def failing_start(server):
            failed["server"] = server
            raise OSError("端口 0 起连续 11 个端口都被占用")

        logging.disable(logging.NOTSET)
        with patch.object(AdminServer, "start", failing_start):
            with self.assertLogs("Bot", "ERROR") as logs:
                self.assertIsNone(app.start_admin())
        self.assertIsNone(app.admin)
        self.assertFalse(app.instruments.enabled)
        self.assertTrue(any("管理台启动失败" in record.getMessage() for record in logs.records))
        # 半开状态被清理干净：统计库已落盘关连接。
        admin_store = failed["server"].api.store
        self.assertTrue(admin_store._closed)
        self.assertIsNone(admin_store._db)
        # 机器人照常运行：插件照常处理消息并回复。
        app.plugins.reload()
        try:
            app._emit(self.message("test"))
            wait_for(lambda: self.backend.sent == [("filehelper", "filehelper")])
        finally:
            app.plugins.shutdown()

    def test_stop_admin_flushes_and_closes_store(self):
        self.config["admin"] = {"enabled": True, "port": 0}
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        app.start_admin()
        admin_store, admin_server = app.admin.store, app.admin.server
        server_thread = admin_server._thread
        app.instruments.record_receive(Message(text="停机前的消息", chat_id="filehelper"), allowed=True)
        app.stop_admin()
        self.assertIsNone(app.admin)
        # 先关监听、再落盘关库、最后摘掉埋点。
        self.assertIsNone(admin_server._httpd)
        self.assertFalse(server_thread.is_alive())
        self.assertEqual(admin_store._records, [])
        self.assertIsNone(admin_store._db)
        self.assertTrue(admin_store._closed)
        self.assertFalse(app.instruments.enabled)
        # 幂等：再次调用不报错、不改变结果。
        app.stop_admin()
        self.assertFalse(app.instruments.enabled)
        db = sqlite3.connect(admin_store.path)
        try:
            rows = db.execute("SELECT kind,summary FROM message_log").fetchall()
        finally:
            db.close()
        self.assertEqual(rows, [("receive", "停机前的消息")])

    def test_run_starts_admin_before_plugins_reload(self):
        self.config["admin"] = {"enabled": True, "port": 0}
        self.write_config()
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        order = []
        app.start_admin = lambda: order.append("admin") or "http://127.0.0.1:1/"
        app.stop_admin = lambda: order.append("admin-stop")
        original_reload = app.plugins.reload
        app.plugins.reload = lambda *args: order.append("plugins") or original_reload(*args)
        app.run(seconds=0.05)
        # 管理台先于插件启动，最早的插件回调也能被记录；收尾时管理台也被停掉。
        self.assertLess(order.index("admin"), order.index("plugins"))
        self.assertEqual(order[-1], "admin-stop")

    def test_stop_order_shuts_admin_down_after_plugins(self):
        app = BotApplication(self.root, self.store, backend_factory=lambda: self.backend)
        order = []
        app.service.cancel_pending = lambda: order.append("cancel")
        app._disconnect = lambda: order.append("disconnect") or True
        app.plugins.shutdown = lambda: order.append("plugins")
        app.stop_admin = lambda: order.append("admin")
        app.members = Mock()
        app.members.close.side_effect = lambda: order.append("members")
        app.stop()
        self.assertEqual(order, ["cancel", "disconnect", "plugins", "admin", "members"])
        self.assertTrue(app.stopped_event.is_set())

    def test_call_web_invokes_plugin_hook(self):
        self.create_plugin("webby",
                           "class Plugin:\n"
                           " def on_message(self, message, ctx):\n  pass\n"
                           " def on_web(self, action, params):\n"
                           "  self.seen = (action, dict(params))\n  return {'action': action, 'count': len(params)}\n")
        self.manager.reload()
        result = self.manager.call_web("webby", "status", {"depth": 3})
        self.assertEqual(result, {"action": "status", "count": 1})
        self.assertEqual(self.manager.records["webby"].instance.seen, ("status", {"depth": 3}))

    def test_call_web_rejects_missing_hook_and_inactive_plugin(self):
        self.create_plugin("webby",
                           "class Plugin:\n"
                           " def on_message(self, message, ctx):\n  pass\n"
                           " def on_web(self, action, params):\n  return {'action': action}\n")
        self.manager.reload()
        with self.assertRaisesRegex(ValueError, "插件未提供管理页面数据"):
            self.manager.call_web("echo", "status", {})
        self.manager.disable("webby")
        with self.assertRaisesRegex(ValueError, "插件未在运行"):
            self.manager.call_web("webby", "status", {})


if __name__ == "__main__":
    unittest.main()
