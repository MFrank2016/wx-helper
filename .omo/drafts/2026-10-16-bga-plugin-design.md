# bga_plugin 设计文档

日期：2026-10-16
状态：待用户确认（确认后本文件移入新插件仓库）

> 说明：本文是设计文档，不是工作计划。计划在本文获批后另写。
> 待落地位置：`plugins/bga_plugin/docs/design.md`（插件仓库内）。

---

## 1. 意图与目的

给微信机器人加一个 Board Game Arena（BGA）查询插件：群友想看某个桌游当前的赛季排行榜、想看总积分榜、想看自己的 BGA 资料与排名、想绑定自己的 BGA 账号、想按关键字搜人，以及想知道自己最近一周积分和排名的变化。

使用者是机器人所在群里的普通群友，通过微信发一条命令拿结果，不需要打开 BGA 网站。

## 2. 目标（Must have）

1. `/bga排行榜` —— 看**当前群所设桌游**的当前赛季排行榜。
2. `/bga积分榜` —— 看当前群所设桌游的**全时段（不分赛季）**排行榜。
3. `/bga我的信息` —— 看自己的 BGA 资料：玩家 ID、昵称、**当前群所设桌游的 ELO（总积分）与全时段名次**、该游戏竞技场积分与排名。每次查询写入一条当期竞技场快照。
4. `/bga绑定 <玩家ID>` —— 把当前微信用户绑定到一个 BGA 玩家 ID；每个微信用户只能绑一个。
5. `/bga查询 <关键字>` —— 按关键字模糊搜玩家，输出昵称、玩家 ID（不补国籍）。
6. `/bga趋势` —— 看过去 7 天**当前群所设桌游的 ELO** 和竞技场积分的变动，以及排名变动。

## 3. 非目标（Must NOT have）

- 不做 BGA Studio / 游戏开发相关功能。
- 不做发帖、聊天、对局操作、对战匹配等写操作，只读查询。
- 不做 Web 管理界面、不做导出报表。
- 不做多 BGA 账号轮换、不做代理池、不做验证码/反爬绕过。
- 不做通用「BGA 客户端库」，只为这 6 个命令服务。
- 不抓取 `robots.txt` 明确禁止的路径——唯一例外是用户 2026-10-16 **追加知情授权**的 `player?id=<数字ID>&section=prestige` 玩家主页页面（见 §9）。
- 不修改主仓库的框架代码（除新增插件的配置样例与文档外）。

## 4. 反目标（反目标：即使技术可用也不做）

- 不为了让数据好看而伪造或平滑排名/积分。
- 不在没有真实接口返回值的情况下猜测字段并写死。
- 不以「先跑起来」为名把 BGA 账号密码写进任何入库文件。

## 5. 约束

| 约束 | 来源 |
|---|---|
| 插件目录名只能用 `[a-z][a-z0-9_]{0,63}`，**不能含连字符** | `wxbot_framework/config.py: validate_name` |
| 微信正文每次发送 1–1023 个 UTF-8 字节，超限发送会抛错 | `wxbot_framework/sdk.py: send_text` |
| 插件回调在其**独立线程内串行**执行；同步 HTTP 会阻塞该插件后续消息 | `wxbot_framework/plugins.py: PluginRecord._run` |
| 插件通过 `ctx` 拿配置/日志/发送；不发文件、不发图片 | `wxbot_framework/sdk.py: PluginContext` |
| 依赖最小化：框架仅用标准库做 HTTP（`urllib`）与 sqlite | `wxbot_framework/llm.py`, `wxbot_framework/members.py` |
| 插件为**独立 git 仓库**，主仓库 `.gitignore` 忽略其目录 | 用户决定（2026-10-16） |

## 6. 形态与位置

```
plugins/bga_plugin/                # 独立 git 仓库；主仓库 .gitignore 忽略
  plugin.json                      # {"api":"v1","description":...,"requires":["text.receive","text.send"]}
  __init__.py                      # Plugin 类：on_load / on_message / on_unload
  bga_config.example.json          # 配置模板（入库）
  bga_config.json                  # 本地实际配置，含账号密码（gitignore）
  bga/                             # 包内实现（相对导入，参照 idiom_chain/judge.py 的做法）
    __init__.py
    config.py                      # 读取 bga_config.json 并校验
    session.py                     # 会话引导、登录、token、806 重建
    client.py                      # 各数据接口的薄封装
    store.py                       # sqlite：绑定关系 + 每日快照
    snapshot.py                    # 后台每日抓取线程
    commands.py                    # 命令解析与文本渲染
  docs/
    design.md                      # 本文件
    bga-api.md                     # 接口文档
  README.md
  .gitignore
```

主仓库 `.gitignore` 追加一行 `plugins/bga_plugin/`。

## 7. 配置

本地文件 `plugins/bga_plugin/bga_config.json`（gitignored）：

```json
{
  "account": { "email": "you@example.com", "password": "..." },
  "groups": { "12345678@chatroom": "arknova" },
  "default_game": null,
  "ranking_depth": 1000,
  "cache_seconds": 300,
  "request_interval_seconds": 1.0,
  "daily_snapshot_hour": 4
}
```

- `groups`：群 ID → 桌游短名。**每个群各配一个桌游**（用户决定）。群里没配该群时，看 `default_game`；两者都没有则回复提示。
- `ranking_depth`：**历史遗留字段，现已不使用**（原「每天抓总积分榜的深度」，默认 1000）。保留是为兼容既有配置文件；「总积分」已改为当前群桌游的 ELO。
- `cache_seconds`：排行榜与「我的信息」的内存缓存时长，默认 300 秒（即 5 分钟，用户要求）。
- `account`：BGA 登录凭据（用户决定登录；存本地配置）。
- 插件启用状态仍由主仓库 `bot_config.json` 的 `plugins.bga_plugin.enabled` 控制（框架机制）。

## 8. 数据与存储

sqlite 落盘在插件目录内的 `plugins/bga_plugin/bga.sqlite3`（相对插件目录解析，文件名由配置项 `database_file` 控制）。该文件被插件自身的 `.gitignore` 忽略，主仓库也整体忽略 `plugins/bga_plugin/`，因此不会进入任何仓库。

表：

- `binding(wxid TEXT PRIMARY KEY, bga_player_id TEXT NOT NULL, bound_at REAL NOT NULL)`
  —— 一个微信用户一行，重复执行绑定即覆盖。
- `prestige_snapshot(day TEXT, player_id TEXT, name TEXT, country TEXT, prestige REAL, rank INTEGER, PRIMARY KEY(day, player_id))`
  —— **历史遗留表，已不再写入**（原「按天一条总积分快照」）。旧表与历史数据保留，不迁移、不删除。
- `arena_snapshot(day TEXT, player_id TEXT, game TEXT, name TEXT, points REAL, rank INTEGER, PRIMARY KEY(day, player_id, game))`
  —— 按天 + 游戏一条竞技场快照。`points` 存的是**解码后的分数**（见 §10.1 的竞技场口径），
  不是 BGA 原始的 `arena` 复合值（如 `501.1817`）；解码不出的日子不落库。
- `elo_snapshot(day TEXT, player_id TEXT, game TEXT, name TEXT, points REAL, rank INTEGER, PRIMARY KEY(day, player_id, game))`
  —— 按天 + 玩家 + 游戏一条 ELO 快照。`points` 存的是 **ELO 原始值口径**（如 `1872`），
  不是页面显示值（`572`）——存原始值便于日后改口径/求差。数值来源是**该玩家主页「名誉」
  区块的显示值**（见 §10.1、§10.4）：落库前用 `bga_sdk/elo.py` 的 `elo_raw_from_display()`
  （`显示值 + 1300`）还原成原始值口径，满足 `elo_display(还原值) == 显示值`（显示与差值口径完全一致）。
  `rank` 存该桌游**全时段名次**，**`0` 表示「未在榜上」**（BGA 对未上榜者返回 `null`）。
  抓取范围是**每个已绑定玩家 × 配置里出现过的每个桌游**——ELO 用**该玩家主页的一次页面请求**
  拿到其全部桌游，名次逐游戏查。

`day` 用北京时间日期字符串（沿用框架 `members.py` 的北京时区做法）。

## 9. 接口来源与合规

接口清单与实测结论见 `bga-api.md`。要点：

- 域名统一用 `https://en.boardgamearena.com`（主域会 302 到语言子域）。
- 需 `PHPSESSID` Cookie + `X-Request-Token` 请求头（除模糊搜索接口）。
- 匿名即可用的接口：赛季榜、指定赛季榜、总积分榜、赛季编号、模糊搜索、玩家世界排名、玩家周边竞技场排名。
- **`/player/player/getPlayerInfos.html`（按 ID 查完整资料）匿名返回 HTTP 500**，登录后可用；其真实返回字段已于 2026-10-16 用已登录浏览器实测确认（见 `bga-api.md` §3.1 与本文 §12.1）。
- `robots.txt` 明确 `Disallow: /player` 与 `Disallow: /playerstat`。用户于 2026-10-16 明确决定：**保留全部功能**。因此本插件不请求 `/player`、`/playerstat` 两个**页面路径**，但会调用 `/player/player/findplayer.html`（模糊搜人）与 `/player/player/getPlayerInfos.html`（登录后按 ID 查资料）这两个数据接口——它们字面上落在 `robots.txt` 的 `Disallow: /player` 前缀规则下。该取舍已向用户说明并获得同意，属于知情决定，不是疏漏。
- **2026-10-16 追加授权**：为拿到「任意玩家的每游戏 ELO」，用户**明知** `/player` 页面路径在 `robots.txt` 禁止列表内，仍追加授权抓取**唯一一个页面形态** `GET /player?id=<纯数字ID>&section=prestige`（玩家主页「名誉」区块，服务端把每个桌游的 ELO 直出进 HTML）。除该形态外，`/player`、`/playerstat` 页面仍不请求。理由与风险见 §10.1、§11；实测结构见 `bga-api.md` §3。
- 没有公开的频率限制文档，`request_interval_seconds` 默认 1 秒，保守发送。

## 10. 行为设计

### 10.1 命令

统一前缀 `/bga`，后面直接接中文词（用户选择「斜杠+中文混合」）。命令名按消息首个空白字段匹配，与框架 `AdminPolicy.command_of` 的取词方式一致。

| 命令 | 输出 | 数据来源 | 缓存 |
|---|---|---|---|
| `/bga排行榜` | 当前群桌游的当前赛季榜前若干名（名次/昵称/国籍/积分） | `getRanking.html` (mode=arena) | 300s |
| `/bga积分榜` | 当前群桌游的**全时段（不分赛季）**榜前最多 20 名（名次/昵称/国籍/分数） | `getRanking.html` (mode=elo) | 300s（按群缓存） |
| `/bga我的信息` | 玩家 ID、昵称、当前群桌游的 ELO 显示值+全时段名次、该桌游的竞技场积分+排名 | **玩家主页「名誉」区块** `player?id=<玩家ID>&section=prestige`（该玩家该桌游的 ELO 显示值）+ `lobby/lobby/getPlayerWorldRanking.html`（`isArena=0`，名次）+ `getArenaRankingAroundPlayer.html`；登录后优先 `getPlayerInfos.html` | 300s |
| `/bga绑定 <玩家ID>` | 绑定结果（能取到昵称则一并回显） | 本地格式校验（纯数字）；昵称尽力取，取不到不回滚绑定（见 §10.6） | — |
| `/bga查询 <关键字>` | 昵称 / ID（**不补国籍**，见 §10.6；返回条数由服务端决定，非固定条数） | `findplayer.html` | 300s |
| `/bga趋势` | 过去 7 天当前群桌游的 ELO、竞技场积分的起止值与差值，及排名变化 | 本地 sqlite 快照（`elo_snapshot` / `arena_snapshot`） | — |

未绑定时 `/bga我的信息`、`/bga趋势` 回复绑定指引。

**竞技场积分口径**：BGA 竞技场榜的 `arena` 字段是**复合编码值**（如 `501.1817`），不是普通数字，直接当浮点显示会丢掉真正有意义的分数。插件的输出统一显示其中的**分数**——即该值**小数点后四位**（`501.1817` → `1817`、`405.1638` → `1638`），**不带联盟名**。`/bga排行榜` 的积分列、`/bga我的信息` 的竞技场行、`/bga趋势` 的竞技场起止值与差值、以及 `arena_snapshot.points` **全部使用同一口径**；解析不到时命令里显示 `?`，快照则跳过不写该行。解码规则见 `bga-api.md` §3 的「竞技场 `arena` 字段是复合值」小节，实现见 `bga_sdk/arena.py` 的 `arena_points()`。

**全时段排行榜的分数口径**：`/bga积分榜` 取的是桌游页「排名」区块切到「不分赛季」的内容（`getRanking` 的 `mode=elo`）。接口返回的 `ranking` 是**原始 ELO**，而**页面显示的是 `floor(max(0, ELO - 1300))`**（减 1300、不为负、**截断**取整，不是四舍五入）。插件照页面口径显示；解析不到时显示 `?`。解码规则见 `bga-api.md` §3 的「ELO 显示口径」小节，实现见 `bga_sdk/elo.py` 的 `elo_display()`。`/bga积分榜` 的缓存键含 `chat_id`（依赖当前群所设桌游，不能全局共用）。

**「总积分」= 当前群所设桌游的 ELO**：`/bga我的信息` 的「总积分」一行为 `总积分：<ELO 显示值>（第 <全时段名次> 名）`；`/bga趋势` 的「总积分」一行为该桌游 ELO 的起止值/差值/名次变化。两处只对**当前群配置的桌游**取数（与 `/bga排行榜`、`/bga积分榜` 同口径）。两条由接口本身决定的限制：

1. **ELO 数值来源是「玩家主页」而非数据接口**——BGA **没有**「按玩家 ID 查 ELO 数值」的接口：`getGameRanking` 的 `data.rank` 只对**登录账号本人**有效，`getPlayerWorldRanking` 只给名次、不给数值。故改为抓**该玩家本人**的主页页面 `player?id=<玩家ID>&section=prestige`（服务端把每个桌游的 ELO 直出进 HTML，一次请求即拿到该玩家全部桌游的显示值；按玩家做 300 秒 TTL 缓存）。**因此无论绑定的是谁都能显示该玩家的 ELO**（不再有「只对登录账号本人」的限制）。解析失败、或该玩家没玩过该桌游（含页面只给「学徒」「新手」这类段位文字、无数字的情况）时，数值位置显示 `—`，**绝不抛异常或让命令失败**。
2. **名次对未上榜者为空**——`getPlayerWorldRanking`（`isArena=0` 全时段、`isArena=1` 竞技场）对未上榜/未定级者返回 `rank: null`，此时显示「未在榜上」；`elo_snapshot.rank` 用 `0` 约定表示「未在榜上」。

`/bga我的信息` 与 `/bga趋势` 不再依赖「全局总积分榜翻榜查排名」（`find_prestige` 已移除）。

### 10.2 输出长度

微信正文上限 1023 字节。`/bga排行榜` 与 `/bga积分榜` 目标输出前 20 名（逐页取数），但按**整行**装入字节预算：放不下的整行直接丢弃，并在末尾追加一行「（篇幅所限，仅显示前 N 名）」（该说明行本身也计入预算），全部放得下时不追加说明；其余命令输出定长摘要。`clip()` 作为最后一道按 UTF-8 字节截断的兜底，绝不生成超限正文。

### 10.3 响应范围

群聊与私聊都响应（与本仓库现有命令插件一致）。除命令本身外不产生任何自发消息。

### 10.4 后台抓取

`on_load` 起一个后台守护线程，`on_unload` 停止它并关闭网络资源（参照 `_template` 的 `on_unload` 约定）：

- **每日 ELO 抓取**：对**每个已绑定玩家**抓一次其主页「名誉」区块（`player?id=<ID>&section=prestige`，一次请求拿到该玩家全部桌游的 ELO 显示值），把**配置里出现过的每个桌游**的 ELO 还原成原始值口径后写 `elo_snapshot`；名次逐游戏走 `getPlayerWorldRanking`（未上榜时 `rank` 记 `0`）。逐个玩家 try/except，失败只记日志、不终止整轮。
- **每日竞技场抓取**：对**每个已绑定玩家 × 配置里出现过的每个不同桌游**（`groups` 的取值去重后的集合）抓一次该玩家本赛季的竞技场积分与排名，写 `arena_snapshot`（见 §10.6 决定）。请求量 = 绑定人数 × 不同游戏数，量级可控。
- 抓取与用户命令共用同一会话与限速器；命令不会被后台抓取阻塞（不同线程）。

### 10.5 登录与会话

以下均为 2026-10-16 用「已登录 BGA 的浏览器」实测确认的事实（原始证据见 `bga-api.md` §0.2 / §0.4 / §3.1）：

1. 引导：`GET /leaderboard` → 取 `PHPSESSID`；从页面 HTML 提取 `requestToken`。
   **token 形态随会话状态变化**：脚本式匿名会话得到的是 64 位十六进制，真实浏览器登录后的页面上只有 15 位字母数字（整页没有 64 位十六进制）。
   实现须同时兼容两种形态：收集全部候选、按出现顺序逐个试，遇到 `code:806` 换下一个候选，候选用尽再重新引导。
2. 登录：`POST /account/auth/loginUserWithPassword.html`，body 为
   `username / password / remember_me=true / request_token`，请求头带 `X-Request-Token`；
   成功判据为 `status == 1` 且 `data.success` 为真。
   （旧实现用的 `/account/account/login.html` + `form_id=loginform` 在 BGA 当前前端上已不存在，是从第三方旧仓库抄来的。）
3. 登录成功后 token 形态会变，**必须作废当前 token 并重新引导**，用登录后的页面重新取 token。
4. 会话缓存：内存中保存 Cookie 与 token，跨请求复用。
5. 自愈：遇到 `code:806` 先换候选 token，候选用尽再重新引导；仍失败则记日志并对用户回复「暂时查不到」，不抛异常。
6. 登录失败（凭据错误、风控等）只记日志并保持匿名会话运行，不抛给调用方；匿名接口仍应可用。

## 10.6 评审后补充的决定

以下 5 点由设计评审提出，处理结果如下（前 4 点为实现者可据以开工的定论，第 5 点需要用户最后配合一次实测）。

1. **竞技场快照的抓取范围**：绑定关系是按微信用户全局唯一的，而桌游是按群配置的，所以无法从绑定关系推出「这个玩家该看哪些游戏」。取**绑定玩家 × 配置中出现过的所有不同桌游**的笛卡尔积，按天各写一条 `arena_snapshot`。`/bga趋势` 在某个群里执行时，只看该群配置的桌游；私聊时看 `default_game`；两者都没有则提示先为本群配置桌游（ELO 与竞技场两部分都依赖该桌游）。

2. **`/bga绑定` 的校验强度**：匿名下没有「按玩家 ID 直达」的接口（`getPlayerInfos.html` 匿名必 500，其余接口要么只能翻榜、要么必须带 `game`），所以绑定**不做网络强校验**。只校验 ID 是纯数字；绑定立即生效，随后尽力取一次昵称回显——取到就显示「已绑定：昵称」，取不到就显示「已绑定，但暂时查不到该 ID 的资料」。不因取昵称失败而回滚。

3. **`/bga查询` 不补国籍**：`findplayer.html` 只返回 ID 和显示名，因此**不补国籍，只输出昵称与 ID**（不再调用 `getPlayerInfos.html` 取国籍，也不为查询命令额外发任何请求）；返回条数由服务端决定（实测见过 10 / 15 / 20 等不同条数，不能假定固定条数），且 **`start` / `count` 被忽略、无法翻页**。缓存按关键字存整条结果。

4. **限速与并发**：用户命令与后台抓取**共用同一个会话**（同一套 Cookie/token），但使用**同一把节流锁**——不拆分通道。理由是 `request_interval_seconds` 默认 1 秒，一条命令最多几个请求，最坏等待量级是几秒，可接受；分通道会让会话复用与重试逻辑复杂化且收益有限。后台抓取每发一个请求即释放锁并短暂让出，保证用户命令不会被上百次连续抓取整体阻塞。

5. **登录的实现与验证顺序**（需要用户配合）：登录流程在本次实现中**完整写入**（引导 → 登录表单 → 复用会话 → 806 自愈），`getPlayerInfos.html` 作为「我的信息」的优先路径，匿名路径作为兜底。但登录**无法在没有真实账号时验证**。因此：实现完成后，由用户把 BGA 账号密码填进 `plugins/bga_plugin/bga_config.json`（本地文件，不入库），再做一次连通性实测（见 §13）。在实测通过之前，文档与汇报必须写明「登录路径未经真实账号验证」，不得声称功能 3 的完整资料已可用。

## 11. 风险

| 风险 | 影响 | 应对 |
|---|---|---|
| 登录接口虽按 BGA 自己前端的调用代码改写，但**尚未用真实账号跑通** | 若凭据格式或风控环节有出入，登录仍可能失败 | 用户填入账号密码后做一次真机登录实测（见 §13） |
| 内部接口无官方承诺，随时可能变 | 某个功能突然失效 | 接口封装集中在 `client.py`，失败时回复友好文案并记日志 |
| 账号被风控/封禁 | 用户资产损失 | 保守限速、只读、不做并发冲刺；登录凭据只存本地 |
| 玩家主页「名誉」区块改版导致解析失效 | ELO 显示不出来 | 解析失败优雅降级为 `—`（不抛异常）；解析锚定 `id="elo_game_<数字ID>"`，与游戏名文本解耦（见 §10.1） |
| 玩家在该桌游未上榜/未定级 | 名次查不到 | 明确回复「未在榜上」；快照 `rank` 记 `0` |
| 趋势头 7 天不完整 | 趋势数字不可信 | 快照不足 7 天时明确标注「数据不足 N 天」 |
| robots.txt 的 `/player` 前缀规则覆盖模糊搜人、按 ID 查资料两个数据接口，以及玩家主页页面 | 合规争议 | 均已向用户说明并获得**知情同意**（`/player` 页面路径于 2026-10-16 追加授权，且仅限 `player?id=<数字ID>&section=prestige` 一种形态），保留功能（见 §9、§12.1） |

## 12. 明确留给实现者的决定

- 模块内部函数划分、命名、日志文案。
- sqlite 具体 DDL 与索引（表结构见 §8，字段可微调）。
- 排行榜默认输出几名（在 1023 字节约束内）。
- 会话重试的退避参数。

## 12.1 实测修正记录（2026-10-16）

原先记录的「`getPlayerInfos.html` 字段名靠猜」这一受控偏离**已关闭**。2026-10-16 用已登录 BGA 的浏览器直接调用该接口，拿到真实返回，据此回填真实字段名：

- 昵称字段是 `fullname`（原候选键 `name` / `display_name` / `player_name` / `username` 全部取不到，已删除）。
- 国籍字段是 `country_infos`（含稳定的 `code` 与随语言变的 `name`），不是 `country`。
- 真实资料在 `json["data"]["data"]`；外层 `data` 形如 `{"success": true, "data": {...}}`。
- 该接口另返回 `prestige`（总积分）。**按用户 2026-10-16 的决定，本次不使用它**：总积分与总排名仍只按总榜前 `ranking_depth` 名内显示，口径不变。
- **2026-10-16 后续变更**：总积分口径改为**当前群所设桌游的 ELO**（见 §8、§10.1）。原先「按总榜前 `ranking_depth` 名查找」的做法与 `find_prestige` / 每日总积分榜抓取一并移除；`getPlayerInfos` 的 `prestige` 仍未使用。
- **2026-10-16 再后续变更**：ELO 数值来源由 `lobby/lobby/getGameRanking`（**仅登录账号本人**可读）改为**解析玩家主页「名誉」区块**（`player?id=<数字ID>&section=prestige`，对任意玩家可得；该页面路径为用户当日追加知情授权，见 §9）。`elo_snapshot` 仍按「存原始值」口径落库，落库前用 `elo_raw_from_display()` 还原。

同时修掉两处会导致「一旦配置账号、所有命令失效」的错误：

- 登录接口与参数按 BGA 自己前端的调用代码改写（见 §10.5）。
- `requestToken` 提取兼容两种形态，不再只认 64 位十六进制。

## 13. 验证策略（最小）

- **接口连通性**：用真实账号跑一次「引导 → 登录 → 取赛季榜 → 取总榜 → 查自己 → 搜人」，确认每个命令的数据源可用（用户需提供账号，属人工步骤）。
- **业务规则核对**：由第三方核对这几条已确认的规则是否落实——① 排行榜/竞技场按「每个群各配一个游戏」取数；② 排行榜与「我的信息」缓存 300 秒；③ 每个微信用户只能绑定一个 BGA ID；④ 总积分口径为「当前群所设桌游的 ELO」，数值来自**该玩家主页「名誉」区块**（对**任意绑定玩家**都显示；解析失败或没玩过显示 `—`），名次未上榜显示「未在榜上」；⑤ 趋势口径为「过去 7 天首末快照的差值」；⑥ 合规口径：按用户 2026-10-16 的知情决定，除调用 `/player/player/findplayer.html` 与 `/player/player/getPlayerInfos.html` 两个数据接口外，**追加授权抓取且仅抓取** `player?id=<数字ID>&section=prestige` 这一个页面路径；其余 `/player`、`/playerstat` 页面不请求（见 §9）。
- 不额外扩张为回归测试、覆盖率或代码审查。
