# bga_plugin 实施计划

> **For agentic workers:** 本计划由单个实现者按任务顺序执行；每个任务末尾都有可独立核对的结果。
> 本项目**不新增自动化测试套件**（范围约束见设计文档 §13），因此任务里的核对步骤是「编译/加载检查」与「实测」而不是单元测试。

**Goal:** 在 `plugins/bga_plugin/` 交付一个独立 git 仓库的微信插件，提供 6 个 BGA 查询命令。

**Architecture:** 插件目录本身是一个 Python 包（框架按包加载，参照 `plugins/idiom_chain/` 的相对导入做法）。网络层、数据层、命令层分包；用户命令在其插件线程内同步执行，每日抓取由 `on_load` 起的后台线程完成，两者共用同一个 BGA 会话与一把限速锁。

**Tech Stack:** Python 3.13 标准库（`urllib`、`http.cookiejar`、`sqlite3`、`threading`），不新增第三方依赖。

**Spec:** `.omo/drafts/2026-10-16-bga-plugin-design.md`（接口细节见 `.omo/drafts/bga-api.md`）。实现者必须先读这两份。

## Global Constraints

- 插件目录名 `bga_plugin`（框架 `validate_name` 只允许 `[a-z][a-z0-9_]{0,63}`，**不含连字符**）。
- 每次发送正文必须是 1–1023 个 UTF-8 字节（`wxbot_framework/sdk.py`），超限会抛错；渲染时必须按字节截断。
- 只使用 Python 标准库；不新增 `requirements.txt` 依赖。
- 请求基址固定 `https://en.boardgamearena.com`；**不得请求 `/player` 与 `/playerstat`**（robots.txt 禁止）。
- 除用户命令外不产生任何自发消息。
- 仓库内不得出现账号密码：`bga_config.json` 必须被 gitignore。
- 修改主仓库的唯一一处是 `.gitignore` 增加 `plugins/bga_plugin/`。

## Review Focus

以下是最容易被使用者碰到、但设计文档没明说的输入，实现者在对应任务里各写一条防呆：

1. 群 ID 没配置游戏、且没配 `default_game` 时发 `/bga排行榜` —— 期望得到「请先配置本群桌游」而不是报错或空白。
2. `default_game` 写了一个不存在的短名（如 `yatzyy`）—— 期望得到「找不到这个桌游」而不是崩栈。
3. `/bga绑定 12abc`、`/bga绑定`（无参数）—— 期望得到用法提示而不是异常。
4. 昵称含 emoji 或很长的中文昵称，导致排行榜 5 行超出 1023 字节 —— 期望截断后仍能发送。
5. BGA 接口临时报错（超时 / `code:806` / HTTP 5xx）—— 期望回复「暂时查不到，请稍后再试」，不抛栈、不漏到其他插件。

---

## 文件结构

```
plugins/bga_plugin/                # 独立 git 仓库
  .gitignore                       # bga_config.json / __pycache__ / *.sqlite3* / .data/
  plugin.json                      # manifest
  __init__.py                      # Plugin 类：装配 + on_load/on_message/on_unload
  bga_config.example.json          # 配置模板（入库）
  README.md                        # 使用说明（命令、配置、限制）
  docs/design.md                   # 由 .omo/drafts/2026-10-16-bga-plugin-design.md 拷入
  docs/bga-api.md                  # 由 .omo/drafts/bga-api.md 拷入
  bga/__init__.py                  # 空
  bga/config.py                    # 读取并校验 bga_config.json
  bga/store.py                     # sqlite：绑定、快照、游戏名表、meta
  bga/session.py                   # 会话引导 / 登录 / 限速 / 806 自愈 / request_json
  bga/client.py                    # 各接口薄封装 + TTL 缓存
  bga/snapshot.py                  # 每日抓取线程
  bga/commands.py                  # 命令解析、渲染、按字节截断
```

---

### Task 1: 仓库与脚手架

**Files:**
- Create: `plugins/bga_plugin/plugin.json`
- Create: `plugins/bga_plugin/.gitignore`
- Create: `plugins/bga_plugin/bga_config.example.json`
- Create: `plugins/bga_plugin/bga/__init__.py`（空文件）
- Create: `plugins/bga_plugin/README.md`
- Create: `plugins/bga_plugin/docs/design.md`、`plugins/bga_plugin/docs/bga-api.md`（拷自设计/接口文档）
- Modify: `.gitignore`（主仓库根，追加 `plugins/bga_plugin/`）

- [ ] **Step 1: 建目录并在新目录内初始化独立仓库**

`plugins/bga_plugin/` 下执行 `git init`。主仓库 `.gitignore` 追加 `plugins/bga_plugin/`。

- [ ] **Step 2: 写 manifest**

`plugin.json` 内容固定为：

```json
{
  "api": "v1",
  "description": "Board Game Arena 排行榜与积分查询",
  "requires": ["text.receive", "text.send"]
}
```

（不要加 `llm` 字段——加了 `main.py --check` 会要求大模型配置。）

- [ ] **Step 3: 写插件 `.gitignore` 与配置模板**

`.gitignore` 内容：`bga_config.json`、`__pycache__/`、`*.py[cod]`、`*.sqlite3*`。

`bga_config.example.json`：

```json
{
  "account": { "email": "", "password": "" },
  "groups": { "12345678@chatroom": "yatzy" },
  "default_game": null,
  "ranking_depth": 1000,
  "cache_seconds": 300,
  "request_interval_seconds": 1.0,
  "daily_snapshot_hour": 4,
  "database_file": "bga.sqlite3"
}
```

（示例里的群 ID 与账号必须是占位值，不得是真实数据。）

- [ ] **Step 4: 拷贝文档并写 README**

README 覆盖：6 个命令、配置项含义、`groups` 用「群 ID → 桌游短名或数字 ID」、限制（前 1000 名之外查不到总积分、趋势需连续运行 7 天、需要登录才能查完整资料）。

- [ ] **Step 5: 核对**

`python main.py --check` 应打印出 `"name": "bga_plugin"` 一行且整体通过；`git -C plugins/bga_plugin status` 能正常工作。

---

### Task 2: `bga/config.py`

**Files:** Create `plugins/bga_plugin/bga/config.py`

**Interfaces:**
- Produces:
  - `class ConfigError(ValueError)`
  - `@dataclass(frozen=True) class Config`，字段：`account_email: str`、`account_password: str`、`groups: dict[str, str]`、`default_game: str | None`、`ranking_depth: int`、`cache_seconds: int`、`request_interval_seconds: float`、`daily_snapshot_hour: int`、`database_path: Path`
  - `Config.has_login -> bool`（email 与 password 都非空）
  - `Config.game_for(chat_id: str) -> str | None`（先查 `groups`，再退回 `default_game`）
  - `Config.all_games() -> list[str]`（`groups` 取值去重后排序）
  - `load_config(path: Path | None) -> Config`（文件不存在时抛 `ConfigError`）

**约束：** `ranking_depth` 取 1–10000；`cache_seconds` 取 0–3600；`request_interval_seconds` 取 0.5–60；`daily_snapshot_hour` 取 0–23。越界抛 `ConfigError` 并带出字段名。`database_path` 相对插件目录解析。

- [ ] **Step 1: 实现上述接口与校验**
- [ ] **Step 2: 核对** —— 用 `python -c` 分别喂合法配置与越界配置，确认前者成功、后者抛 `ConfigError`

---

### Task 3: `bga/store.py`

**Files:** Create `plugins/bga_plugin/bga/store.py`

**Interfaces:**
- Produces `class Store`：
  - `Store(path: Path)`
  - `bind(wxid: str, player_id: str) -> None`（按 wxid 覆盖）
  - `bound_player(wxid: str) -> str | None`
  - `all_bindings() -> list[tuple[str, str]]`
  - `save_prestige(day: str, player_id: str, name: str, country: str, prestige: float, rank: int) -> None`（同日同人覆盖）
  - `save_arena(day: str, player_id: str, game: str, name: str, points: float, rank: int) -> None`
  - `prestige_trend(player_id: str, days: int = 7) -> list[tuple[str, float, int]]`（按 day 升序，最多 days 条）
  - `arena_trend(player_id: str, game: str, days: int = 7) -> list[tuple[str, float, int]]`
  - `player_label(player_id: str) -> tuple[str, str] | None`（最近一次已知的昵称、国籍）
  - `cache_game(name: str, game_id: str) -> None` / `game_id(name: str) -> str | None`
  - `meta_get(key: str) -> str | None` / `meta_set(key: str, value: str) -> None`
  - `close() -> None`

**约束：** 单连接 + `threading.Lock`（消息线程与后台线程共用）；建表用 `CREATE TABLE IF NOT EXISTS`；表结构按设计文档 §8，另加 `game(name TEXT PRIMARY KEY, game_id TEXT)` 与 `meta(key TEXT PRIMARY KEY, value TEXT)`。日期 `day` 用北京时间 `YYYY-MM-DD`。

- [ ] **Step 1: 实现 Store**
- [ ] **Step 2: 核对** —— 在临时目录建库，依次 bind/save/query，确认覆盖语义与趋势返回顺序

---

### Task 4: `bga/session.py`

**Files:** Create `plugins/bga_plugin/bga/session.py`

**Interfaces:**
- Consumes: `Config`
- Produces:
  - `class SessionError(RuntimeError)`
  - `class Session`：`Session(config: Config)`、`request_json(method: str, path: str, params: dict | None = None) -> dict`、`close() -> None`

**行为：**
1. 懒引导：首次请求前 `GET /leaderboard`，用 `http.cookiejar.CookieJar` 存 `PHPSESSID`，从响应文本用正则 `requestToken:\s*'([0-9a-f]{64})'` 取令牌；取不到抛 `SessionError`。
2. 若 `config.has_login` 且尚未登录，走设计文档 §10.5 的登录流程；登录失败只记日志并把会话标记为未登录，**不抛给调用方**（匿名接口仍应可用）。
3. 请求格式：`POST {BASE}/{path}.html`，`Content-Type: application/x-www-form-urlencoded`，头 `X-Request-Token`；返回 JSON 解析后若 `status != 1` 且 `code == 806`，则重新引导（必要时重新登录）后重试**一次**；仍是 806 抛 `SessionError`。
4. 限速：所有请求（含引导与登录）经过同一把锁，两次请求间隔不小于 `config.request_interval_seconds`。
5. 超时 15 秒；网络错误统一包成 `SessionError`，**不得**把 `urllib` 异常泄漏出去。

- [ ] **Step 1: 实现 Session**
- [ ] **Step 2: 实测核对（匿名，不需要账号）** —— 跑一小段脚本：引导成功、`POST /prestige/prestige/seeMorePrestige.html`（`start=1`）返回 `data` 数组且首条含 `prestige`、`rank`。把实际输出贴进完成汇报。

---

### Task 5: `bga/client.py`

**Files:** Create `plugins/bga_plugin/bga/client.py`

**Interfaces:**
- Consumes: `Session`、`Config`、`Store`
- Produces `class Client`：
  - `current_season() -> int`
  - `game_ranking(game: str, *, mode: str = "arena", start: int = 0) -> list[dict]`（返回 `ranks` 列表）
  - `prestige_page(start: int) -> list[dict]`（`start` 为 1 基排名）
  - `find_players(keyword: str) -> list[dict]`
  - `arena_around_player(player_id: str, game: str, season: int) -> dict | None`（返回首条 rank 记录，无则 `None`）
  - `player_infos(player_id: str) -> dict | None`（未登录或失败返回 `None`）
  - `resolve_game_id(game: str) -> str | None`（纯数字直接返回；否则查 `Store.game_id`，未命中则抓 `GET /gamelist` 页解析内嵌的 `game_list` 数组后写缓存）
  - `find_prestige(player_id: str, depth: int) -> tuple[float, int, str, str] | None`（在 `depth` 名内逐页查找，命中返回 `(prestige, rank, name, country)`）

**约束：** 页面级缓存（`current_season` 与 `resolve_game_id`）走 TTL，避免每次命令都抓页面。所有方法只返回结构化数据，不做文案。

- [ ] **Step 1: 实现 Client**，查询接口全部用 `Session.request_json`
- [ ] **Step 2: 实测核对（匿名）** —— 用 `yatzy` 跑 `resolve_game_id` → `current_season` → `game_ranking(mode="arena")` 各一次，确认返回真实数据

---

### Task 6: `bga/snapshot.py`

**Files:** Create `plugins/bga_plugin/bga/snapshot.py`

**Interfaces:**
- Consumes: `Client`、`Store`、`Config`
- Produces `class SnapshotWorker`：`SnapshotWorker(client, store, config, logger)`、`start() -> None`、`stop() -> None`

**行为：**
- 守护线程，每 60 秒检查一次：北京时间当天日期 > `Store.meta_get("last_snapshot_day")` 且当前小时 >= `daily_snapshot_hour` 时执行一轮。
- 一轮内容：① 抓总积分榜前 `ranking_depth` 名（每页 10 条，1 基），逐条 `save_prestige`；② 对 `Store.all_bindings()` 的每个玩家 × `Config.all_games()` 的每个游戏，用 `arena_around_player` 写 `save_arena`（其中游戏先经 `resolve_game_id`）。
- 任一步失败只记日志，不终止线程；一轮结束后 `meta_set("last_snapshot_day", 当天)`。
- `stop()` 置事件并 join（超时 3 秒），允许在抓取中途退出。

- [ ] **Step 1: 实现 SnapshotWorker**
- [ ] **Step 2: 核对** —— 用小的 `ranking_depth`（如 20）在临时库上手工调一次「一轮」函数，确认落库条数与 `meta` 更新；不必等到凌晨

---

### Task 7: `bga/commands.py` + `__init__.py`

**Files:** Create `plugins/bga_plugin/bga/commands.py`、`plugins/bga_plugin/__init__.py`

**Interfaces:**
- `commands.py` 产出：
  - `COMMANDS: frozenset[str]` = `{"/bga排行榜", "/bga积分榜", "/bga我的信息", "/bga绑定", "/bga查询", "/bga趋势", "/bga帮助"}`
  - `handle(message, ctx, deps) -> str | None`（返回要回复的文本；非本插件命令返回 `None`）
  - `clip(text: str, limit: int = 1023) -> str`（按 UTF-8 字节截断）
- `deps` 是一个简单命名空间，字段：`config: Config | None`、`client: Client | None`、`store: Store | None`、`logger`

**`__init__.py` 的 `Plugin`：**
- `on_load(ctx)`：读 `plugins/bga_plugin/bga_config.json`（路径由 `Path(__file__).parent` 推导）。配置缺失或非法时：记 warning，`self.deps = 空 deps`，插件仍然加载成功（命令统一回「BGA 插件尚未配置」，不抛异常）。配置正常则建 `Store`、`Session`、`Client`，并 `SnapshotWorker.start()`。
- `on_message(message, ctx)`：忽略 `message.type != 1`；调用 `handle`，有返回值则 `ctx.reply(message, clip(text))`；`handle` 内部捕获所有异常并返回友好文案。
- `on_unload(ctx)`：`SnapshotWorker.stop()`、`Session.close()`、`Store.close()`。

**渲染（定稿，实现者照抄字段顺序）：**

| 命令 | 输出 |
|---|---|
| `/bga排行榜` | `🏆 <游戏> 第<赛季>赛季竞技场榜` 换行，随后最多 5 行 `N. <昵称> [<国家代码>] <积分>` |
| `/bga积分榜` | `🥇 总积分榜（前 5 名）`，随后最多 5 行 `N. <昵称> [<国家代码>] <总积分>` |
| `/bga我的信息` | `BGA 玩家：<昵称> [<国家代码>]（ID <id>）` / `总积分：<积分>（第 <名次> 名）` 或 `总积分：未在榜上（前 <depth> 名之外）` / `<游戏> 竞技场：<积分>（第 <名次> 名）` |
| `/bga绑定 <ID>` | `已绑定 BGA 玩家 <ID>（<昵称>）。` 或 `已绑定 BGA 玩家 <ID>；暂时查不到该 ID 的资料。` |
| `/bga查询 <关键字>` | `找到 N 个玩家：`，随后每行 `N. <昵称>（ID <id>）`；登录可用时补 `，<国籍>` |
| `/bga趋势` | `📈 近 7 天变化（<起始日> → <今日>）`，随后 `总积分：<起> → <止>（<差值>，排名 <起> → <止>）` 与 `<游戏> 竞技场：...` 两行 |
| 未配置游戏 | `本群尚未配置桌游，请在 bga_config.json 的 groups 里为本群指定一个游戏。` |
| 未绑定 | `请先用 /bga绑定 <你的 BGA 玩家ID> 绑定账号。` |
| 数据不足 | `趋势需要连续记录 7 天，当前只有 <N> 天数据。` |
| 接口失败 | `暂时查不到数据，请稍后再试。` |

**缓存：** `commands.py` 内用 `dict` 存 `(命令, 群/关键字) → (到期 monotonic, 文本)`，TTL = `config.cache_seconds`；只缓存 `/bga排行榜`、`/bga积分榜`、`/bga我的信息`、`/bga查询` 的成功结果。**快照只在缓存未命中（真正取数）时写**。

- [ ] **Step 1: 实现 `commands.py` 与 `__init__.py`**
- [ ] **Step 2: 核对** —— `python main.py --check` 通过；随后用桩客户端（不发网络请求）依次触发上表每一行输出，确认文案与截断正确

---

### Task 8: 配置与端到端核对

- [ ] **Step 1: 生成一份本地 `bga_config.json`**，`groups` 里填一个真实群 ID → `yatzy`（这一步的群 ID 由用户提供；未提供时跳过真实群，改用 `default_game: "yatzy"`）
- [ ] **Step 2: 起机器人实测** `python main.py`，在微信里依次发 6 个命令，确认每个都能收到合理回复
- [ ] **Step 3: 登录路径实测** —— 这一步**需要用户把 BGA 账号密码填进 `bga_config.json`**；若尚未提供，明确记录为「未验证」，不得声称已可用
- [ ] **Step 4: 提交** —— 在插件仓库内提交；主仓库只提交 `.gitignore` 的改动

---

## 不做的事

- 不新增单元测试套件、不跑回归、不改主仓库框架代码。
- 不实现 `/player`、`/playerstat` 相关抓取。
- 不实现多账号、代理、验证码处理。
- 不把 BGA 命令加入框架的 `CONTROL_COMMANDS`（那会改变其他插件行为）。

## Self-Review 记录

- 设计文档 §6–§10 的每一项都能对应到 Task 1–8；§13 的「接口连通性」落在 Task 4.2 / 5.2 / 8.x。
- 类型一致性：`Config.game_for` / `Store` 的方法名在 Task 3、5、6、7 中一致引用。
- Review Focus 的 5 条分别落在 Task 2（配置校验）、Task 5（游戏名解析）、Task 7（用法提示与截断）、Task 4/7（异常包装）。
- 未覆盖：登录路径与登录后的 `getPlayerInfos` 字段，依赖用户账号，已显式标记为「未验证」。
