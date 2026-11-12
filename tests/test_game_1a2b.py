"""1A2B 小游戏的离线收发验证；不连接微信。"""
import unittest
from unittest.mock import Mock

from plugins.game_1a2b import Plugin
from wxbot_framework.backend import TEXT_CAPABILITIES
from wxbot_framework.sdk import Message, PluginContext, ServiceFacade

ROOM = '12345678@chatroom'
OTHER_ROOM = '87654321@chatroom'
FRIEND = 'wxid_game_friend'
A = 'wxid_player_a'
B = 'wxid_player_b'


def message(text, *, chat=ROOM, sender=A, session='current', name=''):
    return Message(text, chat, sender, type=1, is_group=chat.endswith('@chatroom'),
                   session_id=session, data={'sender_wechat_name': name})


class GameTests(unittest.TestCase):
    def setUp(self):
        self.backend = Mock(session_id='current')
        self.backend.send_text.return_value = True
        self.service = ServiceFacade(text_delay={'enabled': False})
        self.service.bind(self.backend)
        self.addCleanup(self.service.close)
        self.ctx = PluginContext('game_1a2b', {}, self.service, TEXT_CAPABILITIES)
        self.plugin = Plugin()
        self.plugin.on_load(self.ctx)
        self.addCleanup(self.plugin.on_unload, self.ctx)

    def emit(self, msg):
        self.plugin.on_message(msg, self.ctx)

    def start(self, chat=ROOM):
        self.emit(message('1A2B', chat=chat))
        return self.plugin.games[('current', chat)]

    def last_text(self):
        return self.backend.send_text.call_args.args[1]

    def test_start_replies_rules_and_creates_unique_answer(self):
        game = self.start()
        self.assertTrue(self.last_text().startswith('1A2B游戏已开始'))
        self.assertIn('A 表示数字和位置都正确', self.last_text())
        self.assertEqual(len(game.answer), 4)
        self.assertEqual(len(set(game.answer)), 4)

    def test_guess_returns_a_b_and_counts_each_player(self):
        game = self.start()
        game.answer = '1234'
        self.emit(message(' 1 2 3 5 ', sender=A, name='甲'))
        self.assertEqual(self.last_text(), '3A0B')
        self.emit(message('5678', sender=B, name='乙'))
        self.assertEqual(self.last_text(), '0A0B')
        self.assertEqual(game.total, 2)
        self.assertEqual(game.guesses, {A: 1, B: 1})

    def test_win_replies_total_player_count_and_descending_board(self):
        game = self.start()
        game.answer = '1234'
        self.emit(message('5678', sender=A, name='甲'))
        self.emit(message('5678', sender=B, name='乙'))
        self.emit(message('1234', sender=A, name='甲'))
        self.assertEqual(self.last_text(), '\n'.join([
            '恭喜回答正确！🎉🎉🎉',
            '本次累计猜测3次',
            '您本次共猜测2次',
            '甲：2次',
            '乙：1次',
        ]))
        self.assertNotIn(('current', ROOM), self.plugin.games)
        self.assertNotIn(('current', ROOM), self.plugin.timers)

    def test_start_while_active_only_reports_countdown(self):
        game = self.start()
        self.backend.send_text.reset_mock()
        self.emit(message('1A2B'))
        self.assertIn('游戏尚未结束', self.last_text())
        self.assertRegex(self.last_text(), r'倒计时\d+分\d+秒')
        self.assertIs(self.plugin.games[('current', ROOM)], game)

    def test_timeout_ends_game_and_reveals_answer(self):
        game = self.start()
        game.answer = '1234'
        timer = self.plugin.timers[('current', ROOM)]
        timer.cancel()
        self.backend.send_text.reset_mock()
        self.plugin._expire(('current', ROOM), game, message('1A2B'), self.ctx)
        self.assertNotIn(('current', ROOM), self.plugin.games)
        self.assertIn('已超时结束', self.last_text())
        self.assertIn('1234', self.last_text())

    def test_different_chats_and_private_chat_have_independent_games(self):
        room = self.start(ROOM)
        other = self.start(OTHER_ROOM)
        private = self.start(FRIEND)
        self.assertIsNot(room, other)
        self.assertIsNot(room, private)
        room.answer = '1234'
        other.answer = '5678'
        private.answer = '9012'
        self.emit(message('1234', chat=ROOM, sender=A, name='甲'))
        self.assertNotIn(('current', ROOM), self.plugin.games)
        self.assertIn(('current', OTHER_ROOM), self.plugin.games)
        self.assertIn(('current', FRIEND), self.plugin.games)

    def test_non_game_messages_do_not_reply(self):
        self.emit(message('1234'))
        self.emit(message('1A2B 5'))
        self.backend.send_text.assert_not_called()
        self.start()
        self.backend.send_text.reset_mock()
        self.emit(message('12345'))
        self.emit(message('12A4'))
        self.emit(message('1234x'))
        self.backend.send_text.assert_not_called()


if __name__ == '__main__':
    unittest.main()
