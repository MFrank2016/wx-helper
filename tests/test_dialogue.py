"""对话 JSON 组装、嘴臭短提示选择与异常结果拒绝；使用伪造模型客户端。"""

import json
import unittest
from types import SimpleNamespace

from wxbot_framework.dialogue import SYSTEM, SHORT_SYSTEM, Dialogue
from wxbot_framework.llm import LLMError
from wxbot_framework.mentions import strip_at_labels


class FakeClient:
    def __init__(self, result, *, state_name="默认"):
        self.state = SimpleNamespace(name=state_name)
        self.result = result
        self.calls = []

    def complete_json(self, system, user):
        self.calls.append((system, user))
        return self.result


class Snapshot:
    def __init__(self, messages, current, speaker_name="测试成员", day="2026-10-10"):
        self.messages = messages
        self.current = current
        self.speaker_name = speaker_name
        self.day = day


class DialogueTests(unittest.TestCase):
    def reply(self, result, messages, current="@机器人\u2005请看看", state_name="默认"):
        client = FakeClient(result, state_name=state_name)
        reply = Dialogue(client).reply(Snapshot(messages, current))
        return client, reply

    def test_reply_assembles_previous_and_current_json(self):
        client, reply = self.reply(
            {"reply": "正常回复"},
            [
                {
                    "received_at": "2026-10-10T15:30:00+00:00",
                    "text": "@机器人\u2005第一条",
                },
                {"received_at": "2026-10-10T15:31:00+00:00", "text": "第二条"},
            ],
        )
        self.assertEqual(reply, "正常回复")
        self.assertEqual(len(client.calls), 1)
        system, user = client.calls[0]
        self.assertEqual(system, SYSTEM)
        payload = json.loads(user)
        self.assertEqual(payload["name"], "测试成员")
        self.assertEqual(payload["date"], "2026-10-10")
        self.assertEqual(
            payload["previous"],
            [
                {"time": "23:30:00", "text": "第一条"},
                {"time": "23:31:00", "text": "第二条"},
            ],
        )
        self.assertEqual(payload["current"], "请看看")

    def test_previous_carries_every_given_message_and_strips_labels(self):
        messages = [
            {"received_at": "2026-10-10T23:30:00+08:00", "text": text}
            for text in ("@机器人\u2005甲", "乙", "")
        ]
        client, _ = self.reply({"reply": "好"}, messages)
        payload = json.loads(client.calls[0][1])
        self.assertEqual(
            [row["text"] for row in payload["previous"]],
            [strip_at_labels(text) for text in ("@机器人\u2005甲", "乙", "")],
        )
        self.assertEqual(payload["previous"][0]["time"], "23:30:00")

    def test_mouthy_state_selects_short_system(self):
        client, _ = self.reply(
            {"reply": "短回复"},
            [{"received_at": "2026-10-10T00:00:00+00:00", "text": "历史"}],
            state_name="嘴臭",
        )
        self.assertEqual(client.calls[0][0], SHORT_SYSTEM)
        self.assertNotEqual(SHORT_SYSTEM, SYSTEM)
        self.assertIn("20 个字", SHORT_SYSTEM)

    def test_default_or_missing_state_uses_full_system(self):
        for client, _ in (
            self.reply({"reply": "好"}, []),
            self.reply({"reply": "好"}, [], state_name="夸夸"),
        ):
            self.assertEqual(client.calls[0][0], SYSTEM)
        captured = []
        client = SimpleNamespace(
            complete_json=lambda system, user: (
                captured.append(system) or {"reply": "好"}
            )
        )
        Dialogue(client).reply(Snapshot([], "问题"))
        self.assertEqual(captured[0], SYSTEM)

    def test_reply_is_stripped_and_whitespace_reply_is_rejected(self):
        client, reply = self.reply({"reply": "  好  "}, [])
        self.assertEqual(reply, "好")
        for result in ({"reply": ""}, {"reply": "   "}, {"reply": "\n\t"}):
            with self.subTest(result=result), self.assertRaises(LLMError):
                Dialogue(FakeClient(result)).reply(Snapshot([], "问题"))

    def test_malformed_or_oversized_replies_raise_llm_error(self):
        for result in (
            {},
            {"reply": None},
            {"reply": 0},
            {"reply": []},
            [],
            {"reply": "字" * 342},
            {"reply": "a" * 1024},
        ):
            with self.subTest(result=result), self.assertRaises(LLMError):
                Dialogue(FakeClient(result)).reply(Snapshot([], "问题"))

    def test_byte_limit_and_control_characters_are_handled(self):
        # 341 个汉字 = 1023 字节，正好压住原生接口上限；控制字符交由人格净化在发送前剥离。
        client, reply = self.reply({"reply": "字" * 341}, [])
        self.assertEqual(reply, "字" * 341)
        _, reply = self.reply({"reply": "正常\t回复\n"}, [])
        self.assertEqual(reply, "正常\t回复")
        _, reply = self.reply({"reply": "a\x00b"}, [])
        self.assertEqual(reply, "a\x00b")


if __name__ == "__main__":
    unittest.main()
