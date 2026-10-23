"""管理台 API：把统计库、只读查询、插件管理器、配置声明与调试器拼成 HTTP 接口。

本模块只做「路由 + 组装 + 把异常转成状态码」，不含任何业务算法：

- 统计与聚合一律委托 `AdminQueries`；插件状态取自 `PluginManager.status()`，
  今日计数取自 `AdminQueries.plugin_today()`，两者按插件名合并；
- 配置的读值、校验与写回一律委托 `admin.schema`；
- 调试一律委托 `Debugger`；插件自带页面的数据一律转给 `PluginManager.call_web`；
- 开关与重载只调 `PluginManager` 的公开方法（`enable`/`disable`/`reload`），不碰它的 `records`。

异常到状态码：`SaveError`/`ValueError`/`CapabilityError` → 400 且带错误原文（页面直接显示给用户）；
`RuntimeError` 分三种——并发调试拒绝（`Debugger` 的 `BUSY_ERROR`）→ 409，统计库已关闭 → 503，其余 → 400；
未知路由 → 404。

定时任务的四个接口挂在框架级前缀 `/api/tasks/...`（`GET /api/tasks` 与
`POST /api/tasks/<插件>/<任务>/toggle|schedule|run`），不嵌进 `/api/plugins/<名>/...`：
任务列表是跨插件的，而 `_plugin_route` 是两段式解析，嵌套会打架。

日志有两个入口，筛选参数同风格：`GET /api/logs` 是既有的扁平记录（一行一条记录），
`GET /api/logs/messages` 按消息关联标识归集（一条消息一行 + 该消息下的明细列表）。

`handle` 是传输层唯一的契约：`method` 为 `"HEAD"` 时按 `GET` 处理（响应体由传输层丢弃），
`query` 是 `dict[str, str]`，`body` 是解析后的 JSON 对象。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ..config import validate_name
from ..scheduler import parse_schedule
from ..sdk import CapabilityError
from .debug import BUSY_ERROR
from .schema import load_declaration, read_values, write_values
from .store import BEIJING

PLUGINS_PREFIX = "/api/plugins/"
PLUGIN_PAGE_PREFIX = "/api/plugin/"
TASKS_PREFIX = "/api/tasks/"

STATUS_OK = 200
STATUS_BAD_REQUEST = 400
STATUS_NOT_FOUND = 404
STATUS_CONFLICT = 409
STATUS_UNAVAILABLE = 503

# 日志页类型列：空串表示不带类型过滤的「全部」；键是页面直接显示的中文名。
LOG_KIND_LABELS = (
    ("", "全部"),
    ("receive", "收到"),
    ("send", "发出"),
    ("preview", "预览"),
    ("plugin", "插件处理"),
    ("error", "异常"),
    ("system", "系统"),
)

# 插件在列表里的一眼状态；已降级（有错误）优先，其次看启用与运行态。
STATE_DEGRADED = "已降级"
STATE_DISABLED = "已停用"
STATE_INACTIVE = "未运行"
STATE_RUNNING = "运行中"


def beijing_today():
    """当前北京日期（YYYY-MM-DD），与 `store.beijing_day_bounds` 同一时区口径。"""
    return datetime.now(BEIJING).date().isoformat()


class AdminApi:
    """管理台 HTTP 接口：把前几层的产物拼起来，并统一把异常转成状态码。"""

    def __init__(self, *, store, queries, plugins, instruments, debugger,
                 plugin_dir, bot_config_path, config_loader, today=None, scheduler=None):
        self.store = store
        self.queries = queries
        self.plugins = plugins
        self.instruments = instruments
        self.debugger = debugger
        self.plugin_dir = Path(plugin_dir)
        self.bot_config_path = Path(bot_config_path)
        self.config_loader = config_loader
        # 调度器由 `start_admin()` 注入；未注入时任务接口报「调度器未启用」。
        self.scheduler = scheduler
        # 「今天」只在这里算一遍，可注入以便测试与按日查看。
        self._today = today or beijing_today

    def handle(self, method, path, query, body):
        """按路由分派；HEAD 当 GET 处理，异常统一转成状态码。"""
        method = (method or "GET").upper()
        if method == "HEAD":
            method = "GET"
        query = query if isinstance(query, dict) else {}
        body = body if isinstance(body, dict) else {}
        try:
            return self._route(method, path, query, body)
        except (ValueError, CapabilityError) as error:
            return STATUS_BAD_REQUEST, {"error": str(error)}
        except RuntimeError as error:
            return _runtime_result(error)

    def _route(self, method, path, query, body):
        if method == "GET":
            if path == "/api/stats/summary":
                return STATUS_OK, self.queries.summary(self._today())
            if path == "/api/stats/groups":
                return STATUS_OK, self.queries.groups(
                    self._today(), limit=_int_query(query, "limit", 50, low=1, high=1000),
                    configured=_bool_query(query, "configured"))
            if path == "/api/stats/trend":
                return STATUS_OK, self.queries.trend(
                    days=_int_query(query, "days", 7, low=1, high=366), day=self._today())
            if path == "/api/stats/talkers":
                return STATUS_OK, self.queries.talkers(
                    self._today(), chat_id=query.get("chat_id") or None,
                    limit=_int_query(query, "limit", 10, low=1, high=1000))
            if path == "/api/members/lurkers":
                return STATUS_OK, self.queries.lurkers(chat_id=query.get("chat_id") or None)
            if path == "/api/plugins":
                return STATUS_OK, self._plugins_view()
            if path == "/api/tasks":
                return STATUS_OK, {"tasks": self._tasks()}
            if path == "/api/logs/counts":
                return STATUS_OK, self._logs_counts()
            if path == "/api/logs/messages":
                return STATUS_OK, self._messages_view(query)
            if path == "/api/logs":
                return STATUS_OK, self._logs_view(query)
        if method == "POST":
            if path == "/api/plugins":
                return STATUS_OK, self._plugin_create(body)
            if path == "/api/plugins/reload":
                self.plugins.reload()
                return STATUS_OK, {"ok": True, "name": None}
            if path == "/api/debug":
                return STATUS_OK, self._debug_view(body)
            if path == "/api/tasks":
                return STATUS_OK, self._task_create(body)
            if path.startswith(TASKS_PREFIX):
                return self._task_route(path, body)
        if path.startswith(PLUGIN_PAGE_PREFIX):
            if method != "GET":
                return STATUS_NOT_FOUND, {"error": f"接口不存在：{path}"}
            return self._plugin_page(path, query)
        if path.startswith(PLUGINS_PREFIX):
            return self._plugin_route(method, path, body)
        return STATUS_NOT_FOUND, {"error": f"接口不存在：{path}"}

    # --- 统计 ---

    def _logs_counts(self):
        """各类型的日志条数：逐个类型取已有查询的 total，不在 API 层另写聚合。"""
        counts = {}
        for kind, label in LOG_KIND_LABELS:
            counts[label] = self.queries.logs(kinds=[kind] if kind else None, limit=1)["total"]
        return {"counts": counts}

    def _logs_view(self, query):
        kinds = _kinds_query(query)
        return self.queries.logs(
            kinds=kinds,
            keyword=query.get("keyword", ""),
            chat_id=query.get("chat_id", ""),
            plugin=query.get("plugin", ""),
            since=query.get("since", ""),
            until=query.get("until", ""),
            limit=_int_query(query, "limit", 100, low=1, high=1000),
            offset=_int_query(query, "offset", 0, low=0))

    def _messages_view(self, query):
        """按消息归集的日志视图（每条消息一行 + 明细列表）。

        筛选参数与 `/api/logs` 同风格，只是 `plugin` / `kinds` / `keyword` 的作用范围是「整条消息」：
        组内有一条记录命中就保留整条消息。默认只返回群消息；`include_direct=1` 只多带私聊消息
        （`is_group=0` 且 `chat_id` 非空），`include_system=1` 才带框架自身的系统记录（`chat_id` 为空）。
        """
        return self.queries.messages(
            kinds=_kinds_query(query),
            keyword=query.get("keyword", ""),
            chat_id=query.get("chat_id", ""),
            plugin=query.get("plugin", ""),
            since=query.get("since", ""),
            until=query.get("until", ""),
            include_direct=_bool_query(query, "include_direct"),
            include_system=_bool_query(query, "include_system"),
            limit=_int_query(query, "limit", 100, low=1, high=1000),
            offset=_int_query(query, "offset", 0, low=0))

    # --- 插件 ---

    def _plugins_view(self):
        day = self._today()
        today = self.queries.plugin_today(day)
        # 加载/初始化失败只认 load_errors；「最近错误」列里的 last_error 不参与降级判定。
        load_errors = dict(getattr(self.plugins, "load_errors", None) or {})
        rows = []
        seen = set()
        for status in self.plugins.status():
            row = dict(status)
            name = row.get("name", "")
            seen.add(name)
            counts = today.get(name, {})
            row["today_handled"] = counts.get("handled", 0)
            row["today_errors"] = counts.get("errors", 0)
            declaration_error = ""
            try:
                declaration = load_declaration(self.plugin_dir / name) if name else None
            except (ValueError, OSError) as error:
                # 单个插件声明畸形或读不出来不能让整页打不开：作为带原因的一条照常列出。
                declaration = None
                declaration_error = str(error)
                row["error"] = row.get("error") or declaration_error
            row["declared"] = declaration is not None
            row["has_config"] = bool(declaration and declaration.sections)
            row["pages"] = [dict(page) for page in declaration.pages] if declaration else []
            # 降级只看「加载/初始化失败」「缺少后端能力」「配置声明读不出来」；
            # 回调期间偶发的 last_error 只出现在「最近错误」列，不把插件永久标成已降级。
            row["degraded"] = bool(load_errors.get(name) or row.get("missing_capabilities")
                                   or declaration_error)
            row["state"] = _state_of(row)
            rows.append(row)
        # 目录名不合法、plugin.json 坏掉的插件只在 load_errors 里：它们不在 status() 里，
        # 也要作为一行返回，否则用户在管理台上根本看不到「哪个插件坏了」。
        for name in sorted(load_errors):
            if name in seen:
                continue
            rows.append({
                "name": name, "active": False, "enabled": False, "api": "unknown",
                "missing_capabilities": [], "handled": 0, "errors": 0, "dropped": 0,
                "error": load_errors[name], "today_handled": 0, "today_errors": 0,
                "declared": False, "has_config": False, "pages": [],
                "degraded": True, "state": STATE_DEGRADED,
            })
        return {"day": day, "plugins": rows}

    def _plugin_route(self, method, path, body):
        name, action = _split_route(path, PLUGINS_PREFIX)
        if not name or not action:
            return STATUS_NOT_FOUND, {"error": f"接口不存在：{path}"}
        validate_name(name)
        directory = self.plugin_dir / name
        if action == "config" and method == "GET":
            return STATUS_OK, self._config_view(name, directory)
        if action == "config" and method == "POST":
            return STATUS_OK, self._save_config(name, directory, body)
        if action == "toggle" and method == "POST":
            return STATUS_OK, self._toggle(name, body)
        if action == "debug" and method == "POST":
            return STATUS_OK, self._plugin_debug(name, body)
        if action == "delete" and method == "POST":
            return STATUS_OK, self._plugin_delete(name)
        if action == "reload" and method == "POST":
            self.plugins.reload(name)
            return STATUS_OK, self._degraded(name, {"ok": True, "name": name})
        return STATUS_NOT_FOUND, {"error": f"接口不存在：{path}"}

    def _plugin_debug(self, name, body):
        debug = body.get("debug")
        if type(debug) is not bool:
            raise ValueError("请求体应带 debug 布尔值")
        self.plugins.set_debug(name, debug)
        return {"ok": True, "name": name, "debug": debug}

    def _plugin_delete(self, name):
        target = self.plugins.delete_plugin(name)
        return {"ok": True, "name": name, "trash": target}

    def _plugin_create(self, body):
        name = body.get("name")
        description = body.get("description", "")
        enabled = body.get("enabled", False)
        self.plugins.create_plugin(name, description, enabled=enabled)
        row = next((row for row in self._plugins_view()["plugins"] if row.get("name") == name), None)
        return {"ok": True, "plugin": row}

    def _config_view(self, name, directory):
        declaration = load_declaration(directory)
        if declaration is None:
            return {"name": name, "declared": False, "raw": self._raw_config(name), "pages": []}
        values = read_values(declaration, plugin_dir=directory, bot_config=self._bot_config())
        sections = []
        for section in declaration.sections:
            fields = []
            for field in section.fields:
                masked = field.type == "password"
                fields.append({
                    "key": field.key,
                    "label": field.label,
                    "type": field.type,
                    # 密码字段不回显：值恒为空串并带 masked 标记，留空表示不修改。
                    "value": "" if masked else values.get(field.key, field.default),
                    "masked": masked,
                    "default": field.default,
                    "help": field.help,
                    "range": list(field.range) if field.range else None,
                    "choices": list(field.choices),
                    "allow_empty": field.allow_empty,
                })
            sections.append({"title": section.title, "fields": fields})
        return {"name": name, "declared": True, "source": declaration.source,
                "file": declaration.file, "sections": sections,
                "pages": [dict(page) for page in declaration.pages]}

    def _save_config(self, name, directory, body):
        declaration = load_declaration(directory)
        if declaration is None:
            raise ValueError(f"插件 {name} 未声明可写的配置项")
        values = body.get("values")
        if not isinstance(values, dict):
            raise ValueError("请求体应带 values 键值对象")
        write_values(declaration, _drop_unchanged_secrets(declaration, values),
                     plugin_dir=directory, bot_config_path=self.bot_config_path)
        payload = {"ok": True, "name": name}
        if body.get("reload", True):
            self.plugins.reload(name)
            self._degraded(name, payload)
        return payload

    def _toggle(self, name, body):
        enabled = body.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("请求体应带 enabled 布尔值")
        if enabled:
            self.plugins.enable(name)
        else:
            self.plugins.disable(name)
        payload = {"ok": True, "name": name, "enabled": enabled}
        if enabled:
            self._degraded(name, payload)
        return payload

    def _degraded(self, name, payload):
        """把该插件的降级原因并进响应：只认加载/初始化失败与缺少后端能力。

        「最近错误」（回调期间的 last_error）不算降级，否则偶发一次异常就会让
        配置页一直弹「配置可能没生效」的黄条。
        """
        reason = self.plugins.load_errors.get(name, "")
        if not reason:
            for row in self.plugins.status():
                if row.get("name") == name and row.get("missing_capabilities"):
                    reason = "缺少后端能力：" + "、".join(row["missing_capabilities"])
                    break
        payload["degraded"] = bool(reason)
        payload["reason"] = reason
        return payload

    def _raw_config(self, name):
        plugins = self._bot_config().get("plugins")
        value = plugins.get(name) if isinstance(plugins, Mapping) else None
        return dict(value) if isinstance(value, Mapping) else {}

    def _bot_config(self):
        data = self.config_loader()
        return data if isinstance(data, Mapping) else {}

    # --- 插件自带页面 ---

    def _plugin_page(self, path, query):
        name, action = _split_route(path, PLUGIN_PAGE_PREFIX)
        if not name or not action:
            return STATUS_NOT_FOUND, {"error": f"接口不存在：{path}"}
        return STATUS_OK, self.plugins.call_web(name, action, dict(query))

    # --- 定时任务 ---

    def _tasks(self):
        """任务行由调度器给出（明细口径在那边），这里只做「有没有调度器」的判断。"""
        if self.scheduler is None:
            raise RuntimeError("调度器未启用")
        return self.scheduler.status()

    def _task_route(self, path, body):
        if self.scheduler is None:
            # 与 GET /api/tasks 用同一口径：干净 400 原文，而不是 AttributeError 500。
            raise RuntimeError("调度器未启用")
        plugin, name, action = _split_task_route(path)
        if not plugin or not name or not action:
            return STATUS_BAD_REQUEST, {"error": f"接口不存在：{path}"}
        validate_name(plugin)
        validate_name(name)
        if action == "toggle":
            return STATUS_OK, self._task_toggle(plugin, name, body)
        if action == "schedule":
            return STATUS_OK, self._task_schedule(plugin, name, body)
        if action == "run":
            return STATUS_OK, self._task_run(plugin, name)
        if action == "delete":
            return STATUS_OK, self._task_delete(plugin, name)
        return STATUS_BAD_REQUEST, {"error": f"接口不存在：{path}"}

    def _task_create(self, body):
        plugin = body.get("plugin")
        name = body.get("name")
        callback = body.get("callback")
        schedule = body.get("schedule")
        enabled = body.get("enabled", True)
        validate_name(plugin)
        validate_name(name)
        if not isinstance(callback, str) or not callback.strip():
            raise ValueError("请选择回调任务")
        validate_name(callback.strip())
        if not isinstance(schedule, str) or not schedule.strip():
            raise ValueError("任务生效时间必须是非空字符串")
        if type(enabled) is not bool:
            raise ValueError("enabled 必须是布尔值")
        parse_schedule(schedule.strip())
        rows = self.scheduler.status()
        if any(row["plugin"] == plugin and row["name"] == name for row in rows):
            raise ValueError(f"任务名已存在：{plugin}/{name}")
        callbacks = {
            row["name"] for row in rows
            if row["plugin"] == plugin and row["registered"] and not row.get("derived")
        }
        if callback.strip() not in callbacks:
            raise ValueError(f"回调任务未注册：{callback}")
        self.scheduler.store.set_task_definition(plugin, name, schedule.strip(),
                                                 callback=callback.strip(), enabled=enabled)
        self.scheduler.refresh()
        task = next((row for row in self.scheduler.status()
                     if row["plugin"] == plugin and row["name"] == name), None)
        return {"ok": True, "task": task}

    def _task_delete(self, plugin, name):
        rows = [row for row in self.scheduler.status()
                if row["plugin"] == plugin and row["name"] == name]
        if not rows:
            raise ValueError(f"未找到任务：{plugin}/{name}")
        if not any(row.get("source") == "config" for row in rows):
            raise ValueError("代码默认任务不能删除，只能停用或修改时间")
        _data, removed = self.scheduler.store.delete_task(plugin, name)
        self.scheduler.refresh()
        return {"ok": True, "removed": bool(removed)}

    def _task_toggle(self, plugin, name, body):
        enabled = body.get("enabled")
        if type(enabled) is not bool:
            raise ValueError("请求体应带 enabled 布尔值")
        return {"ok": True, **self.scheduler.set_enabled(plugin, name, enabled)}

    def _task_schedule(self, plugin, name, body):
        return {"ok": True, **self.scheduler.set_schedule(plugin, name, body.get("schedule"))}

    def _task_run(self, plugin, name):
        # 已投递与投递被拒（上一轮还在跑、队列拒绝）都是 200，用 dispatched 区分。
        return {"ok": True, "dispatched": bool(self.scheduler.run_now(plugin, name))}

    # --- 调试 ---

    def _debug_view(self, body):
        chat_id = body.get("chat_id") or ""
        text = body.get("text")
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("缺少会话 ID（chat_id）")
        if not isinstance(text, str) or not text:
            raise ValueError("缺少消息内容（text）")
        send_for_real = body.get("send_for_real", False)
        if type(send_for_real) is not bool:
            raise ValueError("send_for_real 应为布尔值")
        return self.debugger.run(plugin=body.get("plugin") or "",
                                 chat_id=chat_id,
                                 sender_id=body.get("sender_id") or "",
                                 text=text,
                                 send_for_real=send_for_real)


def _runtime_result(error):
    """把 `RuntimeError` 分清：并发调试拒绝是 409，统计库已关闭是 503，其余按 400 呈现原文。"""
    text = str(error)
    if text == BUSY_ERROR:
        return STATUS_CONFLICT, {"error": text}
    if text and "已关闭" in text:
        return STATUS_UNAVAILABLE, {"error": text}
    return STATUS_BAD_REQUEST, {"error": text or "管理台暂不可用"}


def _split_route(path, prefix):
    """把 `/api/plugins/<name>/<action>` 拆成 (name, action)；路径不足两段时返回空串。"""
    name, separator, action = path[len(prefix):].partition("/")
    if not separator:
        return "", ""
    return name, action


def _split_task_route(path):
    """把 `/api/tasks/<插件>/<任务>/<动作>` 拆成 (插件, 任务, 动作)；不足三段时缺的返回空串。"""
    segments = path[len(TASKS_PREFIX):].split("/")
    if len(segments) != 3:
        return "", "", ""
    return segments[0], segments[1], segments[2]


def _kinds_query(query):
    raw = query.get("kinds") or query.get("kind") or ""
    items = [item for item in raw.split(",") if item]
    return items or None


def _bool_query(query, key):
    """开关型参数：`1`/`true`/`yes` 为真，其余（含缺省与拼错的值）一律当假。"""
    return (query.get(key) or "").strip().lower() in ("1", "true", "yes")


def _int_query(query, key, default, *, low=None, high=None):
    raw = query.get(key, "")
    if raw == "":
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {key} 应为整数：{raw!r}") from None
    # SQLite 里负 LIMIT 等于不限条数，越界必须在进查询前卡住。
    if low is not None and value < low:
        raise ValueError(f"参数 {key} 不能小于 {low}：{value}")
    if high is not None and value > high:
        raise ValueError(f"参数 {key} 不能大于 {high}：{value}")
    return value


def _drop_unchanged_secrets(declaration, values):
    """密码字段提交空串表示「不修改」：从写回集合里去掉，沿用已声明的值。"""
    filtered = dict(values)
    for section in declaration.sections:
        for field in section.fields:
            if field.type == "password" and not filtered.get(field.key):
                filtered.pop(field.key, None)
    return filtered


def _state_of(row):
    """一眼状态：已降级优先（只看降级标记），其次看启用与运行态。"""
    if row.get("degraded"):
        return STATE_DEGRADED
    if not row.get("enabled"):
        return STATE_DISABLED
    if not row.get("active"):
        return STATE_INACTIVE
    return STATE_RUNNING
