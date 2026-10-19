"""长期运行、连接监督与本地控制台命令。"""
from __future__ import annotations

import itertools
import json
import logging
import queue
import shlex
import sys
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from types import SimpleNamespace

from .admin.api import AdminApi
from .admin.debug import Debugger
from .admin.instrument import Instruments
from .admin.queries import AdminQueries
from .admin.server import AdminServer
from .admin.store import AdminStore
from .admins import AdminPolicy
from .backend import CAPABILITIES, TEXT_CAPABILITIES, NativeFileHelperBackend, NativeTextBackend
from .wechat_names import DEFAULT_REFRESH_SECONDS, WechatNameSource
from .plugins import PluginManager
from .scheduler import Scheduler
from .sdk import CLOSE_DEFERRED, CapabilityError, ServiceFacade
from .members import MemberStore
from .personality import prepare_switch

logger = logging.getLogger("Bot")

# 消息关联标识的定宽时间前缀（UTC，到微秒）：同一条上游消息触发的所有记录共用这个标识，
# 日志页按它归集（`admin.queries.AdminQueries.messages`）。前缀定宽是为了字典序等于时间序。
KEY_TIME_FORMAT = "%Y%m%dT%H%M%S%f"
# 同一微秒内可能连收多条消息（同一个群先后两条必须分得开），后缀用进程内自增序号补足，
# 零填充同样为了定宽排序。
KEY_SEQ_WIDTH = 6
_KEY_SEQ = itertools.count(1)
_KEY_LOCK = threading.Lock()


def new_message_key(now=None):
    """生成一条消息的关联标识：UTC 时间前缀 + 进程内自增序号。

    `now` 可注入（测试固定时间前缀用）。跨进程重启时序号从头开始，但时间前缀到微秒，
    与重启前的标识撞在同一微秒的概率可以忽略。
    """
    stamp = (now or datetime.now(timezone.utc)).strftime(KEY_TIME_FORMAT)
    with _KEY_LOCK:
        sequence = next(_KEY_SEQ)
    return f"{stamp}-{sequence:0{KEY_SEQ_WIDTH}d}"


class BotApplication:
    def __init__(self, root, store, *, backend_factory=None):
        self.root, self.store = root, store
        self.config = store.load()
        # 管理台埋点：默认空实现；管理台启动后由 instruments.attach 接上统计库。
        self.instruments = Instruments()
        # 管理台配置：未配置即视为启用；端口与 Task 11 的校验默认值保持一致。
        admin_config = self.config.get("admin", {}) or {}
        self._admin_enabled = bool(admin_config.get("enabled", True))
        self._admin_port = admin_config.get("port", 8787)
        # 未启用时为 None；启用后由 start_admin() 换成带 store/server/api/url 的命名空间对象。
        self.admin = None
        self.service = ServiceFacade(text_delay=self.config.get("text_send_delay"),
                                     instruments=self.instruments)
        self.members = MemberStore.from_config(self.config, self.store.path.parent)
        # 微信库的唯一只读引擎：既把群成员名字写进成员库的微信名缓存（补齐「从没发过言、
        # 因此没有被原生桥接记下名字」的成员），也按需提供群名与群昵称。开关关闭时完全不构造、
        # 不刷新，管理台也取不到群名（回落显示群 ID）。
        # 懒构造是刻意的：取密钥依赖微信进程已在跑，连接成功前构造没有意义。
        names_config = self.config.get("wechat_names") or {}
        self._wechat_names_enabled = bool(names_config.get("enabled", True))
        self._wechat_names_refresh = names_config.get("refresh_seconds", DEFAULT_REFRESH_SECONDS)
        self.wechat_names = None
        self.admins = AdminPolicy(store)
        # 调度器先于插件建好：插件在 on_load 里就能注册任务。
        self.scheduler = Scheduler(store)
        generic = self.config.get("backend") == "native_text"
        capabilities = TEXT_CAPABILITIES if generic else CAPABILITIES
        self.plugins = PluginManager(root / "plugins", store, self.service, capabilities,
                                     self.config.get("plugin_queue_size", 128),
                                     member_store=self.members, admins=self.admins,
                                     instruments=self.instruments, scheduler=self.scheduler)
        # 任务回调借所属插件已有的线程执行；插件构造完才拿得到投递函数。
        self.scheduler.bind(self.plugins.enqueue_job, is_active=self.plugins.is_active)
        backend_type = NativeTextBackend if generic else NativeFileHelperBackend
        self.backend_factory = backend_factory or (lambda: backend_type(root, self.config))
        self.backend = None
        self.stop_event = threading.Event()
        self.stopped_event = threading.Event()
        self._shutdown_complete = False
        self.commands = queue.Queue()
        self.last_cleanup = None
        self._connection_ready = False
        self._last_connection_log = 0
        self._last_connection_error = ""

    def _log_connection_error(self, error):
        text = f"{type(error).__name__}: {error}"
        now = time.monotonic()
        if text != self._last_connection_error or now - self._last_connection_log >= 30:
            logger.warning("等待微信连接：%s", text)
            self._last_connection_error, self._last_connection_log = text, now

    def _start_wechat_names(self):
        """连接成功后启动微信库引擎的后台刷新；开关关闭、成员库缺失或已在运行时什么都不做。

        只启动一次：线程自己在后台按间隔刷新（刷成员名字），群名与群昵称则在被查询时按需读取。
        之后重连不再重启它。
        """
        if not self._wechat_names_enabled or self.members is None or self.wechat_names is not None:
            return
        try:
            source = WechatNameSource(members=self.members, root=self.root,
                                      refresh_seconds=self._wechat_names_refresh)
        except Exception:
            logger.exception("微信名数据源初始化失败，成员库沿用已有的微信名")
            return
        self.wechat_names = source
        source.start()
        # 把群昵称提供者接进成员库，否则「群昵称优先」整条链路取不到数据。
        self.members.set_group_nickname(source.group_member_name)
        logger.info("微信名数据源已启动（每 %s 秒刷新一次）", self._wechat_names_refresh)

    def _stop_wechat_names(self):
        """停止后台刷新并等它退出；必须在成员库关闭之前调用。幂等，失败只记日志。"""
        source, self.wechat_names = self.wechat_names, None
        if source is None:
            return
        try:
            source.stop()
        except Exception:
            logger.exception("微信名数据源收尾失败")
        # 数据源已停：复位群昵称提供者，避免成员库继续查询已停止的引擎。
        if self.members is not None:
            self.members.set_group_nickname(None)

    def _connect(self):
        try:
            candidate = self.backend_factory()
            self.backend = candidate
            self.service.bind(candidate)
            candidate.start(self._emit)
            self._connection_ready = True
            self._last_connection_error = ""
            logger.info("微信已连接，PID=%s；收发范围：%s", candidate.pid,
                        getattr(candidate, "scope", "文本"))
            # 到这一步微信进程确定在跑，取数据库密钥的前提才成立；启动即刷一轮并在后台按间隔刷新。
            self._start_wechat_names()
        except Exception as error:
            self._connection_ready = False
            self._log_connection_error(error)
            self._disconnect()

    def _emit(self, message):
        # 消息关联标识只在这里生成一次：同一条消息的 receive、各插件处理、异常与它引发的
        # 发送/预览都带着同一个值，日志页才能按「一条群消息」归集；同一个群先后两条消息
        # 序号不同，标识不会撞。
        message = replace(message, msg_key=new_message_key())
        # 管理员 token 走私聊文本传输：日志与统计都不能留原文，这里只算一次。
        secret = self.admins.is_secret(message.text)
        # 先显示会话 ID，未加入白名单的新群也能被发现；群正文不进入消息日志。
        if message.is_group or message.chat_id.endswith("@chatroom"):
            logger.info("群消息 | 群ID=%s", message.chat_id)
        else:
            logger.info("私聊消息 | 发送者微信ID=%s", message.sender_id or message.chat_id)
            # 私聊正文可能是管理员 token：只在 DEBUG 留，且截断到 200 字符。
            logger.debug("私聊正文 | 发送者微信ID=%s | 内容=%s",
                         message.sender_id or message.chat_id,
                         "[已隐藏]" if secret else json.dumps(message.text[:200], ensure_ascii=False))
        allowed = self.config.get("chat_allowlist", [])
        # 机器人自己的回显（native 层按 120 秒内发出文本匹配标 bot_generated）不算「收到」：
        # 它仍会走到这里被记一条日志，但不该进收发统计，否则每回一条就多计一次收到。
        if not message.data.get("bot_generated"):
            recorded = message
            if secret:
                # 只换统计用的副本：原文要留给后续插件完成自助绑定。
                recorded = replace(message, text="[已隐藏]")
            self.instruments.record_receive(recorded, allowed=not allowed or message.chat_id in allowed)
        if not allowed or message.chat_id in allowed:
            if self.members is not None:
                try:
                    self.members.record(message)
                except Exception:
                    logger.exception("群文本保存失败，消息继续分发到插件")
            if message.data.get("dispatch_to_plugins", True):
                if self.admins.blocked(message):
                    # 需管理员权限的命令由非管理员发出：不回复、不执行，也不触发人格切换。
                    return
                if self.plugins.is_active("personality"):
                    try:
                        message = prepare_switch(message, self.members)
                    except Exception:
                        logger.exception("人格切换失败，消息继续分发到插件")
                self.plugins.dispatch(message)

    def _disconnect(self):
        self._connection_ready = False
        try:
            result = self.service.close()
            if result is CLOSE_DEFERRED:
                # 清理被延后（原生发送仍在途）：保留 backend 实例，交给 stop()/重连循环重试。
                return False
            if result is not None:
                self.last_cleanup = result
            self.backend = None
            return True
        except Exception as error:
            # 清理忙时保留实例，只重试清理，不覆盖 DLL、不再次注入。
            self._log_connection_error(error)
            return False

    def execute(self, command):
        words = shlex.split(command)
        if not words:
            return
        action = words[0].lower()
        if action in ("quit", "exit", "stop") and len(words) == 1:
            self.stop_event.set()
        elif action == "help" and len(words) == 1:
            logger.info("命令：status / chats / members [群ID] / history 称呼 [条数] / plugins / tasks / admin / enable 名称 / disable 名称 / enable 插件 任务 / disable 插件 任务 / run 插件 任务 / reload [名称] / quit")
        elif action == "status" and len(words) == 1:
            logger.info("状态：%s", json.dumps({"connected": self._connection_ready,
                                               "pid": self.backend.pid if self.backend else None,
                                               "native": self.backend.diagnostics() if self.backend and hasattr(self.backend, "diagnostics") else {},
                                               "plugins": self.plugins.status()}, ensure_ascii=False))
        elif action == "chats" and len(words) == 1:
            rows = self.backend.chats() if self.backend and hasattr(self.backend, "chats") else []
            logger.info("本次连接已接收的会话：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "members" and len(words) in (1, 2):
            if self.members is None:
                raise ValueError("尚未配置成员目录")
            rows = [{"name": m.name, "display_name": m.display_name, "wxid": m.wxid,
                     "room": m.room, "bound": bool(m.wxid)} for m in self.members.list_members(words[1] if len(words) == 2 else None)]
            logger.info("成员目录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "history" and len(words) in (2, 3):
            if self.members is None:
                raise ValueError("尚未配置成员目录")
            rows = self.members.history(words[1], limit=int(words[2]) if len(words) == 3 else 10)
            logger.info("成员聊天记录：%s", json.dumps(rows, ensure_ascii=False))
        elif action == "plugins" and len(words) == 1:
            for row in self.plugins.status():
                state = "运行" if row["active"] else "停用"
                logger.info("%s [%s] 已处理=%s 异常=%s 丢弃=%s %s", row["name"], state,
                            row["handled"], row["errors"], row["dropped"], row["error"])
        elif action == "tasks" and len(words) == 1:
            for row in self.scheduler.status():
                last_ok = row["last_ok"]
                if last_ok is True:
                    result = "成功"
                elif last_ok is False:
                    result = f"失败：{row['last_error']}"
                else:
                    result = "—"
                logger.info("%s/%s [%s] %s 上次=%s %s 跳过=%s", row["plugin"], row["name"], row["state"],
                            row["schedule"] or "—", row["last_run_at"] or "—", result, row["skipped"])
        elif action == "admin" and len(words) == 1:
            # 管理台未启用或启动失败时 self.admin 为 None，只提示原因不抛错。
            if self.admin is not None and getattr(self.admin, "url", None):
                # 带 token 的地址只打印到控制台，不进日志文件。
                print(f"管理台已启动，浏览器打开：{self.admin.url}")
            else:
                logger.info("管理台未启用或启动失败；请查看启动日志中的「管理台启动失败」原因")
        elif action in ("enable", "disable") and len(words) == 2:
            try:
                getattr(self.plugins, action)(words[1])
            except CapabilityError as error:
                # 缺少后端能力或成员目录时给出提示，不打印堆栈。
                logger.warning("命令未执行：%s", str(error))
        elif action in ("enable", "disable") and len(words) == 3:
            try:
                self.scheduler.set_enabled(words[1], words[2], action == "enable")
            except ValueError as error:
                logger.warning("命令未执行：%s", str(error))
        elif action == "run" and len(words) == 3:
            try:
                if not self.scheduler.run_now(words[1], words[2]):
                    logger.warning("任务未能投递：%s/%s", words[1], words[2])
            except ValueError as error:
                logger.warning("命令未执行：%s", str(error))
        elif action == "reload" and len(words) in (1, 2):
            self.plugins.reload(words[1] if len(words) == 2 else None)
        else:
            raise ValueError("未知命令；输入 help 查看用法")

    def start_admin(self):
        """按配置启动管理台，返回访问地址；未启用或启动失败时返回 None。

        失败绝不拖垮机器人：异常只记一条 ERROR 日志，并把已经建好的部分
        （已开的统计库、已起的监听）清理干净，不留半开状态。
        """
        if not self._admin_enabled:
            return None
        store = None
        server = None
        try:
            # 统计库固定放在工作目录下，独立于成员库。
            data_dir = self.root / ".runtime"
            data_dir.mkdir(parents=True, exist_ok=True)
            store = AdminStore(data_dir / "admin_data.sqlite3")
            store.open()
            self.instruments.attach(store)
            resolver = None
            if self.members is not None:
                def resolver(wxid, room):
                    # queries 的契约是「显示名字符串」；走成员目录的统一显示名入口，
                    # 才会带上「显式 /绑定 称呼 → 群昵称 → 微信名」的优先级。
                    return self.members.display_name(wxid, room)
            def chat_resolver(chat_id):
                # 只有群 ID 才有群名；引擎未就绪（未连接/配置关闭）或取不到时返回 None，
                # 由调用方回落显示 chat_id。
                source = self.wechat_names
                if source is None or not isinstance(chat_id, str) or not chat_id.endswith("@chatroom"):
                    return None
                try:
                    return source.group_name(chat_id)
                except Exception:
                    logger.debug("群名查询失败：群=%s", chat_id, exc_info=True)
                    return None
            queries = AdminQueries(store, name_resolver=resolver, chat_name_resolver=chat_resolver,
                                   member_store=self.members)
            debugger = Debugger(self.plugins, self.instruments, run_timeout=120.0)
            api = AdminApi(store=store, queries=queries, plugins=self.plugins,
                           instruments=self.instruments, debugger=debugger,
                           plugin_dir=self.root / "plugins",
                           bot_config_path=self.store.path, config_loader=self.store.load,
                           scheduler=self.scheduler)
            server = AdminServer(api, host="127.0.0.1", port=self._admin_port,
                                 plugin_dir=self.root / "plugins")
            url = server.start()
        except Exception:
            logger.exception("管理台启动失败；机器人继续运行")
            if server is not None:
                try:
                    server.stop()
                except Exception:
                    logger.exception("清理管理台监听失败")
            self.instruments.detach()
            if store is not None:
                try:
                    store.close()
                except Exception:
                    logger.exception("清理管理台统计库失败")
            return None
        self.admin = SimpleNamespace(store=store, server=server, api=api, url=url)
        # 带 token 的地址只打印到控制台：写进 INFO 日志等于把访问凭据落盘。
        print(f"管理台已启动，浏览器打开：{url}")
        return url

    def stop_admin(self):
        """幂等收尾：先关监听、再落盘关库、最后摘掉埋点；任何一步失败都继续往下清理。"""
        current = self.admin
        self.admin = None
        if current is not None:
            try:
                current.server.stop()
            except Exception:
                logger.exception("管理台监听关闭失败")
            try:
                current.store.close()
            except Exception:
                logger.exception("管理台统计库关闭失败")
        self.instruments.detach()

    def _read_console(self):
        # stdin 关闭时仍继续运行，适合后台启动。
        try:
            for line in sys.stdin:
                if self.stop_event.is_set():
                    break
                self.commands.put(line.strip())
        except (OSError, ValueError):
            pass

    def run(self, *, seconds=0):
        started = time.monotonic()
        next_connect = 0
        next_rollover = 0
        try:
            if self.members is not None:
                self.members.initialize()
            # 管理台先于插件启动：最早的插件回调也能被记录下来。
            self.start_admin()
            self.plugins.reload()
            if self.config.get("console", True):
                threading.Thread(target=self._read_console, name="BotConsole", daemon=True).start()
            logger.info("框架已启动；输入 help 查看插件管理命令，Ctrl+C 退出")
            while not self.stop_event.is_set():
                now = time.monotonic()
                if now >= next_rollover:
                    if self.members is not None:
                        self.members.rollover()
                    # 成员库未配置时也要照常判断任务是否到点。
                    self.scheduler.tick()
                    next_rollover = now + 0.5
                if seconds and now - started >= seconds:
                    break
                while True:
                    try:
                        command = self.commands.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self.execute(command)
                    except Exception:
                        logger.exception("控制台命令执行失败")
                if self.stop_event.is_set():
                    break
                if self._connection_ready and self.backend.last_error:
                    self._log_connection_error(self.backend.last_error)
                    self._disconnect()
                    next_connect = now + self.config.get("reconnect_seconds", 5)
                elif not self._connection_ready and now >= next_connect:
                    if self.backend is None or self._disconnect():
                        self._connect()
                    next_connect = time.monotonic() + self.config.get("reconnect_seconds", 5)
                self.stop_event.wait(0.1)
        except KeyboardInterrupt:
            logger.info("收到退出指令")
        finally:
            self.stop()

    def stop(self):
        if self._shutdown_complete:
            return
        self.stop_event.set()
        self.service.cancel_pending()
        cleaned = False
        try:
            # 先卸载原生连接，再等待模型等插件回调，关闭窗口时也能及时清理 DLL。
            for attempt in range(3):
                cleaned = self._disconnect()
                if cleaned:
                    break
                if attempt < 2:
                    time.sleep(0.2)
        finally:
            try:
                # 先停后台刷新线程再往回关成员库：它写的就是成员库。
                self._stop_wechat_names()
            finally:
                try:
                    self.plugins.shutdown()
                finally:
                    try:
                        # 等插件回调整停了再让管理台落盘收尾。
                        self.stop_admin()
                    finally:
                        try:
                            if self.members is not None:
                                self.members.close()
                        finally:
                            # 微信库引擎已在上面的 _stop_wechat_names() 收尾；它保留的明文快照
                            # （.runtime/contact_cache/）是刻意的复用缓存，交给用户按需清理。
                            self.stopped_event.set()
        if not cleaned:
            raise RuntimeError("原生连接未能清理；保留 DLL，请查看日志后再启动")
        self._shutdown_complete = True
        logger.info("框架已停止；连接清理：%s", self.last_cleanup)
