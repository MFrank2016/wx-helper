"""插件配置声明：解析「有哪些配置、各自什么含义、怎么校验」，并按声明读值、校验与写回。

声明写在插件目录的 `plugin.json` 里的 `config` 段（可选，不写就对旧插件没有任何影响）：

    "config": {
      "source": "bot_config",            // bot_config：值在仓库根 bot_config.json 的 plugins.<插件名> 下
      "file": "bga_config.json",         // plugin_file 来源必填：插件目录下的配置文件
      "pages": [{"key": "ranking", "title": "排行榜"}],   // 插件自带页面，可为空
      "sections": [
        {"title": "基础", "fields": [
          {"key": "prefix", "label": "前缀", "type": "text", "default": "echo ",
           "allow_empty": false, "help": "触发命令用的前缀，默认 echo ，非空字符串"},
          {"key": "account.email", "label": "邮箱", "type": "text", "default": "",
           "help": "BGA 登录邮箱；留空表示匿名模式"},
          {"key": "ranking_depth", "label": "排名深度", "type": "int", "default": 1000,
           "range": [1, 10000], "help": "取前多少名，1–10000"}
        ]}
      ]
    }

字段键用点路径表达嵌套（如 `account.email`）。各字段类型与校验口径：

    text      字符串；null 视同空串，默认允许空串，声明 "allow_empty": false 时要求非空
    password  字符串（空串表示尚未设置；页面里做掩码显示）；null 视同空串，同样受 allow_empty 约束
    int       整数（布尔值不算整数），可选 range=[最小值, 最大值]
    float     数值（整数写法会规范化为小数），可选 range=[最小值, 最大值]
    bool      布尔值
    enum      取值必须是 choices 列表里的一项
    map       键值表，键与值都必须是非空字符串（例如群 ID → 桌游短名）
    json      任意可 JSON 序列化的值

读值：缺失的键用声明的默认值；写回：只改声明覆盖到的键，其余键原样保留，
点路径缺中间层时按需建出对象。写文件用与 `ConfigStore._save` 相同的原子写法。
"""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

MANIFEST_NAME = "plugin.json"
BOT_CONFIG_SOURCE = "bot_config"
PLUGIN_FILE_SOURCE = "plugin_file"
SOURCES = (BOT_CONFIG_SOURCE, PLUGIN_FILE_SOURCE)

# 字段类型到 Python 类型的映射只此一处：解析时用它判类型名是否受支持，校验时按它分派。
FIELD_TYPES = {
    "text": str,
    "password": str,
    "int": int,
    "float": float,
    "bool": bool,
    "enum": str,
    "map": dict,
    "json": object,
}

# 读值时用来区分「键不存在」与「键存在但值是 None」。
_MISSING = object()


class SaveError(ValueError):
    """提交的配置值不合法；抛出时磁盘上的文件不会被改动。"""


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str
    default: object = None
    help: str = ""
    range: object = None
    choices: tuple = ()
    allow_empty: bool = True


@dataclass(frozen=True)
class Section:
    title: str
    fields: tuple = ()


@dataclass(frozen=True)
class Declaration:
    source: str
    file: object  # plugin_file 来源的文件名；bot_config 来源为 None
    sections: tuple = ()
    pages: tuple = ()


def load_declaration(plugin_dir):
    """读取插件目录的 plugin.json；没有 plugin.json 或没有 config 段时返回 None。"""
    plugin_dir = Path(plugin_dir)
    name = plugin_dir.name
    manifest_path = plugin_dir / MANIFEST_NAME
    if not manifest_path.is_file():
        return None
    manifest = json.loads(manifest_path.read_text(encoding="utf-8-sig"))
    if not isinstance(manifest, dict):
        raise ValueError(f"插件 {name} 的 plugin.json 必须是 JSON 对象")
    config = manifest.get("config")
    if config is None:
        return None
    if not isinstance(config, dict):
        raise ValueError(f"插件 {name} 的 config 必须是 JSON 对象")
    return _parse_declaration(name, config)


def read_values(decl, *, plugin_dir, bot_config):
    """按声明读出当前值（点路径键 → 值）；键缺失时用声明的默认值。"""
    root = _source_root(decl, Path(plugin_dir), bot_config)
    values = {}
    for field in _iter_fields(decl):
        value = _get_path(root, field.key, _MISSING)
        values[field.key] = field.default if value is _MISSING else value
    return values


def validate_values(decl, values):
    """校验并规范化提交的值；只处理声明覆盖到的键，失败抛 SaveError。"""
    if values is None:
        values = {}
    if not isinstance(values, Mapping):
        raise SaveError("提交的配置必须是键值对象")
    clean = {}
    for field in _iter_fields(decl):
        if field.key in values:
            clean[field.key] = _normalize(field, values[field.key])
    return clean


def write_values(decl, values, *, plugin_dir, bot_config_path):
    """校验后按声明原子写回；返回写回后的完整配置对象。校验失败时不动磁盘。"""
    clean = validate_values(decl, values)
    plugin_dir = Path(plugin_dir)
    if decl.source == BOT_CONFIG_SOURCE:
        path = Path(bot_config_path)
        data = _read_json(path)
        plugins = data.get("plugins")
        if not isinstance(plugins, dict):
            plugins = {}
            data["plugins"] = plugins
        root = plugins.get(plugin_dir.name)
        if not isinstance(root, dict):
            root = {}
            plugins[plugin_dir.name] = root
    else:
        path = plugin_dir / decl.file
        data = _read_json(path)
        root = data
    for key, value in clean.items():
        _set_path(root, key, value)
    _atomic_write(path, data)
    return data


def _parse_declaration(name, config):
    source = config.get("source")
    if source not in SOURCES:
        raise ValueError(f"插件 {name} 的 config.source 应为 {' 或 '.join(SOURCES)}")
    file = None
    if source == PLUGIN_FILE_SOURCE:
        file = config.get("file")
        if not isinstance(file, str) or not file.strip() or "\0" in file:
            raise ValueError(f"插件 {name} 的 config.file 应为 plugin_file 来源的配置文件名")
        file = file.strip()
    raw_sections = config.get("sections", [])
    if not isinstance(raw_sections, list):
        raise ValueError(f"插件 {name} 的 config.sections 应为列表")
    sections = tuple(_parse_section(name, raw) for raw in raw_sections)
    pages = tuple(_parse_pages(name, config.get("pages")))
    return Declaration(source, file, sections, pages)


def _parse_section(name, raw):
    if not isinstance(raw, dict):
        raise ValueError(f"插件 {name} 的 config.sections 每一项应为对象")
    title = raw.get("title", "")
    if not isinstance(title, str):
        raise ValueError(f"插件 {name} 的 config.sections 每一项的 title 应为字符串")
    raw_fields = raw.get("fields", [])
    if not isinstance(raw_fields, list):
        raise ValueError(f"插件 {name} 的分组「{title}」的 fields 应为列表")
    return Section(title, tuple(_parse_field(name, item) for item in raw_fields))


def _parse_field(name, raw):
    if not isinstance(raw, dict):
        raise ValueError(f"插件 {name} 的配置字段应为对象")
    key = raw.get("key")
    if not isinstance(key, str) or not key.strip():
        raise ValueError(f"插件 {name} 的配置字段缺少合法的 key")
    key = key.strip()
    kind = raw.get("type")
    if kind not in FIELD_TYPES:
        raise ValueError(
            f"插件 {name} 的字段 {key} 类型不受支持：{kind!r}；应为 {'、'.join(FIELD_TYPES)} 之一")
    label = raw.get("label")
    label = label.strip() if isinstance(label, str) and label.strip() else key
    help_text = raw.get("help", "")
    help_text = help_text if isinstance(help_text, str) else str(help_text)
    span = raw.get("range")
    if span is not None:
        span = _parse_range(name, key, span)
    choices = ()
    if kind == "enum":
        raw_choices = raw.get("choices")
        if not isinstance(raw_choices, list) or not raw_choices:
            raise ValueError(f"插件 {name} 的字段 {key} 是 enum，必须提供非空的 choices 列表")
        choices = tuple(raw_choices)
    allow_empty = raw.get("allow_empty", True)
    if type(allow_empty) is not bool:
        raise ValueError(f"插件 {name} 的字段 {key} 的 allow_empty 应为布尔值")
    return Field(key, label, kind, raw.get("default"), help_text, span, choices, allow_empty)


def _parse_range(name, key, span):
    if (not isinstance(span, list) or len(span) != 2
            or any(type(bound) not in (int, float) for bound in span) or span[0] > span[1]):
        raise ValueError(f"插件 {name} 的字段 {key} 的 range 应为 [最小值, 最大值] 两个数字")
    return (span[0], span[1])


def _parse_pages(name, raw):
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise ValueError(f"插件 {name} 的 config.pages 应为对象列表，每项形如 {{\"key\": ..., \"title\": ...}}")
    pages = []
    for item in raw:
        if (not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]
                or not isinstance(item.get("title"), str) or not item["title"]):
            raise ValueError(f"插件 {name} 的 config.pages 每项应为含 key 与 title 的非空字符串")
        pages.append(dict(item))
    return pages


def _iter_fields(decl):
    for section in decl.sections:
        for field in section.fields:
            yield field


def _source_root(decl, plugin_dir, bot_config):
    if decl.source == BOT_CONFIG_SOURCE:
        plugins = (bot_config or {}).get("plugins") if isinstance(bot_config, Mapping) else None
        options = plugins.get(plugin_dir.name) if isinstance(plugins, Mapping) else None
        return options if isinstance(options, Mapping) else {}
    return _read_json(plugin_dir / decl.file)


def _read_json(path):
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return {}
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError(f"配置文件必须是 JSON 对象：{path}")
    return data


def _get_path(root, key, default=None):
    node = root
    for part in key.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return default
        node = node[part]
    return node


def _set_path(root, key, value):
    parts = key.split(".")
    node = root
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def _normalize(field, value):
    kind, key = field.type, field.key
    if kind == "text":
        if value is None:  # 配置文件里真实存在 null，视同空值
            value = ""
        if not isinstance(value, str):
            raise SaveError(f"字段 {key} 应为字符串")
        if not value and not field.allow_empty:
            raise SaveError(f"字段 {key} 应为非空字符串")
        return value
    if kind == "password":
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise SaveError(f"字段 {key} 应为字符串")
        if not value and not field.allow_empty:
            raise SaveError(f"字段 {key} 应为非空字符串")
        return value
    if kind == "int":
        if type(value) is not int:
            raise SaveError(f"字段 {key} 应为整数")
        return _check_range(field, value, "整数")
    if kind == "float":
        if type(value) not in (int, float):
            raise SaveError(f"字段 {key} 应为数值")
        return _check_range(field, float(value), "数值")
    if kind == "bool":
        if type(value) is not bool:
            raise SaveError(f"字段 {key} 应为布尔值")
        return value
    if kind == "enum":
        if not any(type(value) is type(choice) and value == choice for choice in field.choices):
            texts = "、".join(_text(choice) for choice in field.choices)
            raise SaveError(f"字段 {key} 应为 {texts} 之一")
        return value
    if kind == "map":
        if not isinstance(value, dict):
            raise SaveError(f"字段 {key} 应为键值表")
        for entry_key, entry_value in value.items():
            if (not isinstance(entry_key, str) or not entry_key
                    or not isinstance(entry_value, str) or not entry_value):
                raise SaveError(f"字段 {key} 的键与值都必须是非空字符串")
        return dict(value)
    try:
        json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        raise SaveError(f"字段 {key} 应为可 JSON 序列化的值") from None
    return value


def _check_range(field, value, word):
    if field.range is None:
        return value
    low, high = field.range
    if value < low or value > high:
        raise SaveError(f"字段 {field.key}（{field.label}）应为 {_text(low)}–{_text(high)} 的{word}")
    return value


def _text(value):
    return value if isinstance(value, str) else str(value)


def _atomic_write(path, data):
    """原子写：同目录临时文件 → fsync → 替换，避免中断留下半份 JSON。"""
    path = Path(path)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}_", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            output.write(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
