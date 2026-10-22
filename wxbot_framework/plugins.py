"""插件发现、独立消息队列和生命周期管理。"""
from __future__ import annotations

import copy
import importlib.util
import json
import logging
import queue
import sys
import threading
import time
import types
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from .admin.instrument import Instruments
from .admins import AdminPolicy
from .config import validate_name
from .scheduler import TaskRegistrar
from .sdk import CapabilityError, PluginContext, LegacyService
from .speech import SpeechController

logger = logging.getLogger("Plugins")

# 任务队列短小：任务每轮只取一个且到点才投递，积压说明插件回调已经拖住线程，
# 再多排也只会让"错过的时刻补跑"，与规格的"错过不补跑"相违。
JOB_QUEUE_SIZE = 8

# 并发插件默认最多同时处理 4 个不同用户/会话；同一用户/会话仍然串行。
DEFAULT_CONCURRENCY = 4


class _DaemonPool:
    """固定数量的 daemon 工作线程；关闭时不再接受新任务，也不阻塞进程退出。"""

    def __init__(self, workers, name):
        self._queue = queue.Queue()
        self._stop = threading.Event()
        self._threads = [
            threading.Thread(target=self._run, name=f"{name}.{index}", daemon=True)
            for index in range(max(1, workers))
        ]
        for thread in self._threads:
            thread.start()

    def submit(self, function, *arguments):
        if not self._stop.is_set():
            self._queue.put((function, arguments))

    def shutdown(self, *, wait=False):
        self._stop.set()
        for _ in self._threads:
            self._queue.put(None)
        if wait:
            for thread in self._threads:
                thread.join(timeout=3)

    def _run(self):
        while not self._stop.is_set():
            try:
                item = self._queue.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                return
            function, arguments = item
            try:
                function(*arguments)
            except BaseException:
                logging.getLogger("Plugins").exception("插件并发任务异常；继续处理后续任务")


@dataclass(frozen=True)
class PluginSpec:
    name: str
    path: Path
    api: str
    description: str
    requires: frozenset
    message_types: frozenset = frozenset({1})
    default_enabled: bool = True
    concurrency: str = "user"
    max_concurrency: int = DEFAULT_CONCURRENCY


class PluginRecord:
    def __init__(self, spec, instance, context, module_name, queue_size, *, instruments=None, scheduler=None):
        self.spec, self.instance, self.context = spec, instance, context
        self.module_name = module_name
        self.instruments = instruments if instruments is not None else Instruments()
        self.scheduler = scheduler
        self.concurrency = getattr(spec, "concurrency", "serial")
        self.max_concurrency = getattr(spec, "max_concurrency", DEFAULT_CONCURRENCY)
        self.queue = queue.Queue(maxsize=queue_size)
        self.jobs = queue.Queue(maxsize=JOB_QUEUE_SIZE)
        self.stop_event = threading.Event()
        self.callback_lock = threading.RLock()
        self.state_lock = threading.Lock()
        self.active = True
        self.handled = self.errors = self.dropped = self.job_dropped = 0
        self.last_error = ""
        self._idle = threading.Condition()
        self._active_messages = 0
        self._job_running = False
        self._unloaded = False
        self._pending = {}
        self._running_keys = set()
        self._pending_count = 0
        self._pending_lock = threading.Lock()
        self._pool = None
        if self.concurrency != "serial":
            self._pool = _DaemonPool(self.max_concurrency, f"Plugin.{spec.name}.msg")
        self.thread = threading.Thread(target=self._run, name=f"Plugin.{spec.name}", daemon=True)

    def enqueue(self, message):
        if self.concurrency == "serial":
            with self.state_lock:
                if not self.active:
                    return
                try:
                    self.queue.put_nowait(message)
                except queue.Full:
                    self._drop()
            return
        key = (message.sender_id if self.concurrency == "user" else message.chat_id) or message.chat_id or ""
        with self.state_lock:
            if not self.active:
                return
        with self._pending_lock:
            if self._pending_count >= self.queue.maxsize:
                full = True
                started = False
            else:
                full = False
                self._pending.setdefault(key, deque()).append(message)
                self._pending_count += 1
                started = key not in self._running_keys
                if started:
                    self._running_keys.add(key)
        if full:
            self._drop()
        elif started and self._pool is not None:
            self._pool.submit(self._drain_key, key)

    def _drop(self):
        with self.state_lock:
            self.dropped += 1
            dropped = self.dropped
        if dropped == 1 or dropped % 100 == 0:
            self.context.logger.warning("插件队列已满，累计丢弃 %d 条", dropped)

    def _drain_key(self, key):
        while True:
            with self._pending_lock:
                if not self.active or self.stop_event.is_set():
                    self._running_keys.discard(key)
                    self._pending.pop(key, None)
                    finished = True
                else:
                    pending = self._pending.get(key)
                    if not pending:
                        self._running_keys.discard(key)
                        self._pending.pop(key, None)
                        finished = True
                    else:
                        message = pending.popleft()
                        self._pending_count -= 1
                        finished = False
            if finished:
                self._maybe_unload()
                return
            try:
                self._process_message(message, concurrent=True)
            except BaseException:
                self.context.logger.exception("插件并发消息处理失败；继续处理后续消息")

    def enqueue_job(self, job):
        with self.state_lock:
            if not self.active:
                return False
            try:
                self.jobs.put_nowait(job)
            except queue.Full:
                self.job_dropped += 1
                return False
            return True

    def _run(self):
        if self.concurrency != "serial":
            self._run_jobs()
            return
        try:
            while not self.stop_event.is_set():
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    job = None
                if job is not None:
                    try:
                        with self.callback_lock:
                            # 与消息回调共用一把锁，同一插件不会一边回消息一边跑任务。
                            if not self.active:
                                if self.scheduler is not None:
                                    self.scheduler.abandon(job.plugin, job.name)
                                continue
                            self.scheduler.run_job(job)
                    except BaseException:
                        self.context.logger.exception("插件任务执行失败；继续等待后续任务")
                    continue
                try:
                    message = self.queue.get(timeout=0.2)
                except queue.Empty:
                    continue
                try:
                    with self.callback_lock:
                        if not self.active:
                            continue
                        self._process_message(message)
                finally:
                    self.queue.task_done()
        finally:
            self.unload()

    def _run_jobs(self):
        try:
            while not self.stop_event.is_set():
                try:
                    job = self.jobs.get_nowait()
                except queue.Empty:
                    time.sleep(0.05)
                    continue
                if not self._acquire_exclusive():
                    if self.scheduler is not None:
                        self.scheduler.abandon(job.plugin, job.name)
                    continue
                try:
                    if not self.active:
                        if self.scheduler is not None:
                            self.scheduler.abandon(job.plugin, job.name)
                        continue
                    self.scheduler.run_job(job)
                except BaseException:
                    self.context.logger.exception("插件任务执行失败；继续等待后续任务")
                finally:
                    self._release_exclusive()
        finally:
            self._maybe_unload()

    def _process_message(self, message, *, concurrent=False):
        if concurrent:
            with self._idle:
                while self._job_running and self.active and not self.stop_event.is_set():
                    self._idle.wait()
                if not self.active or self.stop_event.is_set():
                    return
                self._active_messages += 1
        try:
            if not self.active:
                return
            # 回调期间建立线程局部的「当前插件 / 当前消息」上下文；
            # record_plugin / record_error 必须在这个 with 块内调用。
            with self.instruments.plugin_scope(self.spec.name, message=message) as flag:
                started = time.monotonic()
                try:
                    if self.spec.api == "legacy":
                        service = LegacyService(self.context._service, message.session_id)
                        self.instance.on_message(message.protocol, copy.deepcopy(dict(message.data)), service)
                    else:
                        self.instance.on_message(message, self.context)
                    with self.state_lock:
                        self.handled += 1
                except BaseException as error:
                    with self.state_lock:
                        self.errors += 1
                        self.last_error = str(error)
                    self.context.logger.exception("插件处理消息失败；继续处理后续消息")
                    self.instruments.record_error(self.spec.name, error)
                else:
                    self.instruments.record_plugin(
                        self.spec.name, handled=True,
                        ms=(time.monotonic() - started) * 1000, replied=flag.replied)
        finally:
            if concurrent:
                with self._idle:
                    self._active_messages -= 1
                    if not self._active_messages and not self._job_running:
                        self._idle.notify_all()
                self._maybe_unload()

    def _acquire_exclusive(self, timeout=None):
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._idle:
            while self._job_running or self._active_messages:
                if deadline is None:
                    self._idle.wait()
                else:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    self._idle.wait(remaining)
            self._job_running = True
            return True

    def _release_exclusive(self):
        with self._idle:
            self._job_running = False
            self._idle.notify_all()

    def _wait_idle(self, timeout):
        deadline = time.monotonic() + timeout
        with self._idle:
            while self._active_messages or self._job_running:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
            return True

    def _maybe_unload(self):
        if not self.stop_event.is_set():
            return
        with self._idle:
            if self._active_messages or self._job_running:
                return
        self.unload()

    def unload(self):
        with self.state_lock:
            if self._unloaded:
                return
            self._unloaded = True
        if self._pool is not None:
            self._pool.shutdown(wait=False)
        try:
            if self.spec.api == "v1" and callable(getattr(self.instance, "on_unload", None)):
                self.instance.on_unload(self.context)
        except (Exception, SystemExit):
            self.context.logger.exception("插件资源释放失败")
        finally:
            for name in list(sys.modules):
                if name == self.module_name or name.startswith(self.module_name + "."):
                    sys.modules.pop(name, None)

    def deactivate(self, *, force=False):
        if self.concurrency == "serial":
            # 热重载遇到正在执行的长回调时保留原插件，避免两份实例同时回复。
            acquired = self.callback_lock.acquire(timeout=0 if force else 2)
            if not acquired and not force:
                raise RuntimeError(f"插件 {self.spec.name} 正在处理消息或任务，请稍后重试")
            try:
                with self.state_lock:
                    self.active = False
                self.stop_event.set()
            finally:
                if acquired:
                    self.callback_lock.release()
            return
        with self.state_lock:
            self.active = False
        self.stop_event.set()
        with self._pending_lock:
            self._pending.clear()
            self._pending_count = 0
            self._running_keys.clear()
        if self._pool is not None:
            self._pool.shutdown(wait=False)
        if force:
            self._maybe_unload()
            return
        if not self._wait_idle(2):
            raise RuntimeError(f"插件 {self.spec.name} 正在处理消息或任务，请稍后重试")
        self._maybe_unload()

    def join(self):
        if self.concurrency != "serial":
            if not self._wait_idle(3):
                self.context.logger.warning("插件退出仍在等待并发消息；主程序继续清理连接")
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                self.context.logger.warning("插件退出仍在等待任务或 on_unload；主程序继续清理连接")
            self._maybe_unload()
            return
        self.thread.join(timeout=3)
        if self.thread.is_alive():
            self.context.logger.warning("插件退出仍在等待回调或 on_unload；主程序继续清理连接")


class PluginManager:
    def __init__(self, directory, store, service, capabilities, queue_size=128, *, member_store=None, admins=None,
                 instruments=None, scheduler=None):
        self.directory = Path(directory).resolve()
        self.store, self.service = store, service
        self.capabilities = frozenset(capabilities)
        self.queue_size = queue_size
        self.instruments = instruments
        self.scheduler = scheduler
        self._lock = threading.RLock()
        self.records = {}
        self.load_errors = {}
        self.config = store.load()
        self.member_store = member_store
        self.speech = SpeechController(
            self.store.path.parent / self.config.get("tts_config_file", "tts_config.json"),
            self.store.path.parent / ".runtime/tts", member_store, lambda: self.is_active("voice_reply"))
        self.admins = admins if admins is not None else AdminPolicy(store)

    def is_active(self, name):
        with self._lock:
            record = self.records.get(name)
            return bool(record and record.active)

    def discover(self):
        specs = {}
        for directory in sorted(self.directory.iterdir()):
            if not directory.is_dir() or directory.name.startswith("_"):
                continue
            path = directory / "__init__.py"
            if not path.is_file():
                continue
            try:
                name = validate_name(directory.name)
                if not path.resolve().is_relative_to(self.directory):
                    raise ValueError("插件源文件必须位于插件目录内")
                manifest_path = directory / "plugin.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig")) if manifest_path.exists() else {}
                api = manifest.get("api", "v1")
                requires = manifest.get("requires", ["text.receive"])
                message_types = manifest.get("message_types", [1])
                default_enabled = manifest.get("default_enabled", True)
                if type(default_enabled) is not bool:
                    raise ValueError("plugin.json 的 default_enabled 必须是布尔值")
                if api not in ("v1", "legacy") or not isinstance(requires, list) or not all(isinstance(x, str) for x in requires):
                    raise ValueError("plugin.json 的 api / requires 不合法")
                if (not isinstance(message_types, list) or not message_types or
                        any(type(value) is not int or value <= 0 for value in message_types)):
                    raise ValueError("plugin.json 的 message_types 必须是非空正整数列表")
                concurrency = manifest.get("concurrency", "user")
                if concurrency not in ("serial", "user", "chat"):
                    raise ValueError("plugin.json 的 concurrency 只能是 serial / user / chat")
                max_concurrency = manifest.get("max_concurrency", DEFAULT_CONCURRENCY)
                if type(max_concurrency) is not int or not 1 <= max_concurrency <= 16:
                    raise ValueError("plugin.json 的 max_concurrency 应为 1–16 的整数")
                specs[name] = PluginSpec(name, path, api, str(manifest.get("description", "")),
                                         frozenset(requires), frozenset(message_types), default_enabled,
                                         concurrency, max_concurrency)
            except Exception as error:
                self.load_errors[directory.name] = str(error)
                logger.error("插件 %s 无法发现：%s", directory.name, error)
        return specs

    def _prepare(self, spec):
        missing = spec.requires - self.capabilities
        if missing:
            raise CapabilityError(f"缺少后端能力：{', '.join(sorted(missing))}")
        options = self.config.get("plugins", {}).get(spec.name, {})
        registrar = TaskRegistrar(self.scheduler, spec.name) if self.scheduler is not None else None
        context = PluginContext(spec.name, options, self.service, self.capabilities,
                                llm_config_path=self.store.path.parent / self.config.get("llm_config_file", "llm_config.json"),
                                member_store=self.member_store, speech=self.speech, plugin_active=self.is_active,
                                admins=self.admins, tasks=registrar)
        # 每次使用独立模块名并重新编译源文件，热重载不复用旧入口字节码。
        if "_wxbot_plugins" not in sys.modules:
            package = types.ModuleType("_wxbot_plugins")
            package.__path__ = [str(self.directory)]
            sys.modules[package.__name__] = package
        module_name = f"_wxbot_plugins.{spec.name}_{uuid.uuid4().hex}"
        module_spec = importlib.util.spec_from_file_location(module_name, spec.path,
                                                            submodule_search_locations=[str(spec.path.parent)])
        module = importlib.util.module_from_spec(module_spec)
        sys.modules[module_name] = module
        record = None
        try:
            exec(compile(spec.path.read_bytes(), str(spec.path), "exec"), module.__dict__)
            instance = module.Plugin()
            if not callable(getattr(instance, "on_message", None)):
                raise TypeError("Plugin 必须实现 on_message")
            record = PluginRecord(spec, instance, context, module_name, self.queue_size,
                                  instruments=self.instruments, scheduler=self.scheduler)
            try:
                if spec.api == "v1" and callable(getattr(instance, "on_load", None)):
                    instance.on_load(context)
            finally:
                # on_load 结束后关闭注册窗口；on_load 抛异常也要关。
                if registrar is not None:
                    registrar.close()
            self._warn_unregistered_tasks(spec.name)
            return record
        except BaseException:
            # 加载失败：清掉本次已注册的任务，避免留下永不执行的回调。
            if self.scheduler is not None:
                self.scheduler.unregister_plugin(spec.name)
            if record is not None:
                record.unload()
            else:
                for name in list(sys.modules):
                    if name == module_name or name.startswith(module_name + "."):
                        sys.modules.pop(name, None)
            raise

    def _warn_unregistered_tasks(self, name):
        """配置里声明过、代码却没注册的任务名：只提示，不影响插件加载。"""
        declared = self.config.get("plugins", {}).get(name, {}).get("tasks")
        if self.scheduler is None or not isinstance(declared, dict):
            return
        registered = {row["name"] for row in self.scheduler.status()
                      if row["plugin"] == name and row["registered"]}
        missing = sorted(set(declared) - registered)
        if missing:
            logger.warning("插件 %s 配置里声明了未注册的任务：%s", name, "、".join(missing))

    def load_one(self, name):
        validate_name(name)
        spec = self.discover().get(name)
        if spec is None:
            raise ValueError(f"未找到插件：{name}")
        snapshot = self.scheduler.snapshot_plugin(name) if self.scheduler is not None else None
        if self.scheduler is not None:
            # 重载即重新注册；先清旧任务，注册是幂等的。
            self.scheduler.unregister_plugin(name)
        candidate = None
        try:
            candidate = self._prepare(spec)
            with self._lock:
                previous = self.records.get(name)
            if previous is not None:
                previous.deactivate()
            with self._lock:
                self.records[name] = candidate
                candidate.thread.start()
            if previous is not None:
                previous.join()
            self.load_errors.pop(name, None)
            logger.info("已加载插件：%s (%s)", name, spec.api)
        except (Exception, SystemExit):
            # 候选实例的任务在 _prepare() 里已经注册进调度器；这里收尾失败时必须一并
            # 清掉，否则会留下指向「已 unload、且不在 records 里」实例的孤儿回调。
            # 先摘任务注册再 unload：unload 是尽力而为的资源释放，避免它出问题时任务残留。
            try:
                if self.scheduler is not None:
                    self.scheduler.unregister_plugin(name)
                if candidate is not None:
                    candidate.unload()
            finally:
                # 失败时旧实例仍在运行，必须还原它的任务注册，否则它的定时任务凭空消失。
                if snapshot is not None:
                    self.scheduler.restore_plugin(name, snapshot)
            raise

    def disable(self, name, *, persist=True):
        validate_name(name)
        with self._lock:
            record = self.records.get(name)
        if record is not None:
            record.deactivate()
        removed = False
        try:
            with self._lock:
                # deactivate() 可能等了 2 秒，期间同名插件可能已被并发重载替换；
                # 只有当前记录仍是同一个时才移除并注销，避免误删新记录及其任务。
                if self.records.get(name) is record:
                    if persist:
                        try:
                            self.config = self.store.set_enabled(name, False)
                        finally:
                            # 回调已停止，明确报告配置保存错误；不会重新启动半关闭实例。
                            self.records.pop(name, None)
                            removed = True
                    else:
                        self.records.pop(name, None)
                        removed = True
        finally:
            # 记录已被移除（含配置保存失败）时必须注销任务，避免留下孤儿回调。
            if removed and self.scheduler is not None:
                self.scheduler.unregister_plugin(name)
        if record is not None:
            record.join()
        logger.info("已停用插件：%s", name)

    def enable(self, name):
        if name not in self.discover():
            raise ValueError(f"未找到插件：{name}")
        previous = bool(self.config.get("plugins", {}).get(name, {}).get("enabled", True))
        self.config = self.store.set_enabled(name, True)
        if self.scheduler is not None:
            # enable 是唯一绕过 reload 的注册路径，先刷新避免按旧配置快照注册。
            self.scheduler.refresh()
        try:
            self.load_one(name)
        except Exception:
            # 加载失败时回滚启用标记，避免命令报错但配置显示已启用。
            self.config = self.store.set_enabled(name, previous)
            if self.scheduler is not None:
                # 回滚后刷新，让调度器缓存的配置与文件一致。
                self.scheduler.refresh()
            raise

    def reload(self, name=None):
        self.config = self.store.load()
        self.speech.path = self.store.path.parent / self.config.get("tts_config_file", "tts_config.json")
        if self.scheduler is not None:
            # 插件注册时会读调度器缓存的配置，先刷新让它看到新内容。
            self.scheduler.refresh()
        specs = self.discover()
        if name is not None:
            validate_name(name)
            if name not in specs and name not in self.records:
                raise ValueError(f"未找到插件：{name}")
        targets = [name] if name else sorted(set(specs) | set(self.records))
        for target in targets:
            try:
                if target not in specs or not self.config.get("plugins", {}).get(target, {}).get("enabled", specs[target].default_enabled):
                    self.disable(target, persist=False)
                else:
                    self.load_one(target)
            except CapabilityError as error:
                # 未配置成员目录或后端能力不足属于预期状态，只提示不打印堆栈。
                self.load_errors[target] = str(error)
                logger.warning("插件 %s 未加载：%s", target, str(error))
            except (Exception, SystemExit) as error:
                self.load_errors[target] = str(error)
                logger.exception("插件 %s 加载失败，其他插件继续运行", target)

    def dispatch(self, message, *, only=None):
        with self._lock:
            records = tuple(self.records.values())
        for record in records:
            if (only is None or record.spec.name == only) and message.type in record.spec.message_types:
                record.enqueue(message)

    def enqueue_job(self, plugin: str, job) -> bool:
        """把一次任务投递到该插件自己的线程；未接入调度器、插件未运行或队列满都返回 False。"""
        if self.scheduler is None:
            return False
        with self._lock:
            record = self.records.get(plugin)
        if record is None or not record.active:
            return False
        return record.enqueue_job(job)

    def call_web(self, name, action, params):
        """在持有该插件回调锁时调用它的 on_web，供管理台的插件自带页面取数。"""
        with self._lock:
            record = self.records.get(name)
        if record is None or not record.active:
            raise ValueError("插件未在运行")
        hook = getattr(record.instance, "on_web", None)
        if not callable(hook):
            raise ValueError("插件未提供管理页面数据")
        if record.concurrency == "serial":
            with record.callback_lock:
                if not record.active:
                    raise ValueError("插件未在运行")
                return hook(action, params)
        if not record._acquire_exclusive(timeout=2):
            raise RuntimeError("插件正在处理消息或任务，请稍后重试")
        try:
            if not record.active:
                raise ValueError("插件未在运行")
            return hook(action, params)
        finally:
            record._release_exclusive()

    def status(self):
        specs = self.discover()
        with self._lock:
            rows = []
            for name in sorted(set(specs) | set(self.records)):
                record = self.records.get(name)
                spec = specs.get(name)
                missing = spec.requires - self.capabilities if spec else frozenset()
                if record is not None:
                    with record.state_lock:
                        active, handled, errors, dropped, last_error = (
                            record.active, record.handled, record.errors, record.dropped, record.last_error)
                else:
                    active = handled = errors = dropped = 0
                    last_error = ""
                rows.append({"name": name, "active": bool(active),
                             "enabled": self.config.get("plugins", {}).get(name, {}).get(
                                 "enabled", spec.default_enabled if spec else True),
                             "api": spec.api if spec else "unknown", "missing_capabilities": sorted(missing),
                             "handled": handled, "errors": errors,
                             "dropped": dropped,
                             "error": self.load_errors.get(name) or last_error})
            return rows

    def shutdown(self):
        with self._lock:
            records = tuple(self.records.values())
            self.records.clear()
            for record in records:
                record.deactivate(force=True)
        self.speech.close()
        for record in records:
            record.join()
