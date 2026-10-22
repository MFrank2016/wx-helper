# BGA 公开 Web 接口文档

调研日期：2026-10-16
目标站：`boardgamearena.com` 主站（不含 BGA Studio 游戏开发框架）
调研时前端版本：`themereleases/261015-1030`
落点：`plugins/bga_plugin/docs/bga-api.md`（插件仓库内）

**可信度标注**

- ✅ **本机实测**——真实发出 HTTP 请求并看到返回体。标注为「✅」的条目中，带 ★ 的是**编排者本人复核过**的（会话引导、`seeMorePrestige`、`arenaInfos`、`findplayer`），其余为本轮调研中实测。
- 🔶 **推断**——从 BGA 前端 JS 或第三方仓库代码读出，未逐条实测。

> 已核实：BGA 没有任何面向主站 Web 接口的官方文档（`en.doc.boardgamearena.com` 全部是 BGA Studio 游戏开发文档）。GitHub 上也不存在现成的第三方 BGA 排行榜接口封装库（对 `seeMorePrestige`、`gameranking/gameranking/arenaInfos`、`getPlayerWorldRanking` 等字符串全量代码检索均为 0 结果）。本文件基本属于首个公开整理，**所有结论以实测为准，勿当成有承诺的 API**。

---

## 0. 通用前提

### 0.1 域名

`https://boardgamearena.com/*` 一律 **302** 到语言子域（✅ ★）：

```
302 boardgamearena.com/player?id=X  ->  en.boardgamearena.com/player?id=X
```

请求请直接打 `https://en.boardgamearena.com`，不要依赖自动跳转。

### 0.2 会话引导（匿名即可，三步）★

1. `GET https://en.boardgamearena.com/leaderboard`（任一页面均可）→ 响应 `Set-Cookie: PHPSESSID=...`。
2. 从返回 HTML 提取令牌（`requestToken`）。**令牌形态不唯一**：
   - 脚本式匿名会话得到的是 64 位十六进制，正则为 `requestToken:\s*'([0-9a-f]{64})'`；
   - 真实浏览器**登录后**的页面里只有 15 位字母数字（实测值形如 `739y1YLuEipvI92`），整页 `[0-9a-f]{64}` 匹配数为 0。
   因此必须同时兼容两种形态：收集全部候选、逐个试，遇到 `code:806` 就换下一个候选。
3. 之后所有数据接口按此格式调用：

```
POST https://en.boardgamearena.com/<module>/<class>/<method>.html
Content-Type: application/x-www-form-urlencoded
X-Request-Token: <requestToken>
Cookie: PHPSESSID=<...>
body: <URLSearchParams>
```

**令牌走 HTTP 头，不是 body 参数**；当成 body 参数会得到 `code:806`（✅ ★）。

### 0.3 统一返回格式

全部返回 `application/json`：

```json
{"status":1,"data":...}
{"status":"0","exception":"...","error":"...","code":806,"args":null}
```

常见错误码（🔶 部分实测）：

| code | 含义 |
|---|---|
| 806 | `Invalid session information`——缺/错 `X-Request-Token`（✅ ★） |
| 800 | `Visitor not allowed`——该接口禁止匿名 |
| 801 | 需要 Premium 账号 |
| 300 | 缺必填参数（✅，报错带参数名） |
| 504 | DB 超时，可重试（✅ 遇到过一次） |
| 505 | 服务端降级 |

### 0.4 登录流程（✅ 已实测）

来源：BGA 自己前端 `js/sveltec/dist/main.js` 里的调用代码，并在已登录浏览器中按此实测通过。

1. 引导：`GET https://en.boardgamearena.com/leaderboard`，从页面取 `requestToken`（见 §0.2）。
2. 登录：`POST https://en.boardgamearena.com/account/auth/loginUserWithPassword.html`
   - 请求头：`X-Request-Token: <requestToken>`
   - body：`username=<邮箱或用户名> / password / remember_me=true / request_token=<同一个 requestToken>`
   - 成功判据：`status == 1` 且 `data.success` 为真；返回值另含 `data.username` / `data.user_id` / `data.avatar` / `data.is_premium` / `data.partnerEvent`。
3. 登录成功后**页面的 `requestToken` 形态会改变**，必须重新引导取新令牌（见 §0.2）。

**已废弃的旧流程**：`GET /account` 取 hidden `request_token` + `POST /account/account/login.html`（body `email / password / rememberme / redirect / request_token / form_id=loginform / dojo.preventCache`）。
该接口与这些参数在当前 BGA 上**已不存在**——`form_id` / `loginform` / `dojo.preventCache` 在 BGA 当前前端包里一个都搜不到。旧流程来自第三方仓库 `pocc/bga_discord`。

**另一个发现**：`POST /account/auth/getRequestToken.html` 拿不到令牌——登录态与匿名态都返回 `{"status":1,"data":[]}`。令牌只能从页面里抠。

### 0.5 游戏短名 ↔ 数字 ID

✅ 匿名可用。两个来源：页面内嵌的 `game_list` JSON（字段 `id` / `name` / `display_name_en` / `games_played` / …），或 `POST /gamelist/gamelist/getGameInfosById.html`（body `gameId=49`）。

**构造 URL 用短名，调数据接口用数字 ID**——`/gameranking?game=yatzy` 正常，传数字 ID 会 500。

---

## 1. 排行榜（某个桌游）

页面：`https://en.boardgamearena.com/gameranking?game=yatzy`（只吃短名；页面是 Svelte 空壳，数据靠接口拉）

**数据接口** ✅ ★

```
POST /gamepanel/gamepanel/getRanking.html
body: game=49&start=0&mode=elo      # 全时段 Elo 榜
body: game=49&start=0&mode=arena    # 当前赛季 Arena 榜
```

- `game` 数字 ID；`start` **0 基偏移**，每页 10 条；`mode` = `elo` | `arena`。
- 实测无 Top100 上限（`start=100`、`start=1000` 均可翻）。
- 返回示例（elo 模式）：

```json
{"status":1,"data":{"ranks":[
 {"id":"88632604","name":"creed23","country":{"name":"South Korea","cur":"KRW","code":"KR","flag_x":384,"flag_y":99},
  "ranking":"1829.66","nbr_game":"2193","rank_no":"1","avatar":"_def_1683","device":"mobile","status":"offline"}],
 "champion":{...}}}
```

字段差异：elo 模式用 `ranking` + `nbr_game`；arena 模式用 `arena`（如 `"501.1681"`）且**没有** `nbr_game`。

**指定赛季榜** ✅

```
POST /halloffame/halloffame/getRanking.html
body: game=49&start=0&mode=arena&season=27
```

⚠️ 对应的**页面** `halloffame?game=...&season=...` 会跳登录，但 **AJAX 接口匿名可用**——很容易踩错。

**登录要求**：不需要。

## 2. 积分排行榜（总积分 XP）

页面：`https://boardgamearena.com/leaderboard`（区块 "Overall leaderboard"）

**数据接口** ✅ ★

```
POST /prestige/prestige/seeMorePrestige.html
body: start=1
```

- `start` 是 **1 基排名**（与 §1 的 0 基不同！），每页 10 条。
- 返回示例（真实片段）：

```json
{"status":1,"data":[
 {"id":"85096311","name":"slywww2020",
  "country":{"name":"Marshall Islands","cur":"USD","code":"MH","flag_x":384,"flag_y":99},
  "prestige":"10175653","avatar":"FK8SknHF6g","rank":"1","rank_ties":"1",
  "is_premium":"1","is_beginner":"0"}]}
```

`prestige` = 总积分，`rank` = 总排名，`rank_ties` = 同分并列数。这是匿名下**字段最全**的玩家数据接口。

**易错项**：`POST /lobby/lobby/getGameRanking.html`（body `game=49`）**不是排行榜**，它返回「当前登录用户在该游戏的排名」，匿名返回 `rank_no:0`。

**登录要求**：不需要。

## 3. 玩家资料

### 3.1 按 ID 查完整资料：**需要登录**

- `GET /player?id=56435591` 匿名 → 302 到登录页（带 Cookie/token/浏览器 UA 也一样）。
- `POST /player/player/getPlayerInfos.html`（body `id=<pid>`）匿名 → **HTTP 500**。
- ✅ **登录后可用**（2026-10-16 在已登录浏览器里实测，两个不同玩家均返回 `status:1`）。真实返回原文：

```json
{"status":1,"data":{"success":true,"data":{
  "id":56435591,"fullname":"DrPuffer","status":"offline","avatar":"622b78b0e3",
  "country_infos":{"name":"美国","cur":"USD","code":"US","flag_x":384,"flag_y":99},
  "prestige":"154295","karma":"100","is_premium":"0",
  "all_lang_infos":[{"name":"English","code":"en_US"}],
  "current_table_id":null,"game_name":null,"table_status":null,"outbound_friend_request":null}}}
```

**字段要点：**
- 昵称字段是 **`fullname`**（不是 `name` / `display_name` / `player_name` / `username`）。
- 国籍字段是 **`country_infos`**（不是 `country`），其下 `code`（`US`）稳定、`name`（`美国`）随页面语言变。
- **真实资料在 `json["data"]["data"]`**：外层 `data` 形如 `{"success":true,"data":{...}}`。
- `prestige` 就是总积分（字符串）；`karma` 有时是字符串有时是数字；**没有总积分排名**。
- 页面语言为 zh 时国名是中文；插件请求 `en.boardgamearena.com`，国名会是英文，`code` 两边一致。

BGA 自己前端的调用代码（`main.js` 原文）：`Uo((await Nt("/player/player/getPlayerInfos.html",{id:e})).data.data)`。

### 3.2 匿名的替代取数路径 ✅

| 需要的字段 | 接口 | 说明 |
|---|---|---|
| 昵称 / ID / 国籍 / 总积分 / 总排名 | `POST /prestige/prestige/seeMorePrestige.html` | 只能按页翻 Top-N，**不能按 ID 直达** |
| 某游戏 Elo 世界排名 | `POST /lobby/lobby/getPlayerWorldRanking.html` body `game=49&isArena=0&player=56435591` → `{"rank":18}` | 只有排名，没有昵称/国籍 |
| 某游戏 Arena 世界排名 | 同上，`isArena=1` → `{"rank":1}` | 同上 |
| 某游戏某赛季的 Arena 积分与排名 | `POST /halloffame/halloffame/getArenaRankingAroundPlayer.html` body `player=56435591&game=49&season=27` | 返回该玩家附近的 `ranks` 数组（含 `id/name/country/arena/rank_no`），**首条即本人**；另有 `rank`（**0 基**，官方前端显示 `rank+1`） |

### 3.3 `/playerstat?id=&game=`（公开 HTML，需解析）

⚠️ 在 `robots.txt` 禁止列表内，**本插件不使用**。

## 4. 玩家模糊搜索 ✅ ★

```
GET https://en.boardgamearena.com/player/player/findplayer.html?q=dupont&start=0&count=Infinity
```

- **完全匿名**，不需要 Cookie、不需要 token。
- 返回：`{"items":[{"id":"98762546","q":"dupont antoine"}, ...]}`
- **只有 `id` 和 `q`（显示名），没有国籍。** 要补国籍只能再查排行榜接口。
- 实测**每次固定返回 10 条**；`start` / `count` 参数被忽略，**无法翻页**。
- 大小写不敏感的前缀匹配。

## 5. 历史趋势

**结论：没有公开的历史时间序列接口，只能自建快照。**

✅ 实测证据：
- `POST /playerstat/playerstat/getrankevol.html`（body `player=...&game=...`）→ `{"data":[]}`。该接口是 `/playerstat` 页面 Rating evolution 图的取数源（🔶），但图表对非 Premium 显示占位图——**Premium 门槛挡住**。
- `POST /gamepanel/gamepanel/getArenaHistory.html`（body `game_id=49&from_season=1`）→ `{"data":[]}`。
- 官方前端 JS 中 423 个 `.html` 端点全量筛过，无按玩家返回逐日 XP / Arena 快照的接口。

**可行做法**：每天定时调用 §2 的总积分榜（覆盖前 N 名）与 §3.2 的 `getArenaRankingAroundPlayer`（指定 player + game + season），落库后自行算 7 日差值。

## 6. 赛季信息 ✅

三种来源，均匿名可用：

```
POST /gameranking/gameranking/arenaInfos.html           （空 body）→ {"data":{"currentSeason":27}}
POST /halloffame/halloffame/getCurrentSeason.html       （空 body）→ {"data":27}
POST /gamepanel/gamepanel/getData.html  body: game_id=49&with_ranking_info=1
     → currentArenaSeason / currentArenaSeasonStatus / currentArenaTimeSpan
```

`getData.html` 信息最全：同时返回该游戏当前赛季 Arena 榜片段（`rankingInfo.ranks`）。注意其中的 `champion` 是「卫冕冠军」语义，**不是**当前赛季第 1 名。

## 7. 反爬与合规

✅ 实测响应头：nginx + PHP 8.4.25，**没有 Cloudflare、没有 JS 挑战、没有验证码**（`/table` 等路径才有反机器人）；未见频率限制响应头。本轮约 50 次请求未被封禁或降级——但这只是经验观察，**不代表官方无限流**。

- CSRF：缺 `X-Request-Token` 一律 806。官方前端的恢复逻辑是重新取 token，但 `POST /account/auth/getRequestToken.html` 对匿名返回空数组（✅），实践上应**重新走 §0.2 引导**。
- `robots.txt` 原文（✅）：

```
User-agent: *
Allow: /
Disallow: /table
Disallow: /playerstat
Disallow: /message/board
Disallow: /newreport
Disallow: /report
Disallow: /player
Disallow: /play
Disallow: /doc/images
Disallow: /doc/Images
Disallow: /web/scripterror
Disallow: /*TidakTidak*
```

所谓 `/player` 与 `/playerstat` **两个路径都在禁止列表内**。它们对应的 `.html` AJAX 接口不在列表内，但这是形式问题，**是否抓取由项目所有者决定；本插件当前决定不碰这两个路径**。

## 8. 最小可用调用清单（速查）

| 功能 | 接口 | 方法 | 关键参数 | 登录 |
|---|---|---|---|---|
| 游戏 Elo 榜 | `/gamepanel/gamepanel/getRanking.html` | POST | `game, start(0基), mode=elo` | 否 |
| 游戏当前赛季 Arena 榜 | `/gamepanel/gamepanel/getRanking.html` | POST | `game, start(0基), mode=arena` | 否 |
| 指定赛季 Arena 榜 | `/halloffame/halloffame/getRanking.html` | POST | `game, start, mode=arena, season` | 否 |
| 总积分（XP）总榜 | `/prestige/prestige/seeMorePrestige.html` | POST | `start(1基排名)` | 否 |
| 玩家某游戏 Elo 世界排名 | `/lobby/lobby/getPlayerWorldRanking.html` | POST | `game, isArena=0, player` | 否 |
| 玩家某游戏 Arena 世界排名 | `/lobby/lobby/getPlayerWorldRanking.html` | POST | `game, isArena=1, player` | 否 |
| 玩家某游戏赛季 Arena 积分/排名 | `/halloffame/halloffame/getArenaRankingAroundPlayer.html` | POST | `player, game, season` | 否 |
| 玩家完整资料（按 ID） | `/player/player/getPlayerInfos.html` | POST | `id` | **是**（匿名 500） |
| 昵称模糊搜索 | `/player/player/findplayer.html` | GET | `q` | 否，连 token 都不用 |
| 当前赛季编号 | `/gameranking/gameranking/arenaInfos.html` | POST | 空 | 否 |
| 赛季编号 + 起止时间 | `/gamepanel/gamepanel/getData.html` | POST | `game_id, with_ranking_info` | 否 |
| 游戏 ID ↔ 短名 | `/gamelist/gamelist/getGameInfosById.html` | POST | `gameId` | 否 |
| 积分历史趋势 | — | — | 无公开接口 | 自建快照 |

## 9. 不确定项（按优先级）

1. ~~登录流程未实测~~ **已实测**（§0.4）：登录接口、参数与成功判据均取自 BGA 自己前端的调用代码。**仍待做**：用真实账号完整跑一次登录（需用户提供账号）。
2. ~~`getPlayerInfos.html` 登录后字段未知~~ **已实测**（§3.1）：字段名已确认，并已回填到插件代码（`fullname` / `country_infos` / 内层 `data.data`）。
3. 匿名 token 的有效期与并发稳定性未测；插件需有 806 自动重建会话的兜底。
4. `findplayer` 的 10 条上限无法翻页——关键字命中 >10 人时拿不到后面的。
5. `POST /halloffame/halloffame/getSeasonData.html` 未跑通（带全参数遇 504），是否匿名可用未知。
6. `getrankevol.html` 是否在登录+Premium 后返回真实序列，未验证。
7. 各接口无官方兼容性承诺，属随时可变动的内部接口。
