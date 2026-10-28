"""文本节奏、统一发送入口及等待期间的退出/重连验证；不连接微信。"""
from __future__ import annotations

import threading
import time
import unittest
from contextlib import contextmanager
from unittest.mock import Mock, patch

from wxbot_framework.pacing import TextSendDelay
from wxbot_framework.sdk import CapabilityError, LegacyService, Message, PluginContext, ServiceFacade


class MemoryBackend:
    def __init__(self, session="original"):
        self.session_id = session
        self.sent = []
        self.stopped = False

    def send_text(self, chat, text):
        self.sent.append((chat, text, time.monotonic()))
        return True

    def stop(self):
        self.stopped = True
        return {"dll_unloaded": True}


class RecordingCancel:
    def __init__(self):
        self.waits = []
        self.cancelled = False

    def wait(self, seconds):
        self.waits.append(seconds)
        return self.cancelled

    def is_set(self):
        return self.cancelled

    def set(self):
        self.cancelled = True


class BlockingBackend:
    """send_text 阻塞直到 release 置位，用于复现原生发送横跨 close 的场景。"""

    def __init__(self, session="blocking"):
        self.session_id = session
        self.sent = []
        self.stopped = False
        self.native_started = threading.Event()
        self.release = threading.Event()

    def send_text(self, chat, text):
        self.native_started.set()
        self.release.wait(30)
        self.sent.append((chat, text, time.monotonic()))
        return True

    def stop(self):
        self.stopped = True
        return {"dll_unloaded": True}


class GuardedMemberStore:
    """delivery_guard 跨 yield 持有成员锁的假成员目录，复现 guard 与发送路径的锁序。"""

    def __init__(self):
        self._lock = threading.RLock()

    @contextmanager
    def delivery_guard(self, snapshot=None):
        with self._lock:
            yield

    @contextmanager
    def personality_delivery_guard(self, state=None):
        with self._lock:
            yield


class TextPacingTests(unittest.TestCase):
    def test_short_text_skips_wait_and_long_text_draws_once_in_range(self):
        pacing = TextSendDelay.from_config()
        self.assertEqual(pacing.seconds_for("你好🌸 abc！"), 0)
        self.assertEqual(pacing.seconds_for("字" * 30), 0)
        for _ in range(5):
            drawn = pacing.seconds_for("字" * 31)
            self.assertGreaterEqual(drawn, 0.5)
            self.assertLessEqual(drawn, 2.0)
        with patch("wxbot_framework.pacing.random.uniform", return_value=0.7) as uniform:
            self.assertAlmostEqual(pacing.seconds_for("字" * 31), 0.7)
            self.assertEqual(uniform.call_count, 1)
            self.assertAlmostEqual(pacing.seconds_for("字" * 100), 0.7)
            self.assertEqual(uniform.call_count, 2)  # 每次长文本只抽一次，不按长度累加
        self.assertEqual(TextSendDelay.from_config({"enabled": False}).seconds_for("字" * 100), 0)

    def test_configuration_rejects_invalid_random_ranges_and_short_text_threshold(self):
        for options in ([], {"enabled": 1}, {"short_text_max_chars": -1},
                        {"short_text_max_chars": 10001}, {"short_text_max_chars": 1.5},
                        {"short_text_max_chars": True}, {"min_seconds": -1},
                        {"max_seconds": float("inf")}, {"max_seconds": float("nan")},
                        {"min_seconds": True}, {"min_seconds": 0.4, "max_seconds": 0.3}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                TextSendDelay.from_config(options)

    def test_all_plugin_text_apis_wait_and_other_payloads_do_not(self):
        backend = MemoryBackend()
        service = ServiceFacade()
        service.bind(backend)
        cancel = RecordingCancel()
        service._cancel = cancel
        ctx = PluginContext("test", {}, service, {"text.send"})
        legacy = LegacyService(service, backend.session_id)
        message = Message(text="test", chat_id="wxid_friend", session_id=backend.session_id)
        long_text = "长" * 31
        with patch("wxbot_framework.pacing.random.uniform", return_value=0.2):
            ctx.reply(message, long_text)
            ctx.send_text("12345@chatroom", long_text)
            legacy.send_text("filehelper", long_text)
            legacy.send_payload({"type": 11237, "data": {"to_wxid": "filehelper", "content": long_text}})
            with self.assertRaises(CapabilityError):
                legacy.send_payload({"type": 99999})
        self.assertEqual(cancel.waits, [0.2] * 4)
        self.assertEqual(len(backend.sent), 4)
        service.close()

    def test_invalid_text_is_rejected_before_wait_or_native_send(self):
        backend = MemoryBackend()
        service = ServiceFacade()
        service.bind(backend)
        cancel = RecordingCancel()
        service._cancel = cancel
        for text in (None, "", "含\0字符"):
            with self.subTest(text_type=type(text)), self.assertRaises(ValueError):
                service.send_text("filehelper", text)
        self.assertEqual(cancel.waits, [])
        self.assertEqual(backend.sent, [])
        service.close()

    def test_two_concurrent_plugins_each_wait_between_native_sends(self):
        backend = MemoryBackend()
        service = ServiceFacade(text_delay={"min_seconds": 0.08, "max_seconds": 0.08})
        service.bind(backend)
        threads = [threading.Thread(target=service.send_text, args=("filehelper", str(i) * 31)) for i in range(2)]
        started = time.monotonic()
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(2)
            self.assertFalse(thread.is_alive())
        self.assertEqual(len(backend.sent), 2)
        self.assertGreaterEqual(backend.sent[0][2] - started, 0.07)
        self.assertGreaterEqual(backend.sent[1][2] - backend.sent[0][2], 0.07)
        service.close()

    def test_close_interrupts_wait_without_blocking_connection_queries(self):
        backend = MemoryBackend()
        service = ServiceFacade()
        service.bind(backend)
        waiting, errors = threading.Event(), []

        def delay(_text):
            waiting.set()
            return 30

        service.text_delay = Mock(seconds_for=delay)
        thread = self._start_send(service, errors)
        self.assertTrue(waiting.wait(1))
        self.assertTrue(service.connected)
        service.close()
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(backend.sent, [])
        self.assertTrue(backend.stopped)

    def test_pending_send_cannot_move_to_new_connection_even_without_session_argument(self):
        original, replacement = MemoryBackend(), MemoryBackend("replacement")
        service = ServiceFacade()
        service.bind(original)
        waiting, errors = threading.Event(), []

        def delay(_text):
            waiting.set()
            return 30

        service.text_delay = Mock(seconds_for=delay)
        thread = self._start_send(service, errors)
        self.assertTrue(waiting.wait(1))
        service.close()
        service.bind(replacement)
        thread.join(1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(errors), 1)
        self.assertEqual(original.sent, [])
        self.assertEqual(replacement.sent, [])
        service.text_delay = TextSendDelay.from_config({"enabled": False})
        service.send_text("filehelper", "新连接")
        self.assertEqual(replacement.sent[0][:2], ("filehelper", "新连接"))
        service.close()

    def test_shutdown_cancellation_rejects_new_sends_before_backend_cleanup(self):
        backend = MemoryBackend()
        service = ServiceFacade(text_delay={"enabled": False})
        service.bind(backend)
        service.cancel_pending()
        with self.assertRaises(RuntimeError):
            service.send_text("filehelper", "退出期间不再发消息")
        self.assertEqual(backend.sent, [])
        self.assertFalse(backend.stopped)
        service.close()
        self.assertTrue(backend.stopped)

    def test_close_defers_cleanup_while_native_send_is_in_flight(self):
        backend = BlockingBackend()
        service = ServiceFacade(text_delay={"enabled": False})
        service.bind(backend)
        sender = threading.Thread(target=lambda: service.send_text("filehelper", "发送"), daemon=True)
        sender.start()
        self.assertTrue(backend.native_started.wait(1))
        closed = []
        closer = threading.Thread(target=lambda: closed.append(service.close()), daemon=True)
        started = time.monotonic()
        closer.start()
        closer.join(2)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 2.0)
        self.assertFalse(closer.is_alive(), "close 不应无限阻塞在在途原生发送上")
        self.assertFalse(backend.stopped, "不允许在原生发送途中卸载 DLL")
        backend.release.set()
        sender.join(1)
        self.assertFalse(sender.is_alive())
        service.close()
        self.assertTrue(backend.stopped)

    def test_response_guard_reply_waiting_on_concurrent_send_does_not_deadlock(self):
        backend = MemoryBackend()
        service = ServiceFacade(text_delay={"enabled": False})
        service.bind(backend)
        store = GuardedMemberStore()
        ctx = PluginContext("test", {}, service, {"text.send"}, member_store=store)
        message = Message(text="hi", chat_id="wxid_friend", session_id=backend.session_id)
        b_holding_send = threading.Event()
        errors = []

        def concurrent_send():
            try:
                def guard():
                    b_holding_send.set()
                    return store.delivery_guard(None)
                service.send_text("wxid_friend", "并发发送", delivery_guard=guard)
            except Exception as error:
                errors.append(repr(error))

        def guarded_reply():
            try:
                with ctx.response_guard(None, None):
                    b_holding_send.wait(2)
                    ctx.reply(message, "带 guard 的回复")
            except Exception as error:
                errors.append(repr(error))

        reply_thread = threading.Thread(target=guarded_reply, daemon=True)
        send_thread = threading.Thread(target=concurrent_send, daemon=True)
        reply_thread.start()
        send_thread.start()
        reply_thread.join(2)
        send_thread.join(2)
        self.assertFalse(reply_thread.is_alive(), errors[-1] if errors else "guard+reply 线程死锁")
        self.assertFalse(send_thread.is_alive(), errors[-1] if errors else "并发发送线程死锁")
        self.assertEqual(errors, [])
        self.assertEqual(len(backend.sent), 2)
        service.close()

    @staticmethod
    def _start_send(service, errors):
        def send():
            try:
                service.send_text("filehelper", "等待发送")
            except RuntimeError as error:
                errors.append(error)
        thread = threading.Thread(target=send, daemon=True)
        thread.start()
        return thread


if __name__ == "__main__":
    unittest.main()
