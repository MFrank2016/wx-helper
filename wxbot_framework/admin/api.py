"""管理台 API：把统计库、只读查询、插件管理器、配置声明与调试器拼成 HTTP 接口。

本模块只做「路由 + 组装 + 把异常转成状态码」，不含任何业务算法：

- 统计与聚合一律委托 `AdminQueries`；插件状态取自 `PluginManager.status()`，
  今日计数取自 `AdminQueries.plugin_today()`，两者按插件名合并；
- 配置的读值、校验与写回一律委托 `admin.schema`；
- 调试一律委托 `Debugger`；插件自带页面的数据一律转给 `PluginManager.call_web`；
- 开关与重载只调 `PluginManager` 的公开方法（`enable`/`disable`/`reload`），不碰它的 `records`。

异常到状态码：`SaveError`/`ValueError`/`CapabilityError` → 400 且带错误原文（页面直接显示给用户）；
`RuntimeError`（统计库已关闭、调试不可用或正忙）→ 503 且带原文；未知路由 → 404。

`handle` 是传输层唯一的契约：`method` 为 `"HEAD"` 时按 `GET` 处理（响应体由传输层丢弃），
`query` 是 `dict[str, str]`，`body` 是解析后的 JSON 对象。
"""
from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from ..config import validate_name
from ..sdk import CapabilityError
from .schema import load_declaration, read_values, write_values
from .store import BEIJING

PLUGINS_PREFIX = "/api/plugins/"
PLUGIN_PAGE_PREFIX = "/api/plugin/"

STATUS_OK = 200
STATUS_BAD_REQUEST = 400
STATUS_NOT_FOUND = 404
STATUS_UNAVAILABLE = 503

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
                 plugin_dir, bot_config_path, config_loader, today=None):
        self.store = store
        self.queries = queries
        self.plugins = plugins
        self.instruments = instruments
        self.debugger = debugger
        self.plugin_dir = Path(plugin_dir)
        self.bot_config_path = Path(bot_config_path)
        self.config_loader = config_loader
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
            return STATUS_UNAVAILABLE, {"error": str(error) or "管理台暂不可用"}

    def _route(self, method, path, query, body):
        if method == "GET":
            if path == "/api/stats/summary":
                return STATUS_OK, self.queries.summary(self._today())
            if path == "/api/stats/groups":
                return STATUS_OK, self.queries.groups(self._today(),
                                                      limit=_int_query(query, "limit", 50))
            if path == "/api/stats/trend":
                return STATUS_OK, self.queries.trend(days=_int_query(query, "days", 7),
                                                     day=self._today())
            if path == "/api/stats/talkers":
                return STATUS_OK, self.queries.talkers(self._today(),
                                                       chat_id=query.get("chat_id") or None,
                                                       limit=_int_query(query, "limit", 10))
            if path == "/api/plugins":
                return STATUS_OK, self._plugins_view()
            if path == "/api/logs":
                return STATUS_OK, self._logs_view(query)
        if method == "POST":
            if path == "/api/plugins/reload":
                self.plugins.reload()
                return STATUS_OK, {"ok": True, "name": None}
            if path == "/api/debug":
                return STATUS_OK, self._debug_view(body)
        if path.startswith(PLUGIN_PAGE_PREFIX):
            if method != "GET":
                return STATUS_NOT_FOUND, {"error": f"接口不存在：{path}"}
            return self._plugin_page(path, query)
        if path.startswith(PLUGINS_PREFIX):
            return self._plugin_route(method, path, body)
        return STATUS_NOT_FOUND, {"error": f"接口不存在：{path}"}

    # --- 统计 ---

    def _logs_view(self, query):
        kinds = _kinds_query(query)
        return self.queries.logs(
            kinds=kinds,
            keyword=query.get("keyword", ""),
            chat_id=query.get("chat_id", ""),
            plugin=query.get("plugin", ""),
            since=query.get("since", ""),
            until=query.get("until", ""),
            limit=_int_query(query, "limit", 100),
            offset=_int_query(query, "offset", 0))

    # --- 插件 ---

    def _plugins_view(self):
        day = self._today()
        today = self.queries.plugin_today(day)
        rows = []
        for status in self.plugins.status():
            row = dict(status)
            name = row.get("name", "")
            counts = today.get(name, {})
            row["today_handled"] = counts.get("handled", 0)
            row["today_errors"] = counts.get("errors", 0)
            declaration = load_declaration(self.plugin_dir / name) if name else None
            row["declared"] = declaration is not None
            row["has_config"] = bool(declaration and declaration.sections)
            row["pages"] = [dict(page) for page in declaration.pages] if declaration else []
            row["degraded"] = bool(row.get("error"))
            row["state"] = _state_of(row)
            rows.append(row)
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
        if action == "reload" and method == "POST":
            self.plugins.reload(name)
            return STATUS_OK, self._degraded(name, {"ok": True, "name": name})
        return STATUS_NOT_FOUND, {"error": f"接口不存在：{path}"}

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
        """把该插件的降级原因并进响应：优先取 load_errors，其次取 status() 里的 error。"""
        reason = self.plugins.load_errors.get(name, "")
        if not reason:
            for row in self.plugins.status():
                if row.get("name") == name and row.get("error"):
                    reason = row["error"]
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


def _split_route(path, prefix):
    """把 `/api/plugins/<name>/<action>` 拆成 (name, action)；路径不足两段时返回空串。"""
    name, separator, action = path[len(prefix):].partition("/")
    if not separator:
        return "", ""
    return name, action


def _kinds_query(query):
    raw = query.get("kinds") or query.get("kind") or ""
    items = [item for item in raw.split(",") if item]
    return items or None


def _int_query(query, key, default):
    raw = query.get(key, "")
    if raw == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ValueError(f"参数 {key} 应为整数：{raw!r}") from None


def _drop_unchanged_secrets(declaration, values):
    """密码字段提交空串表示「不修改」：从写回集合里去掉，沿用已声明的值。"""
    filtered = dict(values)
    for section in declaration.sections:
        for field in section.fields:
            if field.type == "password" and not filtered.get(field.key):
                filtered.pop(field.key, None)
    return filtered


def _state_of(row):
    if row.get("error"):
        return STATE_DEGRADED
    if not row.get("enabled"):
        return STATE_DISABLED
    if not row.get("active"):
        return STATE_INACTIVE
    return STATE_RUNNING
