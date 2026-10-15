"""原生后端初始化与透传调用；用伪造原生模块，不加载真实 DLL 或微信。"""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from wxbot_framework.backend import NativeFileHelperBackend, NativeTextBackend
from wxbot_framework.sdk import Message

ROOT = Path(__file__).resolve().parent.parent
ROOM = "12345678@chatroom"


class FakeService:
    last_error = ""

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.handler = None
        self.started = False
        self.chats = {"room": {"room_wxid": ROOM}}
        self.self_wxid = "wxid_robot"
        self.preferred_account = "wxid_robot"
        self.mention_messages = 2
        self.router = SimpleNamespace(
            unidentified=0, unparsed_groups=[], ignored_self=1
        )
        self.pid = 123

    def register_msg_handler(self, handler):
        self.handler = handler

    def start(self):
        self.started = True
        return True

    def stop(self):
        self.started = False
        return True

    def send_text(self, chat_id, text):
        return ("sent", chat_id, text)

    def send_at_text(self, room_wxid, content, at_list):
        return ("sent_at", room_wxid, content, at_list)


def fake_native_modules():
    helper = types.ModuleType("filehelper_service")
    helper.NativeFileHelperService = FakeService
    text = types.ModuleType("text_service")
    text.NativeTextService = FakeService
    return helper, text


class BackendTests(unittest.TestCase):
    def setUp(self):
        scratch = ROOT / ".runtime/tests"
        scratch.mkdir(parents=True, exist_ok=True)
        self.temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.root = Path(self.temporary.name)
        self.native = self.root / "native"
        self.native.mkdir()

    def tearDown(self):
        for path in sys.path:
            if str(self.native) == path:
                sys.path.remove(str(self.native))
        self.assertTrue(
            self.root.resolve().is_relative_to((ROOT / ".runtime/tests").resolve())
        )
        self.temporary.cleanup()

    def test_missing_native_directory_raises_for_both_backends(self):
        self.native.rmdir()
        for backend_type in (NativeFileHelperBackend, NativeTextBackend):
            with (
                self.subTest(backend=backend_type.__name__),
                self.assertRaisesRegex(RuntimeError, "未找到.*原生适配目录"),
            ):
                backend_type(self.root, {})

    def test_filehelper_backend_inserts_native_path_and_constructs_service(self):
        helper, _ = fake_native_modules()
        with patch.dict(sys.modules, {"filehelper_service": helper}):
            backend = NativeFileHelperBackend(self.root, {"wechat_pid": 777})
        self.assertIn(str(self.native), sys.path)
        self.assertEqual(backend.scope, "文件传输助手文本")
        self.assertEqual(backend.raw.kwargs, {"pid": 777, "preserve_reports": False})
        self.assertTrue(backend.session_id)
        self.assertEqual(backend.raw.last_error, "")
        self.assertEqual(backend.pid, 123)

    def test_filehelper_backend_start_wires_emit_and_passes_through(self):
        helper, _ = fake_native_modules()
        received = []
        with patch.dict(sys.modules, {"filehelper_service": helper}):
            backend = NativeFileHelperBackend(self.root, {})
            self.assertTrue(backend.start(received.append))
            self.assertTrue(backend.raw.started)
            backend.raw.handler(
                None,
                11046,
                {"msg": "你好", "room_wxid": ROOM, "sender_id": "amy123"},
                None,
            )
            self.assertEqual(len(received), 1)
            self.assertIsInstance(received[0], Message)
            self.assertEqual(received[0].text, "你好")
            self.assertEqual(received[0].chat_id, ROOM)
        self.assertEqual(
            backend.send_text("filehelper", "你好"), ("sent", "filehelper", "你好")
        )
        self.assertTrue(backend.stop())
        self.assertFalse(backend.raw.started)

    def test_text_backend_constructs_service_with_account(self):
        _, text = fake_native_modules()
        with patch.dict(sys.modules, {"text_service": text}):
            backend = NativeTextBackend(
                self.root, {"wechat_pid": 123, "wechat_account": "wxid_robot"}
            )
        self.assertEqual(backend.scope, "文件传输助手、好友和群聊文本")
        self.assertEqual(
            backend.raw.kwargs,
            {"pid": 123, "account": "wxid_robot", "preserve_reports": False},
        )
        self.assertTrue(backend.session_id)

    def test_text_backend_diagnostics_chats_and_send_at(self):
        _, text = fake_native_modules()
        with patch.dict(sys.modules, {"text_service": text}):
            backend = NativeTextBackend(self.root, {})
        self.assertEqual(
            backend.diagnostics(),
            {
                "account_ready": True,
                "account_source": "current_process",
                "self_wxid": "wxid_robot",
                "bound_account": "wxid_robot",
                "unidentified_messages": 0,
                "unparsed_groups": [],
                "ignored_self_messages": 1,
                "mention_messages": 2,
            },
        )
        self.assertEqual(backend.chats(), [{"room_wxid": ROOM}])
        self.assertEqual(
            backend.send_at_text(ROOM, "内容", ["amy123"]),
            ("sent_at", ROOM, "内容", ["amy123"]),
        )


if __name__ == "__main__":
    unittest.main()
