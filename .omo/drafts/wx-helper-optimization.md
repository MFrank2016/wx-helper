---
slug: wx-helper-optimization
status: reviewed-approved
intent: clear
review_required: true
pending-action: handoff — execution via $ulw-execute wx-helper-optimization (worker session); user runs Windows probes/tests later; plan sha256 178b55ea36172d9b9e1e4b88406240236cb82ee2f58cf3df86073b5f1d17c6b3

## Review receipts (dual high-accuracy review, 3 rounds, bounded to 5)
- Round 1 (sha ccfacab8…): momus APPROVED (notes) · oracle changes_requested (B1 circular build/verify; B2 evidence trips check_publication).
- Round 2 (sha dbe5f4b9…): momus changes_requested · oracle changes_requested — both converged on N1 (check_publication exclusion orphan + plan self-trip) and N2 (CI check after build/test).
- Round 3 (sha 178b55ea…): momus [OKAY] APPROVED · oracle APPROVED — 0 eligible blockers; N1/N2 resolved; no regressions. Non-blocking notes: T42 todo Blocked-by omits T4 (matrix includes it); CI `--check`/T36 ordering nuance.
- Fixes applied: 原生重建纪律 (B1); 证据脱敏 + T41 owns check_publication.py exclusion (B2/N1); literal path removed (N1); T17 cleans build/test artifacts (N2); T42 deps += T1,T4; T43 label aligned.
- Final live validation: sha256 178b55ea… matches reviewed artifact; 43 impl + 4 verification todos.
approach: Remediate the findings of the full code+functional review of wx-helper, grouped into 5 independently-verifiable components (C1 native safety, C2 Python robustness, C3 security/injection, C4 test+CI, C5 hygiene), phased by severity (P0 crash/data-safety -> P1 availability/engineering -> P2 robustness/maintainability). User confirmed (Q1=A) a Windows x64 + WeChat 4.1.15.13 + MSVC environment exists, so native tasks include implement + REAL-MACHINE probe verification (prove or disprove the 4 reverse-engineered contracts with refcount/sentinel probes). User confirmed (Q2=A) TDD for all Python changes, spec+probe protocol for native changes, and new tests for the zero-coverage modules (dialogue.py, backend.py, runtime.execute(), config validation). Every todo decision-complete with exhaustive references, agent-executable acceptance criteria, and happy+failure QA with an evidence path.
---

# Draft: wx-helper-optimization

## Components (topology ledger)

<!-- id | outcome (one line) | status | evidence path -->

- C1 | 原生桥接安全：修复崩溃/堆破坏/契约未验证项，艾特生效可校验 | active | native/text_bridge.cpp, native/selftest_bridge.cpp, native/run_selftest.py, native/filehelper_service.py, native/text_service.py
- C2 | Python 框架健壮性：锁序/关停阻塞/回调未守护/事务原子性/输出校验 | active | wxbot_framework/{sdk,runtime,members,plugins,summary,dialogue,llm,config}.py, main.py
- C3 | 安全与提示注入：所有人格统一输出后处理；移除/收敛调试后门 | active | wxbot_framework/personality.py, summary.py, dialogue.py, native/text_service.py:146-154
- C4 | 测试与工程化：测试自举、隔离缺陷、Windows-only 守护、零覆盖模块、CI | active | tests/_, tools/_, (no CI config)
- C5 | 文档与仓库卫生：友好错误、gitignore、README 测试运行说明 | active | main.py:45, .gitignore, README.md

## Open assumptions (announced defaults)

<!-- assumption | adopted default | rationale | reversible? -->

- 交付形态 | 写成 `.omo/plans/wx-helper-optimization.md` + 在对话给出交接摘要 | skill 规定 | yes
- 范围 | 覆盖评审全部发现（P0/P1/P2 + Low） | skill: 全量范围是默认 | yes
- 测试策略 | Python 改动走 TDD；原生改动走"规格+真机探针协议" | 用户 Q2=A 已确认 | yes
- 原生验证 | 用户 Q1=A：有 Windows x64 + 微信 4.1.15.13 + MSVC，原生任务含真机探针验证 | 用户确认 | no

## Findings (cited - path:lines)

- N1 Critical: mention_factory __try/__finally 抛 RaiseException 进宿主 — native/text_bridge.cpp:19-39
- N2 Critical: 猜测分配器头释放微信字符串（魔数 39） — native/selftest_bridge.cpp:230-241
- N3 Critical: 完成对象引用计数契约未验证，destroy 无条件 delete — native/text_bridge.cpp:123,141
- N4 High: 任务内字符串分配在 __except 之外 — native/text_bridge.cpp:110-122,191-195
- N5 High: 艾特是否 applied 无人校验（applied 只写不读） — native/text_bridge.cpp:19-39,127-140
- N6 High: 请求字符串在构建器返回后释放（拷贝/移动语义未验证） — native/text_bridge.cpp:135,143-144
- N7 Medium: 队列溢出或脏字节永久杀死接收线程 — native/filehelper_service.py:74-75,164-167; text_service.py:124
- N8 Medium: WxWaitForTasks 5s 上限误判失败并泄漏 DLL — selftest_bridge.cpp:533-536; run_selftest.py:254-265
- N9 Medium: read_pb_string 未限制堆容量 — selftest_bridge.cpp:430-439
- N10 Medium: release_shared 虚析构后读控制块(UAF) — selftest_bridge.cpp:143-150
- N11 Medium: MH_ERROR_ALREADY_INITIALIZED 3/4 路径未处理 — text_bridge.cpp:283-284,391-392
- N12 Medium: std::function/Location ABI 语义未验证 — selftest_bridge.cpp:384-409
- N13-N16 Low: 探针越界读 / 调试后门 / 双次 SHA256 / 回调内磁盘 IO
- P1 High: ServiceFacade 原生发送全程持锁，关停阻塞 ~15s — sdk.py:98-104
- P2 High(工程): 测试依赖 gitignore 的 bot_config.json，全新 clone 14 项失败（实测） — test_framework.py:73; test_chat_summary.py:398
- P3 High(工程): logging.disable 泄漏导致跨文件污染（实测） — test_framework.py:63-64
- P4-P10 Medium: active 竞态 / manager 锁内等待 / response_guard ABBA / 嵌套 with 事务 / 4汉字(已降级) / prepare_switch(已降级) / 注入输出
- P11-P16 + L6 Low: split_text / 字符vs字节 / own_group_command / IncompleteRead / --check 硬编码 / backend 重复 / traceback
- T-* 工程: 无 CI/pytest 配置; dialogue.py+backend.py+runtime.execute()+config 校验分支零覆盖; 3 个 Windows-only 测试未守护

## Decisions (with rationale)

- 按严重度分阶段（P0 崩溃/数据安全 → P1 可用性/工程 → P2 健壮/可维护），而非按模块，确保风险优先下降。
- 原生层在本机（macOS，无微信）不可验证；用户已确认存在 Windows x64 + 微信 4.1.15.13 + MSVC 环境（Q1=A）→ 每个原生改动都要**真机证据**，否则显式标注"未验证"，绝不伪造。
- 已复核修正 3 条代理误判（prepare_switch 实际被 _receive_loop 捕获；4汉字被 bot_generated 去重；--dispatch+mentions 悬空钩子不成立），不纳入计划。

## Scope IN

- C1-C5 全部组件的修复、测试、文档。

## Scope OUT (Must NOT have)

- 不做任何"重写原生注入架构"或"更换 hook 方案"的大改造。
- 不新增未请求的功能/插件。
- 不修改微信客户端或分发其二进制。
- 不在本机（macOS）伪造原生层实机验证结论。

## Open questions

- Q1 原生层验证环境与验证方式 -> RESOLVED: A（有 Windows x64 + 微信 4.1.15.13 + MSVC；含真机探针验证）
- Q2 测试策略确认 -> RESOLVED: A（Python TDD；原生规格+探针；补齐零覆盖）

## Decisions (Q&A)

- Q1=A：原生任务包含"实现 + 真机探针"；探针必须能证实或证伪 N3(完成对象引用)/N6(请求串拷贝-移动)/N12(std::function 拷贝)/N2(错误文本分配器头) 四个契约。
- Q2=A：所有 Python 行为改动先写失败测试(RED)再实现(GREEN)；新增 dialogue/backend/runtime.execute/config 校验 测试。

## Approval gate

status: awaiting-approval
<!-- brief presented; waiting for explicit user okay before writing the plan file -->

## Pre-execution confirmations (user, resolved)

- 通道（Q1）: 用户自行在 Windows 运行探针脚本并回传 JSON 证据（对应计划 T43 / 验证策略第 61 行）。
- 版本来源（Q2）: 用户将从 `cscnk52/wechat-windows-versions` 的 release `v4.1.15.13` 下载安装包测试。
  - 该 release 实测存在，唯一资源 `weixin_4.1.15.13.exe`，256,624,952 字节，**安装包** sha256 `d5f3bb4232c0f172bce53141db90bf60f5f0a53eb137663ab8d32e74e04b9f4f`（2026-09-22 发布）。
  - ⚠️ 该哈希是**安装包**的，不是计划要锁定的 **`Weixin.dll`**。安装后必须核对 DLL：大小 `201552944`、sha256 `10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5`（`compatibility.json`）。
  - 该核对由 T43 执行；且 `native/run_selftest.py` 在加载前会自行校验指纹，不匹配即返回"微信核心 DLL 指纹不匹配，拒绝加载"——用户只需运行探针即可暴露不匹配。
  - 校验命令（Windows PowerShell，安装目录按实际替换）:
    `(Get-Item "<安装目录>\Weixin.dll").Length` 与 `(Get-FileHash "<安装目录>\Weixin.dll" -Algorithm SHA256).Hash`
