"""1A2B 猜数字小游戏；每个群聊或私聊各自一局，所有玩家共享猜测次数。"""
from __future__ import annotations

import math
import random
import re
import threading
import time
from dataclasses import dataclass, field

START_COMMAND = "1A2B"
TIMEOUT_SECONDS = 180
_WHITESPACE = re.compile(r"\s+")
_GUESS = re.compile(r"[0-9]{4}")
_RULES = (
    "1A2B游戏已开始！\n"
    "规则：每次发送 4 位数字进行猜测，答案由 4 个不重复的数字组成。\n"
    "A 表示数字和位置都正确，B 表示数字正确但位置不对。\n"
    "所有玩家共享本局，限时 3 分钟，猜中后本局结束。"
)


@dataclass
class Game:
    answer: str
    deadline: float
    total: int = 0
    guesses: dict = field(default_factory=dict)
    names: dict = field(default_factory=dict)


class Plugin:
    def on_load(self, ctx):
        self.games = {}
        self.timers = {}
        self.lock = threading.RLock()
        self.active = True

    def on_unload(self, ctx):
        with self.lock:
            self.active = False
            for timer in self.timers.values():
                timer.cancel()
            self.timers.clear()
            self.games.clear()

    def on_message(self, message, ctx):
        if not self.active or message.type != 1 or message.data.get("bot_generated"):
            return
        text = message.text if isinstance(message.text, str) else ""
        if not text:
            return
        compact = _WHITESPACE.sub("", text)
        if compact.upper() == START_COMMAND:
            self._start(message, ctx)
            return
        if not _GUESS.fullmatch(compact):
            return
        self._guess(message, ctx, compact)

    def _start(self, message, ctx):
        key = self._key(message)
        with self.lock:
            game = self.games.get(key)
        if game is not None:
            ctx.reply(message, self._countdown_text(game))
            return
        answer = "".join(random.sample("0123456789", 4))
        # 先确认开局提示能发出，再登记对局；避免发送失败后玩家不知道已经开局。
        if ctx.reply(message, _RULES) is False:
            return
        game = Game(answer, time.monotonic() + TIMEOUT_SECONDS)
        with self.lock:
            if not self.active:
                return
            self.games[key] = game
            timer = threading.Timer(TIMEOUT_SECONDS, self._expire, args=(key, game, message, ctx))
            timer.daemon = True
            self.timers[key] = timer
            timer.start()

    def _guess(self, message, ctx, guess):
        key = self._key(message)
        with self.lock:
            if key not in self.games:
                return
        name = self._display_name(message, ctx)
        player = message.sender_id or message.chat_id or "玩家"
        with self.lock:
            game = self.games.get(key)
            if game is None:
                return
            game.total += 1
            game.guesses[player] = game.guesses.get(player, 0) + 1
            game.names[player] = name
            exact, misplaced = self._score(game.answer, guess)
            if exact == 4:
                self.games.pop(key, None)
                self._cancel_timer(key)
                reply = self._win_text(game, player)
            else:
                reply = f"{exact}A{misplaced}B"
        ctx.reply(message, reply)

    def _expire(self, key, game, message, ctx):
        """计时器到点后结束对局并公布答案；已被猜中或被新局替换时不动。"""
        with self.lock:
            if not self.active or self.games.get(key) is not game:
                return
            self.games.pop(key, None)
            self.timers.pop(key, None)
        try:
            ctx.reply(message, f"1A2B游戏已超时结束，答案是{game.answer}。")
        except Exception:
            ctx.logger.exception("1A2B 超时提示发送失败")

    def _cancel_timer(self, key):
        timer = self.timers.pop(key, None)
        if timer is not None:
            timer.cancel()

    @staticmethod
    def _countdown_text(game):
        remaining = max(0, math.ceil(game.deadline - time.monotonic()))
        minutes, seconds = divmod(remaining, 60)
        return f"游戏尚未结束，倒计时{minutes}分{seconds}秒"

    @staticmethod
    def _key(message):
        # 连接也纳入键：换账号后不能复用上一账号的同名群对局。
        return message.session_id, message.chat_id

    @staticmethod
    def _score(answer, guess):
        exact = sum(left == right for left, right in zip(answer, guess))
        common = sum(min(answer.count(digit), guess.count(digit)) for digit in set(guess))
        return exact, common - exact

    @staticmethod
    def _display_name(message, ctx):
        name = ""
        try:
            name = ctx.members.display_name(message.sender_id, message.chat_id) or ""
        except Exception:
            pass
        if not name:
            name = message.data.get("sender_wechat_name") or message.sender_id or "玩家"
        return str(name).replace("\n", " ").replace("\r", " ").strip()[:64] or "玩家"

    @staticmethod
    def _win_text(game, player):
        rows = sorted(game.guesses.items(), key=lambda item: (-item[1], game.names.get(item[0], "")))
        lines = [
            "恭喜回答正确！🎉🎉🎉",
            f"本次累计猜测{game.total}次",
            f"您本次共猜测{game.guesses.get(player, 0)}次",
        ]
        lines.extend(f"{game.names.get(sender, sender)}：{count}次" for sender, count in rows)
        return "\n".join(lines)
