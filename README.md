# 微信文本机器人框架
项目已在linux.do公开声明[https://linux.do/](https://linux.do/t/topic/3029497/51)
交流群
<img width="630" height="952" alt="image" src="https://github.com/user-attachments/assets/908a812a-e6e8-4e31-873c-77514adfff96" />

# 已完成
登录  
收发消息  
群聊艾特  

# 进行中
消息类型区分  
收发图片  


面向 Windows x64、微信 4.1.15.13 的 Python 插件框架。统一入口为 `main.py`，原生桥接负责文本收发，功能以插件加载。


# 本项目仅供学习，请勿用于违法行为

## 功能

- 文件传输助手、好友和群聊文本收发，以及真正的群艾特。
- 成语接龙：独立玩家、合并抢答和退出模式。
- `/绑定 昵称`：只设置当前群的称呼；不同群各自保存。未绑定时用当前群里的真实名字（顺序见「群成员名字来源」）。
- `/总结 昵称`、`/总结群聊`：通过配置的大模型总结当前群当天的文字记录。
- `/切换人格 嘴臭|卑微|夸夸|取消`：为当前群的对话、总结和接龙点评设置表达风格。
- 真正艾特机器人后对话，携带该成员在当前群当天此前最多十条有效发言。
- 每天北京时间 0 点清理旧日文本；`/清空` 手动清理当前群文本。
- 按文本长度随机等待后发送。
- 插件启停、重载与开发模板。

## 运行环境

Windows 10/11 x64、64 位 Python（已验证 Python 3.13）、`requirements.txt` 中的依赖（含解密本机微信数据库用的 `pycryptodome`，单独安装即 `pip install pycryptodome`），以及 Microsoft Visual C++ x64 运行库。

## 首次配置

在项目目录运行：

```powershell
python -m pip install -r requirements.txt
python tools/init_config.py
```

1. `bot_config.json`：首次使用将 `wechat_account` 保持为 `null`，不需要提前知道自己的 wxid。运行 `python main.py` 后，程序会等待电脑微信登录；只有一个已登录账号时自动绑定，多个账号时显示昵称和 ID，输入序号选择机器人账号。绑定会保存到本地配置，重新登录后 PID 改变不影响选择；后续多开只会等待绑定的账号。
2. `members_config.json`：在 `groups` 中添加自己要启用的群 ID，成员列表可以为空：

```json
{
  "database_file": ".runtime/member_data.sqlite3",
  "history_enabled": true,
  "daily_clear": true,
  "groups": {
    "12345678@chatroom": []
  }
}
```

3. `llm_config.json`：填写自己的 `api_key`、`base_url` 和 `model`。模板保留 DeepSeek 接口格式，密钥为空。
4. 模板默认关闭需要模型的 `idiom_chain`、`chat_summary`、`mention_chat` 插件。填写模型配置后，把它们的 `enabled` 改为 `true`。

旧版配置里的 `wxid_example_robot` 和空字符串也会进入首次绑定流程；需要换机器人账号时，将本地 `wechat_account` 改回 `null` 并重启。首次选择只读取登录账号的 ID 和微信名，绑定完成后才启动机器人。群 ID 以 `@chatroom` 结尾，收到群文本时可以直接从控制台日志复制。`test` 消息会由 `id_responder` 插件回复当前会话 ID，`chats` 控制台命令可以列出本次连接收到消息的会话。所有示例 ID 和测试用名称均为虚构数据。

## 群成员名字来源

回复里艾特成员（以及管理台的成员名）显示的名字不需要手工维护：`/绑定` 的称呼最优先，其余从
本机微信数据里取，优先级如下：

| 优先级 | 来源 | 位置 |
| --- | --- | --- |
| 1 | 群昵称（群名片） | `contact.db` 的 `chat_room.ext_buffer` |
| 2 | 微信昵称 | `contact.nick_name` |
| 3 | 备注 | `contact.remark` |
| 4 | 微信号 | `contact.alias` |

四级都取不到的成员会被跳过，不会写入「未知」「群友」之类的占位符；这些人的艾特照旧退回原始 ID。

数据源在连上微信后读一次，之后每 300 秒刷新一次，结果写进成员库的微信名缓存
（`.runtime/member_data.sqlite3`）。这样「从没在群里发过言、因此没被收发桥接记下名字」的成员
也能显示正确名字。管理台按群名显示会话的地方也走同一个数据源：群 ID → 群名，取值顺序为
备注 → 微信昵称 → 微信号，都取不到时回落显示群 ID。数据库密钥按账号缓存在 `.runtime/db_key.json`
——只存密钥，不含任何聊天数据。

本机微信数据库是 SQLCipher 加密的，读取需要当前登录账号的密钥：程序只读枚举微信进程内存、
从 `Weixin.dll` 取辅助 key，再用数据库第 1 页的 HMAC 校验还原出的密钥；微信数据目录里的文件
只会被只读复制，绝不修改、移动或删除。

**隐私说明**：为了不在每轮刷新时重复解密，程序会在 `.runtime/contact_cache/contact.db` 保留一份
**解密后的明文库**供复用——源库（及其 `-wal`）没变化时直接拿它查询。它是本机微信 `contact.db`
只读复制后解密得到的，只含联系人、群与群名片信息；仅存本机，不会上传、不会写进仓库，不需要时
可随时手工删除该目录（下一次刷新会自动重建）。解密过程中的加密中间副本都放在系统临时目录、
每轮用完即删。

`bot_config.json` 里可以一键关掉，或改刷新间隔：

```json
{
  "wechat_names": {
    "enabled": true,
    "refresh_seconds": 300
  }
}
```

`enabled` 必须是布尔值，`refresh_seconds` 必须是 5–86400 秒的数值；整段缺省等同
`{"enabled": true, "refresh_seconds": 300}`。关掉（`enabled=false`）后完全不碰微信库：成员名字
沿用成员库里已有的缓存，**管理台也拿不到群名**，群会话一律显示群 ID。

微信升级导致内存/DLL 密钥特征或数据库布局失效、账号或数据目录变动、密钥校验不通过时，
只会退化成「读不到名字」：程序记一条日志、保留上一轮结果，并在下一次刷新自动重试，
收发消息、群记录和总结都不受影响。长期看不到名字时，可在 `logs/bot.log` 里看
`WechatNames` 与 `DatabaseKey` 的 WARNING。

## 管理台

管理台跟机器人一起启动，不需要单独运行。账号绑定完成、机器人就绪后，启动日志里会打印访问地址：

```text
管理台已启动：http://127.0.0.1:8787/
```

在浏览器打开该地址即可查看统计、消息日志并管理插件。地址只监听本机回环地址，不对外网开放。默认端口为 8787，被占用时向后顺延尝试；所有候选端口都被占用时，管理台不启动，只记一条错误日志，机器人照常运行。

管理台的数据固定保存在 `.runtime/admin_data.sqlite3`，与成员库、聊天缓存分开存放；消息日志最多保留 7 天或 20 万条，超出后自动清理。日志页会记录消息正文，属于本机数据，请勿连同该文件一起分享或提交。

`bot_config.json` 的 `admin` 段可以调整这两项，缺省时按启用、8787 处理：

```json
{
  "admin": {
    "enabled": true,
    "port": 8787
  }
}
```

`enabled` 必须是布尔值，`port` 必须是 0–65535 的整数（`0` 表示由系统自动选择空闲端口，仅供特殊场景使用），JSON 中没有该段也能正常启动。`admin` 段里的未知字段会直接报错（例如把 `port` 拼成 `prot`），避免配置静默失效。该段与 `admins`、`admin_token`、`admin_commands` 无关，后者约束的是微信里的管理员身份与命令权限。

## 启动与验证

```powershell
python main.py --check
python main.py
```

`--check` 只检查配置、插件清单与语法，不识别或绑定账号，不注入微信，也不调用模型。直接运行 `python main.py` 时会自动补齐缺失的默认本地配置，已经填写的内容不会被覆盖；之后在微信中完成登录即可继续。

默认 `INFO` 日志级别下，启动后会在控制台实时显示当前连接的微信账号、收到私聊的发送者 ID 和正文，以及收到群消息的群 ID；这些日志也会保存到 `logs/bot.log`：

```text
当前登录微信ID=wxid_example_robot；好友和群聊文本已就绪
私聊消息 | 发送者微信ID=wxid_example_friend | 内容="你好"
群消息 | 群ID=12345678@chatroom
```

消息显示在会话白名单过滤之前，因此新群尚未加入白名单时，也可以让群里的人发一条文本，从控制台复制群 ID。自动消息日志不打印群正文或群成员 ID；总结功能使用的群聊天缓存按原有配置保存。私聊中的换行会显示为 `\n`，一条消息占一行。`logs/` 已被 Git 忽略。

也可以运行 `start_bot.ps1`：脚本优先使用项目的 `.venv`，其次使用 PATH 中的 Python；可通过 `-Python` 或环境变量 `WXBOT_PYTHON` 指定解释器，不依赖某台电脑的安装路径。

控制台命令：

```text
status
chats
members [群ID]
history 称呼 [条数]
plugins
enable 插件名
disable 插件名
reload [插件名]
quit
```

增加群、修改账号或成员配置后重启。停止时使用 `quit`，等待正常卸载原生桥接；微信可以保持打开。

`quit`、Ctrl+C、Ctrl+Break 和关闭控制台窗口都会请求清理。退出时先停止接收、卸载桥接 DLL，再等待插件退出；运行中会有一个隐藏的清理守护进程，主程序被强制结束时会尝试卸载本项目残留的桥接，完成后自行退出。下次启动也会检查并恢复本项目的残留；原生任务未结束时保留模块并重试，不强行卸载。

同一微信进程只能由一个项目副本连接，自用版与公开版也共用此限制。其他副本正在运行或加载了其他目录的桥接时，会提示先退出该副本。`.runtime/bot.lock` 是可重复使用的锁文件，存在本身不表示重复运行，不需要手工删除；账号、群配置和聊天记录会保留。守护清理日志位于 `logs/bridge_watchdog.log`。

首次自动绑定完成后才初始化成员目录和群记录，使用保存的机器人账号隔离数据。无交互终端且同时登录多个账号时，程序会提示在交互终端选择账号或只保留机器人账号登录，不会自动挑选其中一个。

## 运行测试

```powershell
python -m pip install -r requirements.txt
python -m unittest discover -s tests -p "test_*.py"
```

测试不连接微信或真实大模型，也不需要本地 `bot_config.json`（缺失时自动使用 `bot_config.example.json`）。非 Windows 平台会自动跳过原生相关用例。

## 目录

```text
main.py                  统一入口
wxbot_framework/         框架、成员目录、消息缓存、大模型客户端
plugins/                 各功能插件和开发模板
native/                  原生桥接源码、两个必要 DLL、版本指纹和 MinHook
tests/                  本地测试
tools/                   本地配置初始化和发布前检查
```

