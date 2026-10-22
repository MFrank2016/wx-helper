# wx-helper-optimization - Work Plan

## TL;DR (For humans)

<!-- Fill this LAST, after the detailed plan below is written, so it summarizes the REAL plan. -->
<!-- Plain English for a non-engineer: NO file paths, NO todo numbers, NO wave/agent/tool names. -->

**What you'll get:** 一个经过加固的微信机器人框架——原生注入层不再因为版本或布局不符而崩溃微信、也不再用"猜出来的内存布局"静默破坏内存；机器人关停不再长时间卡住，单条坏消息或队列溢出不再让收发永久停摆；所有人格的回复都会先做净化再发群；测试在全新克隆上无需任何本地配置即可跑通，并接入持续集成。

**Why this approach:** 先按"崩溃/数据安全 → 可用性 → 健壮性"排序，让最大风险最先下降；原生层那几个"靠猜的逆向假设"先用探针在真机上证实或证伪，再据此修复，而不是继续凭假设改代码。

**What it will NOT do:** 不重写注入架构、不更换 hook 方案；不新增功能或插件；不修改微信客户端；在没有真机证据时不谎称"已验证"。

**Effort:** Large
**Risk:** High - 涉及向微信进程注入的原生内存安全改动，做错可能导致宿主崩溃或数据损坏。
**Decisions to sanity-check:** ① 原生修复采用"先探针、后修复"（你已选 A）；② Python 改动严格 TDD（你已选 A）；③ **Metis 复核新增的两个待确认点**：(a) 真机探针的**执行与证据回传通道** —— 代理能否直达 Windows 机器，还是由你运行探针脚本并回传 JSON？(b) 真机 `Weixin.dll` 是否与 `compatibility.json` 的 SHA-256 指纹**完全一致**（自动更新过的微信会拒绝所有探针，令整个原生阶段卡死）。

Your next move: 由执行会话运行本计划，或先做一次高精度复核。完整执行细节见下。

---

> TL;DR (machine): Large / High — 43 implementation todos + 4 verification, 7 waves, 5 components (native safety, python robustness, security, test+CI, hygiene); native fixes gated on real-machine probes of 4 RE contracts + a rebuild/commit-DLL delivery step; python TDD.

## Scope

### Must have

- **C1 原生桥接**：消除"崩溃宿主 / 静默堆破坏"的 P0 项（N1–N6）；用**真机探针**证实或证伪 4 个逆向契约；接收线程健壮性（N7）；清理 N8–N16。
- **C2 Python 框架**：关停阻塞（P1）、回调守护（P8）、并发竞态（P4/P5/P6）、事务原子性（P10）、输入/输出校验（P11/P12/P13/P14）、工程一致性（P15/P16）。
- **C3 安全**：所有人格统一输出后处理（P7）；调试后门收敛（N14）。
- **C4 测试与工程化**：测试自举（P2）、隔离修复（P3）、Windows-only 守护、CI、零覆盖模块测试（`dialogue.py` / `backend.py` / `runtime.execute()` / `config` 校验）。
- **C5 文档与卫生**：友好错误（L6）、`.gitignore`、README、原生回滚说明。
- 每个 Python 行为改动都有测试；每个原生改动都有**真机证据**或明确标注"未验证"。
- **明确真机探针的执行与证据回传通道**（谁运行、如何把 JSON 证据取回 `.omo/evidence/`）；证据缺失时以"未验证"显式标注，绝不伪造。
- **确认真机 `Weixin.dll` 与 `compatibility.json` 指纹一致**；不一致则先解决版本/指纹（owner 决策，见 TL;DR）。
- **原生交付物**：修复后必须用 MSVC 重建并提交两个 DLL（`native/build/selftest_bridge.dll`、`native/build_text/text_bridge.dll`），否则修复不生效。

### Must NOT have (guardrails, anti-slop, scope boundaries)

- 不重写原生注入架构、不更换 hook 方案、不引入新的原生第三方依赖。
- 不新增未请求的插件/功能。
- 不修改微信客户端，不分发其二进制。
- **不伪造原生实机验证结论**：无真机证据不得声称"已验证"。
- **不对用户主账号做探针以外的自动化**；探针只在经批准的测试账号/群执行，并显式声明接受 hook 检测/封号风险。
- **不在仓库内留下构建产物**（`.build/`、`*.obj/*.exe/*.lib`）——否则 `check_publication.py` 会 `passed:false`。
- 不做与评审无关的格式化/重命名大扫除。

## Verification strategy

> 零人工**判定** —— 所有验证结论由代理给出；原生探针/构建在 Windows 机执行（无代理直达通道时由用户运行脚本），JSON 证据回传 `.omo/evidence/` 供代理判定。

- **Test decision: TDD（Python，先 RED 后 GREEN，`unittest`）+ 原生"规格 + 真机探针"（非 TDD）**。Python 每个行为改动先写失败测试；原生改动以探针脚本输出为证据。
- Evidence: `.omo/evidence/task-<N>-wx-helper-optimization.<ext>`
- **关键验证面（真实入口，非"测试通过"自证）**：
  - `python -m unittest discover -s tests -p "test_*.py"` —— Windows 上**全绿**；macOS/Linux 上仅 Windows-only 用例被 `skipUnless` 跳过。
  - **全新 clone 自举**：无本地 `bot_config.json` 时测试也全绿（P2 的验收）。
  - `python main.py --check` 与 `--list-plugins` 输出正确、exit 0。
  - `python tools/check_publication.py` 在干净树返回 `passed: true`。
  - 原生：真机探针往返（发送 + 接收 + 艾特）报告 JSON，含引用计数/哨兵字节/析构计数。
  - CI：Windows + Linux 两个 workflow 均绿。
- **F3 的"真机手工 QA"= 代理驱动的探针证据**：在 Windows 机器上运行探针脚本并把 JSON 回传 `.omo/evidence/`，由代理判定；**不把人工目测作为门禁**（否则与"零人工介入"矛盾）。若代理无法直达 Windows 机器，则由用户执行脚本、回传 JSON（这是唯一允许的人工环节，且只做"运行脚本"）。
- **CI 范围**：`.github/workflows` 只跑 **构建 + 单测 + `main.py --check`**；CI 无微信，**不跑探针**，不得以探针证据作为 CI 门禁。
- **证据清单契约**：每个原生 todo 的证据文件必须记录探针 JSON 的来源机器与路径（T43 的通道），F1 据此核对，而非自评。
- **证据脱敏（避免触发发布检查）**：`tools/check_publication.py` 全树扫描、只排除 `.git`、**不读 `.gitignore`**；探针 JSON 含 `report_path` 等**绝对路径**（Windows 盘符 + 用户目录形式）或 `.runtime/` 产物会被判 `passed:false`。因此写入 `.omo/evidence/` 前必须剥离绝对路径，并在跑发布检查前清理 `.runtime/`；同时修改 `tools/check_publication.py` 显式跳过 `.omo/` 与 `.code-review-graph/`（该工具不读 `.gitignore`，必须在工具内跳过）。

## Execution strategy

### Parallel execution waves

> Target 5-8 todos per wave. Fewer than 3 (except the final) means you under-split.

- **Wave 1 — P0 原生独立修复 + 快速工程 + 探针通道**（7）：T1 探针基建、T2 N1、T3 N4、T4 N5、T5 P3、T6 Windows-only 守护、T43 探针执行/证据通道。
- **Wave 2 — 契约判定与依赖修复 + 接收健壮性 + 测试自举**（7）：T7 探针判定（依赖 T1,T4,T43）、T8 N3、T9 N6、T10 N12、T11 N2（均依赖 T7）、T12 N7、T13 P2。
- **Wave 3 — P1 可用性/工程**（5）：T14 N8、T15 P1、T16 P8、T17 CI、T18 P15。
- **Wave 4 — P2 Python 并发/健壮性**（7）：T19 P4、T20 P5、T21 P10、T22 P14、T23 P12、T24 P11、T25 P13。
- **Wave 5a — P2 原生加固（探针无关）+ 安全**（7）：T26 N9、T27 N10、T28 N11、T29 N13、T31 N15、T32 N16、T33 P7。
- **Wave 5b — 探针相关（1）**：T30 N14（依赖 T7；范围限定，见 T30）。
- **Wave 6 — P2 卫生 + 覆盖补齐**（8）：T34 P6、T35 P16、T36 L6、T37 `dialogue.py` 测试、T38 `backend.py` 测试、T39 `runtime.execute()` 测试、T40 `config` 校验测试、T41 卫生/文档。
- **Wave 7 — 原生交付**（1）：T42 用 MSVC 重建并提交两个 DLL（依赖全部原生 .cpp 改动）。
- **Final verification wave — F1–F4**（并行，全部 APPROVE）。
- **原生重建纪律（消除"修复→构建→探针"环）**：任何需要真机探针证据的 todo，其证据必须在**含该修复的重建 DLL**上产生——每轮真机探针前先用 `native/build_bridges.ps1` 重建：T1 首建（探针装置）→ T2/T3/T4 修复后重建一次再跑真机探针 → T8–T11 修复后重建一次 → T42 最终重建并提交。**不存在"未重建即验收"的原生 todo**。

### Dependency matrix

| Todo                  | Depends on                            | Blocks            | Can parallelize with     |
| --------------------- | ------------------------------------- | ----------------- | ------------------------ |
| T1 探针基建           | —                                     | T7                | T2,T3,T4,T5,T6,T43       |
| T2 N1                 | —                                     | F3                | T1,T3,T4,T5,T6,T43       |
| T3 N4                 | —                                     | F3                | T1,T2,T4,T5,T6,T43       |
| T4 N5                 | —                                     | T7,T8,T9          | T1,T2,T3,T5,T6,T43       |
| T5 P3                 | —                                     | F1                | 全部                     |
| T6 Windows-only 守护  | —                                     | T17               | 全部                     |
| T43 探针执行/证据通道 | —                                     | T7                | T1,T2,T3,T4,T5,T6        |
| T7 探针判定           | T1,T4,T43                             | T8,T9,T10,T11,T30 | T12,T13                  |
| T8 N3                 | T7                                    | F3                | T9,T10,T11               |
| T9 N6                 | T7                                    | F3                | T8,T10,T11               |
| T10 N12               | T7                                    | F3                | T8,T9,T11                |
| T11 N2                | T7                                    | F3                | T8,T9,T10                |
| T12 N7                | —                                     | F3                | T7,T13                   |
| T13 P2                | —                                     | T17               | T7,T12                   |
| T14 N8                | —                                     | F3                | T15,T16,T17,T18          |
| T15 P1                | —                                     | F3                | T14,T16,T17,T18          |
| T16 P8                | —                                     | F3                | T14,T15,T17,T18          |
| T17 CI                | T6,T13                                | F1                | T14,T15,T16,T18          |
| T18 P15               | —                                     | F1                | T14,T15,T16,T17          |
| T19–T25 (Wave4)       | —                                     | F2                | 组内互不依赖             |
| T26–T29,T31–T33 (5a)  | —                                     | F2                | 组内互不依赖（探针无关） |
| T30 N14 (Wave5b)      | T7                                    | F2                | —                        |
| T34–T41 (Wave6)       | —                                     | F2                | 组内互不依赖             |
| T42 重建+提交 DLL     | T1,T2,T3,T4,T8,T9,T10,T11,T14,T26–T32 | F3                | —                        |
| F1–F4                 | 全部 T                                | —                 | 相互并行                 |

## Todos

> Implementation + Test = ONE todo. Never separate.

<!-- APPEND TASK BATCHES BELOW THIS LINE WITH edit/apply_patch - never rewrite the headers above. -->

- [ ] 1. 原生契约探针基建（4 契约的可证伪探针 + 驱动）
      What to do / Must NOT do: 复用 `selftest_bridge.cpp` 结构新增导出 `WxProbeContracts`，对四个契约输出 JSON：完成对象引用计数（在 completion 里计 AddRef/Release/destroy 次数）、请求串拷贝/移动（发送后在本地释放前后各读一次队列消息正文哨兵字节）、调度器 `std::function` 拷贝（给 `TaskLease` 加析构计数）、错误文本分配器头（dump `result+8` 的块头）。**同时提供强制失配用例**（vtable 不符、TLS 置空）供 T2/T4 验收使用。新增 `native/run_probe.py` 驱动（沿用 run_selftest 的 remote_call 模式）。MUST NOT 改动现有生产导出行为。
      Parallelization: Wave 1 | Blocked by: — | Blocks: T7
      References (executor has NO interview context - be exhaustive): native/selftest_bridge.cpp:314-343,376-382,384-409,515-536; native/text_bridge.cpp:77-108,123-146; native/run_selftest.py:107-117,146-287; native/build_bridges.ps1:20-40
      Acceptance criteria（Windows 执行；代理核对回传证据）: `powershell native/build_bridges.ps1 -Target all` exit 0；真机 `python native/run_probe.py --contracts --pid <wxpid>` 输出含 `completion_refcount`/`request_string_move_or_copy`/`scheduler_function_copy`/`error_allocator_header` 四项判定的 JSON。
      QA scenarios (name the exact tool + invocation): happy = Bash 运行探针，断言 JSON 四字段非空且 exit 0；failure = 微信未启动时探针打印"等待微信"并 exit≠崩溃。Evidence .omo/evidence/task-1-wx-helper-optimization.json
      Commit: Y | feat(native): add contract probe harness
      Recommended task executor category: ultrabrain — cohesive RE + SEH + ABI work that must not be split.

- [ ] 2. N1：`mention_factory` 用 `__except` 记录并降级，绝不向宿主抛异常
      What to do / Must NOT do: 把 `text_bridge.cpp:22-37` 的 `__try/__finally` 改为 `__try/__except`，用 `record_exception` 记录布局/成员不符，随后**跳过艾特写入**（保留 `context->applied=false`）并返回；删除 `RaiseException`。MUST NOT 让异常穿出宿主；MUST NOT 静默成功（必须留可读日志/报告）。
      Parallelization: Wave 1 | Blocked by: — | Blocks: F3
      References: native/text_bridge.cpp:19-39,127-140; native/selftest_bridge.cpp:96-104
      Acceptance criteria: 真机发送一条艾特消息，`mention_messages`/报告显示 applied=true 且进程存活；构造 vtable 不符的注入用例（探针）返回 applied=false 且微信不崩溃。
      QA: happy = 真机发送艾特并读报告；failure = 探针强制 vtable 不符，断言微信进程仍存活（tasklist 检查）。Evidence .omo/evidence/task-2-wx-helper-optimization.json
      Commit: Y | fix(native): degrade instead of raising into host on at-mention mismatch
      Recommended task executor category: deep — safety-critical SEH change.

- [ ] 3. N4：任务体（分配 + 请求构造 + 构建器）整体纳入 `__try/__except`
      What to do / Must NOT do: `send_text_on_task`(text_bridge.cpp:110-146) 与 `dispatch_on_native_task`(selftest_bridge.cpp:345-374) 把 `set_native_string`/`set_inline`/TLS/构建器全部放进一个 `__try/__except(record_exception)`；异常时写报告并返回失败码。MUST NOT 让 AV 穿出调度线程。
      Parallelization: Wave 1 | Blocked by: — | Blocks: F3
      References: native/text_bridge.cpp:110-146,191-195; native/selftest_bridge.cpp:345-374,411-428
      Acceptance criteria: 构造分配器 RVA 失效的探针用例，返回失败报告且微信进程存活。
      QA: happy = 真机正常发送成功；failure = 探针注入坏 RVA，断言不崩溃且报告含 exception。Evidence .omo/evidence/task-3-wx-helper-optimization.json
      Commit: Y | fix(native): wrap task setup in __except
      Recommended task executor category: deep — SEH + native.

- [ ] 4. N5：构建器返回后校验艾特 `applied`，未生效返回独立错误码
      What to do / Must NOT do: `WxSendAtText`/`send_text_on_task` 在构建器调用后检查 `mention.applied`；false 时返回新错误码（如 7）并写报告。`text_service._send_text` 将其翻译为 Python 侧可读错误。MUST NOT 把"未生效"当成功。
      Parallelization: Wave 1 | Blocked by: — | Blocks: T7,T8,T9
      References: native/text_bridge.cpp:15,35,127-145,173-198; native/text_service.py:169-186; native/run_selftest.py:242-267
      Acceptance criteria（可观测）: 真机艾特后 `at_receive_probe.json` 的 `at_members` 含目标 wxid 且 `is_at_self=true`（或完成结果回传 applied=true）→ `success=true`；探针令工厂不触发 → 返回码 7、`success=false` 且报告 applied=false。
      QA: happy = 真机艾特往返；failure = 探针禁用 TLS 上下文，断言错误码 7。Evidence .omo/evidence/task-4-wx-helper-optimization.json
      Commit: Y | feat(native): verify at-mention application, distinct error code
      Recommended task executor category: deep — cross-layer (C++↔Python) contract.

- [ ] 5. P3：修复 `logging.disable` 泄漏导致的跨文件测试污染
      What to do / Must NOT do: `test_framework.py:65-66` 的 `logging.disable` 用 `addCleanup` 或在 `try/finally` 中恢复 `logging.root.manager.disable`，保证 setUp 抛异常也恢复。MUST NOT 依赖 tearDown 执行。
      Parallelization: Wave 1 | Blocked by: — | Blocks: F1
      References: tests/test_framework.py:65-66（disable）,96（restore）,73（缺配置抛错）; tests/test_idiom_chain.py:160-172
      Acceptance criteria: 制造 setUp 失败的场景后，`test_idiom_chain` 的 `assertLogs` 仍通过。
      QA: happy = `python -m unittest discover -s tests` 全绿；failure = 临时令某 setUp 抛错，断言后续 assertLogs 不失败（RED 复现→GREEN）。Evidence .omo/evidence/task-5-wx-helper-optimization.txt
      Commit: Y | test(framework): restore logging level via cleanup
      Recommended task executor category: quick — single-file mechanical.

- [ ] 6. 为 3 个未守护的 Windows-only 测试补 `skipUnless(win32)`
      What to do / Must NOT do: 给 `test_framework.py::test_single_instance_lock_is_released`、`test_personality.py::NativeAtReceiveTests::test_native_metadata_marks_only_actual_robot_group_at`、`test_text_routing.py::test_native_service_skips_recent_bot_text_but_allows_players` 加 `@unittest.skipUnless(sys.platform=="win32", ...)`，与既有 5 个一致。
      Parallelization: Wave 1 | Blocked by: — | Blocks: T17
      References: tests/test_framework.py:248-255; tests/test_personality.py:325-345; tests/test_text_routing.py:69-85; 对比 tests/test_account.py:168,181,193,203
      Acceptance criteria: macOS `python -m unittest discover -s tests` 显示这 3 项为 skip 而非 error。
      QA: happy = 全套运行无 error；failure = 无。Evidence .omo/evidence/task-6-wx-helper-optimization.txt
      Commit: Y | test: guard windows-only tests with skipUnless
      Recommended task executor category: quick — mechanical.

- [ ] 7. 运行契约探针，判定 4 个契约并记录
      What to do / Must NOT do: 在用户的 Windows 真机运行 T1 探针，把四个判定（完成对象引用计数、请求串拷贝/移动、`std::function` 拷贝、错误文本分配器头）写入 `.omo/evidence/contracts.md`，作为 T8–T11 的输入。MUST NOT 猜测；无真机结果不得进入 T8–T11。
      Parallelization: Wave 2 | Blocked by: T1,T4 | Blocks: T8,T9,T10,T11
      References: .omo/evidence/task-1-wx-helper-optimization.json; native/selftest_bridge.cpp:230-241,384-409; native/text_bridge.cpp:123-146
      Acceptance criteria: `.omo/evidence/contracts.md` 含四项结论 + 原始 JSON 路径。
      QA: happy = 四项均有明确结论；failure = 某项无法判定时标记 UNKNOWN 并阻断对应 T。Evidence .omo/evidence/task-7-wx-helper-optimization.md
      Commit: N | —
      Recommended task executor category: deep — interpretation of probe results.

- [ ] 8. N3：按探针结果修正完成对象生命周期
      What to do / Must NOT do: 若探针显示构建器 AddRef 而不置空 → 改为引用计数（调用前 `InterlockedIncrement`，归零才 `delete`）；若确认 consume/null → 保留并在 `text_completion_destroy` 前断言 `references==1`。MUST NOT 无条件 `delete` 活对象。
      Parallelization: Wave 2 | Blocked by: T7 | Blocks: F3
      References: native/text_bridge.cpp:77-108,123,141; native/selftest_bridge.cpp:314-343,359,371; .omo/evidence/contracts.md
      Acceptance criteria（按 T7 判定分支）: 证明"AddRef 不置空" → 改引用计数后真机连发 100 条 `live_completions` 归零、无崩溃；证明"consume/置空" → 保留现逻辑并断言 `references==1` 才 delete；判定 inconclusive → 采用最保守（引用计数）并标注"未验证"。三支均需无重复释放的探针计数。
      QA: happy = 100 条发送计数归零；failure = 探针记录 destroy 次数≠AddRef 次数则 RED。Evidence .omo/evidence/task-8-wx-helper-optimization.json
      Commit: Y | fix(native): correct completion lifetime per verified contract
      Recommended task executor category: ultrabrain — memory-safety contract.

- [ ] 9. N6：按探针结果修正请求字符串释放（拷贝语义确认后再 free）
      What to do / Must NOT do: 探针若确认构建器深拷贝 → 保留现有 `free_native_string`；若为移动/窃取 → 不再本地 free（交由消息析构）。MUST NOT 在语义未知时释放。
      Parallelization: Wave 2 | Blocked by: T7 | Blocks: F3
      References: native/text_bridge.cpp:110-146; native/selftest_bridge.cpp:352-358,372; .omo/evidence/contracts.md
      Acceptance criteria（按 T7 判定分支）: 证明"深拷贝" → 保留本地 free 且正文完好；证明"移动/窃取" → 删除本地 free（交消息析构）且正文完好；inconclusive → 不本地 free（最保守）并标注"未验证"。三支均无堆破坏/重复释放。
      QA: happy = 发送后正文校验通过；failure = 哨兵字节被改写则 RED。Evidence .omo/evidence/task-9-wx-helper-optimization.json
      Commit: Y | fix(native): free request strings only under verified copy semantics
      Recommended task executor category: deep — native memory contract.

- [ ] 10. N12：按探针结果修正 `std::function`/`Location` 传递
      What to do / Must NOT do: 探针若证实调度器按 `std::function` 拷贝构造 → 保留；若为裸 `memcpy` → 改为传 DLL 自持的稳定堆结构指针（避免 shared_ptr 双重析构）。MUST NOT 依赖未证实的拷贝语义。
      Parallelization: Wave 2 | Blocked by: T7 | Blocks: F3
      References: native/selftest_bridge.cpp:376-409; .omo/evidence/contracts.md
      Acceptance criteria（按 T7 判定分支）: 证明"按 std::function 拷贝构造" → 保留；证明"裸 memcpy" → 改传自持堆结构指针；inconclusive → 采用自持指针（最保守）。三支均需真机连发 100 条、Lease 析构计数精确归零、无崩溃。
      QA: happy = 析构计数归零；failure = 计数≠100 则 RED。Evidence .omo/evidence/task-10-wx-helper-optimization.json
      Commit: Y | fix(native): pass scheduler payload by stable pointer
      Recommended task executor category: ultrabrain — ABI/ownership.

- [ ] 11. N2：停止用"猜测的分配器头"释放微信字符串
      What to do / Must NOT do: 把 `selftest_bridge.cpp:230-241` 的 error_text 释放改为"拷贝到本地缓冲、不释放"，或按探针确认的块头精确释放；删除魔数 39。MUST NOT 用未验证的偏移/大小释放宿主堆。
      Parallelization: Wave 2 | Blocked by: T7 | Blocks: F3
      References: native/selftest_bridge.cpp:228-241; .omo/evidence/contracts.md
      Acceptance criteria（按 T7 判定分支）: 证明块头布局 → 按确认布局精确释放；证明魔数 39 错误 → 删除该路径、改为拷贝不释放；inconclusive → 拷贝不释放（最保守）。三支均需连续触发 100 次稳定、报告正常。
      QA: happy = 连续 100 次触发稳定；failure = 探针记录堆异常则 RED。Evidence .omo/evidence/task-11-wx-helper-optimization.json
      Commit: Y | fix(native): stop freeing host strings with a guessed allocator header
      Recommended task executor category: deep — heap safety.

- [ ] 12. N7：接收线程健壮性（脏字节不致命、队列溢出不致命）
      What to do / Must NOT do: `text_service._map_message` 的 `message.text.decode("utf-8")` 改 `errors="replace"`（与 mention_source 一致）；`filehelper_service.poll` 的 `message.dropped` 由 `raise` 改为返回可忽略字段 + 日志；`_receive_loop` 捕获后**继续**而非 `_stop.set()`。MUST NOT 因单条消息永久停摆。
      Parallelization: Wave 2 | Blocked by: — | Blocks: F3
      References: native/filehelper_service.py:58-76,147-167; native/text_service.py:119-164
      Acceptance criteria: 注入非法 UTF-8 与 dropped>0 的探针消息，接收循环继续处理后续消息（真机或单元仿真）。
      QA: happy = 脏字节后仍收到下一条；failure = 模拟 dropped，断言服务未停。Evidence .omo/evidence/task-12-wx-helper-optimization.txt
      Commit: Y | fix(native): keep receiver alive on dirty bytes and queue overflow
      Recommended task executor category: deep — native service robustness.

- [ ] 13. P2：测试自举——不再依赖被 gitignore 的本地配置
      What to do / Must NOT do: 让 `test_framework.py:73` 与 `test_chat_summary.py:398` 从 `bot_config.example.json` 生成临时配置（或 setUp 内 `tools.init_config.initialize` 到临时目录），使全新 clone 无本地配置也全绿。MUST NOT 要求人工先跑 init_config 才能测试。
      Parallelization: Wave 2 | Blocked by: — | Blocks: T17
      References: tests/test_framework.py:73; tests/test_chat_summary.py:396-399; tools/init_config.py:8-17
      Acceptance criteria: 删除根目录 `bot_config.json` 后 `python -m unittest discover -s tests` 全绿；先核对 `bot_config.example.json` 含 `test_framework.py:76-81` 会改写的全部 plugin 键。
      QA: happy = 无本地配置全套绿；failure = 保留旧写法时 RED（先复现）。Evidence .omo/evidence/task-13-wx-helper-optimization.txt
      Commit: Y | test: make suite self-bootstrapping from example config
      Recommended task executor category: quick — contained test-infra change.

- [ ] 14. N8：`WxWaitForTasks` 重试到 15s，避免误判与 DLL 泄漏
      What to do / Must NOT do: `selftest_bridge.cpp:533-536` 的等待上限提到与报告截止一致（~15s）；`run_selftest.py:254-265` 在 ERROR_BUSY 时重试清理后再判失败。MUST NOT 在可成功时跳过 FreeLibrary。
      Parallelization: Wave 3 | Blocked by: — | Blocks: F3
      References: native/selftest_bridge.cpp:533-536 **与 :555（WxRemoveSelfTest 同一 5s 上限）**; native/run_selftest.py:242-265
      Acceptance criteria: 慢发送（大文本）场景 `success=true` 且 DLL 已卸载；**两处 5s 上限（:533 与 :555）都放宽**，避免 remove 路径仍 5s 超时导致泄漏。
      QA: happy = 大文本发送成功且 dll_unloaded=true；failure = 无。Evidence .omo/evidence/task-14-wx-helper-optimization.json
      Commit: Y | fix(native): retry cleanup to 15s before failing
      Recommended task executor category: unspecified-high — native + python.

- [ ] 15. P1：ServiceFacade 关停不再被原生发送无限阻塞
      What to do / Must NOT do: `sdk.py:98-104` 让 `close()`/`cancel_pending()` 不被原生发送无限阻塞——发送持锁期间 `close()` 带超时并记录"发送中"，或把原生调用移出连接锁并显式防护卸载。MUST NOT 让 `quit` 卡满 15s 无提示。**先写失败测试（RED）再改。**
      Parallelization: Wave 3 | Blocked by: — | Blocks: F3
      References: wxbot_framework/sdk.py:74-143; wxbot_framework/runtime.py:180-188; tests/test_text_pacing.py:111-130
      Acceptance criteria: **用阻塞后端假件（send_text 内 sleep/事件阻塞）** 复现"发送中 close()"，`close()` 在 ≤2s 有界返回（有日志）、无死锁；`test_text_pacing` 全绿。MemoryBackend 即时返回无法复现，必须新增阻塞假件。
      QA: happy = 阻塞发送中 close() 有界返回；failure = 无超时会挂起则 RED。Evidence .omo/evidence/task-15-wx-helper-optimization.txt
      Commit: Y | fix(sdk): bound shutdown while a native send is in flight
      Recommended task executor category: deep — concurrency.

- [ ] 16. P8：`_emit` 中的 `prepare_switch` 加守护
      What to do / Must NOT do: `runtime.py:72-73` 照抄 `members.record` 的 `try/except: logger.exception(...)`，异常时**继续分发未切换的消息**。MUST NOT 让 DB 抖动丢整条消息。
      Parallelization: Wave 3 | Blocked by: — | Blocks: F3
      References: wxbot_framework/runtime.py:63-74; wxbot_framework/personality.py:73-88; native/filehelper_service.py:159-163
      Acceptance criteria: mock `set_personality` 抛错时，消息仍被 dispatch。
      QA: happy = 异常下仍 dispatch；failure = 当前实现 RED（先复现）。Evidence .omo/evidence/task-16-wx-helper-optimization.txt
      Commit: Y | fix(runtime): guard personality switch on the receive path
      Recommended task executor category: quick — small guarded change + test.

- [ ] 17. CI：Windows + Linux 工作流（unittest + `--check` + 发布检查）
      What to do / Must NOT do: 新增 `.github/workflows/ci.yml`（GitHub Actions）：Windows job（windows-latest）跑 MSVC 构建两个 DLL + `python -m unittest discover -s tests` + `python main.py --check`；Linux job（ubuntu-latest）跑纯 Python 子集（Windows-only skip）；两 job 跑 `python tools/check_publication.py`。**发布检查前必须清理构建/测试产物**（删除 `__pycache__`/`*.pyc` 与 `native/.build`，或把 `check_publication.py` 放在构建与单测**之前**运行）——否则 `.obj/.lib/.pyc/.build` 会被判 `passed:false`。依赖 T6、T13。MUST NOT 依赖本地配置；MUST NOT 以探针证据作 CI 门禁（CI 无微信）。
      Parallelization: Wave 3 | Blocked by: T6,T13 | Blocks: F1
      References: requirements.txt; tests/*; tools/check_publication.py; README.md
      Acceptance criteria: 全新 clone push 后两 job 全绿（截图/日志）。
      QA: happy = CI 绿；failure = 无。Evidence .omo/evidence/task-17-wx-helper-optimization.txt
      Commit: Y | ci: add windows+linux test and check workflow
      Recommended task executor category: unspecified-low — CI YAML.

- [ ] 18. P15：`--check` 去掉硬编码的 LLM 插件名
      What to do / Must NOT do: `main.py:58` 改为基于 manifest/requires（如新增 `requires: ["llm.chat"]` 或从插件元数据）判定哪些插件需要校验 LLM 配置。MUST NOT 硬编码 `idiom_chain/chat_summary/mention_chat`。
      Parallelization: Wave 3 | Blocked by: — | Blocks: F1
      References: main.py:55-60; plugins/*/plugin.json; wxbot_framework/plugins.py:126-148
      Acceptance criteria: 新增一个 LLM 插件后 `--check` 自动校验其 LLM 配置。
      QA: happy = 新增插件被覆盖；failure = 旧硬编码漏检则 RED。Evidence .omo/evidence/task-18-wx-helper-optimization.txt
      Commit: Y | refactor(main): drive --check llm validation from manifest
      Recommended task executor category: quick — small refactor.

- [ ] 19. P4：`active` 竞态（enqueue/status 与 deactivate 同步）
      What to do / Must NOT do: `plugins.py:44` 的 `enqueue` 检查放进 `callback_lock`（或把 `active` 换成 `threading.Event`）；`status()` 在锁内快照计数。MUST NOT 让消息进正在退出的队列。**先写 RED 测试。**
      Parallelization: Wave 4 | Blocked by: — | Blocks: F2
      References: wxbot_framework/plugins.py:31-106,250-270; tests/test_framework.py:160-211
      Acceptance criteria: 用并发压测假件（多线程 enqueue + deactivate）断言无"投进已退出队列"的丢失窗口。
      QA: happy = 压测 0 丢失；failure = 旧实现偶发丢失则 RED。Evidence .omo/evidence/task-19-wx-helper-optimization.txt
      Commit: Y | fix(plugins): synchronize active-state reads
      Recommended task executor category: deep — concurrency.

- [ ] 20. P5：`load_one`/`disable` 不在持 manager 锁时等待
      What to do / Must NOT do: `plugins.py:195-197,210-213` 改为锁内快照、锁外 `deactivate()`/`join()`。MUST NOT 让长回调停摆 dispatch 最长 2s。**先写 RED。**
      Parallelization: Wave 4 | Blocked by: — | Blocks: F2
      References: wxbot_framework/plugins.py:187-224; tests/test_framework.py:195-211
      Acceptance criteria: 用**慢回调假插件**（on_message 阻塞 >2s）断言重载时 dispatch 不被阻塞（时序断言）。
      QA: happy = dispatch 延迟有界；failure = 旧实现超时则 RED。Evidence .omo/evidence/task-20-wx-helper-optimization.txt
      Commit: Y | fix(plugins): release manager lock before joining
      Recommended task executor category: deep — concurrency.

- [ ] 21. P10：`members.record/clear` 事务原子性
      What to do / Must NOT do: `members.py:504-505,236-242` 把 `rollover()` 移到外层 `with db` 之前，或让内部 SQL 走调用者事务，避免嵌套 `with conn:` 提前提交。MUST NOT 让 day-bump/DELETE 与 INSERT 分离提交。**先写 RED。**
      Parallelization: Wave 4 | Blocked by: — | Blocks: F2
      References: wxbot_framework/members.py:215-242,493-513; tests/test_chat_summary.py:90-130
      Acceptance criteria: 注入 INSERT 失败，断言 rollover 副作用被回滚（或原子提交）。
      QA: happy = 失败时无部分提交；failure = 旧实现部分提交则 RED。Evidence .omo/evidence/task-21-wx-helper-optimization.txt
      Commit: Y | fix(members): make record/clear atomic with rollover
      Recommended task executor category: deep — DB transaction semantics.

- [ ] 22. P14：`llm.py` 捕获 `http.client.HTTPException`
      What to do / Must NOT do: `llm.py:53-68` 增加 `except http.client.HTTPException`（含 IncompleteRead）→ 包装为 `LLMError`。MUST NOT 让原始异常穿出契约。**先写 RED。**
      Parallelization: Wave 4 | Blocked by: — | Blocks: F2
      References: wxbot_framework/llm.py:44-75; tests/test_llm.py:59-67
      Acceptance criteria: 模拟 IncompleteRead，`complete_json` 抛 `LLMError` 而非原始异常。
      QA: happy = 抛 LLMError；failure = 旧实现抛 IncompleteRead 则 RED。Evidence .omo/evidence/task-22-wx-helper-optimization.txt
      Commit: Y | fix(llm): wrap http.client exceptions as LLMError
      Recommended task executor category: quick — small.

- [ ] 23. P12：`dialogue.py`/`summary.py` 按 UTF-8 字节校验长度
      What to do / Must NOT do: 把 600 字符上限改为 ≤1023 **字节**校验（与 `send_text` 一致）。MUST NOT 让通过校验的回复在发送时才失败。**先写 RED。**
      Parallelization: Wave 4 | Blocked by: — | Blocks: F2
      References: wxbot_framework/dialogue.py:28-31; wxbot_framework/summary.py:79-83; wxbot_framework/sdk.py:74-80
      Acceptance criteria: 340 个 CJK 字符的回复被接受，400 个被拒绝。
      QA: happy = 边界正确；failure = 旧实现放行超限则 RED。Evidence .omo/evidence/task-23-wx-helper-optimization.txt
      Commit: Y | fix(llm): validate reply length in utf-8 bytes
      Recommended task executor category: quick — small.

- [ ] 24. P11：`split_text` 单字符超限处理
      What to do / Must NOT do: `summary.py:34-46` 对 `len(char.encode) > byte_limit` 的字符抛错或跳过，避免产出超限块。MUST NOT 让块超过 `byte_limit`。**先写 RED。**
      Parallelization: Wave 4 | Blocked by: — | Blocks: F2
      References: wxbot_framework/summary.py:34-46; tests/test_chat_summary.py:258-263
      Acceptance criteria: 4 字节字符 + byte_limit=3 时抛错或跳过，不返回超限块。
      QA: happy = 边界正确；failure = 旧实现超限则 RED。Evidence .omo/evidence/task-24-wx-helper-optimization.txt
      Commit: Y | fix(summary): handle single-char overflow in split_text
      Recommended task executor category: quick — small.

- [ ] 25. P13：`own_group_command` 与 `summary.CONTROL` 判定统一
      What to do / Must NOT do: `text.py:12-15` 改为首词 + 集合匹配，与 `summary.CONTROL` 共用一份控制命令集。MUST NOT 保留 `list in tuple` 的脆弱写法。**先写 RED。**
      Parallelization: Wave 4 | Blocked by: — | Blocks: F2
      References: wxbot_framework/text.py:8-15; wxbot_framework/summary.py:10; tests/test_text_routing.py:62-67
      Acceptance criteria: `成语接龙 开始` 在两侧判定一致。
      QA: happy = 两侧一致；failure = 旧实现不一致则 RED。Evidence .omo/evidence/task-25-wx-helper-optimization.txt
      Commit: Y | refactor(text): share the control-command set
      Recommended task executor category: quick — small.

- [ ] 26. N9：`read_pb_string` 增加堆容量门限
      What to do / Must NOT do: `selftest_bridge.cpp:430-439` 套用 `try_read_source` 的 `capacity==15 || 16..1MB` 门限。MUST NOT 接受任意大 capacity。
      Parallelization: Wave 5 | Blocked by: — | Blocks: F2
      References: native/selftest_bridge.cpp:430-439; native/text_bridge.cpp:227-239
      Acceptance criteria: 构造坏 capacity 用例被拒绝，不越界读。
      QA: happy = 正常读取；failure = 坏 capacity 被拒。Evidence .omo/evidence/task-26-wx-helper-optimization.json
      Commit: Y | fix(native): bound read_pb_string heap capacity
      Recommended task executor category: quick — small native.

- [ ] 27. N10：`release_shared` 缓存函数指针，消除虚析构后读控制块
      What to do / Must NOT do: `selftest_bridge.cpp:143-150` 在调用 `table[0]` 前缓存第二个函数指针，避免 UAF 读。MUST NOT 在可能释放后读 `control`。
      Parallelization: Wave 5 | Blocked by: — | Blocks: F2
      References: native/selftest_bridge.cpp:140-153
      Acceptance criteria: 真机连发 100 条无崩溃、无 UAF（探针/稳定性）。
      QA: happy = 稳定；failure = 无。Evidence .omo/evidence/task-27-wx-helper-optimization.json
      Commit: Y | fix(native): cache vtable fn before destroy
      Recommended task executor category: quick — small native.

- [ ] 28. N11：`MH_ERROR_ALREADY_INITIALIZED` 在 4 个 arm 路径统一处理
      What to do / Must NOT do: `start_receive`/`WxStartTextReceiver`/`WxArmMentionProbe` 与 `ensure_mention_factory` 一致地容忍该状态并记录 `initialized_here`。MUST NOT 误报 arm 失败。
      Parallelization: Wave 5 | Blocked by: — | Blocks: F2
      References: native/text_bridge.cpp:41-64,272-292,382-399; native/selftest_bridge.cpp:483-505
      Acceptance criteria: 先发艾特再启接收的调用序不再报错。
      QA: happy = 两种调用序都成功；failure = 旧实现报错则 RED。Evidence .omo/evidence/task-28-wx-helper-optimization.json
      Commit: Y | fix(native): tolerate already-initialized MinHook in all arms
      Recommended task executor category: quick — small native.

- [ ] 29. N13：探针越界读边界
      What to do / Must NOT do: `text_bridge.cpp:365-367` 的 hex dump 限制在已核对的对象长度内。MUST NOT 无条件 dump 0x798 字节。
      Parallelization: Wave 5 | Blocked by: — | Blocks: F2
      References: native/text_bridge.cpp:324-370
      Acceptance criteria: 小对象不越界读（探针稳定）。
      QA: happy = 探针稳定；failure = 无。Evidence .omo/evidence/task-29-wx-helper-optimization.json
      Commit: Y | fix(native): bound probe hex dump
      Recommended task executor category: quick — small native.

- [ ] 30. N14：收敛/移除 `WXBOT_AT_CHAT_PROBE` 调试后门
      What to do / Must NOT do: `text_service.py:146-154` 的 `WXBOT_AT_CHAT_PROBE` 接收后门改为**显式配置开关（默认关闭）**，README 标注；在 F3 通过前**保留**该通道（T4/T7 需要它），仅收敛触发条件。MUST NOT 让任意群成员触发本地写文件；MUST NOT 波及 `WxArmMentionProbe`(text_bridge.cpp:382-400) 等 C1 探针所需的 hook。
      Parallelization: Wave 5 | Blocked by: — | Blocks: F2
      References: native/text_service.py:146-154; bot_config.example.json
      Acceptance criteria: 默认配置下发送该标记不写文件；开启开关后才写。
      QA: happy = 默认不写；failure = 旧实现写入则 RED。Evidence .omo/evidence/task-30-wx-helper-optimization.txt
      Commit: Y | fix(native): gate at-receive probe behind a config flag
      Recommended task executor category: quick — small.

- [ ] 31. N15：指纹校验缓存，避免双次 192MB SHA-256
      What to do / Must NOT do: 在进程内缓存 `core_fingerprint` 判定（仅在 `--arm`/`--listen` 重算），并去掉 Python 侧冗余全量哈希（或复用）。MUST NOT 每次操作都全量哈希。
      Parallelization: Wave 5 | Blocked by: — | Blocks: F2
      References: native/selftest_bridge.cpp:106-138; native/run_selftest.py:186-191
      Acceptance criteria: 首次后同进程指纹校验命中缓存（日志/耗时）。
      QA: happy = 耗时下降；failure = 无。Evidence .omo/evidence/task-31-wx-helper-optimization.txt
      Commit: Y | perf(native): cache core fingerprint verdict
      Recommended task executor category: unspecified-high — native perf.

- [ ] 32. N16：完成回调移出磁盘 IO
      What to do / Must NOT do: `text_bridge.cpp:86-103`/`selftest_bridge.cpp:322-338` 的回调改为内存缓冲，由清理远程线程/Python 侧落盘。MUST NOT 在微信完成线程做 fopen/fprintf。
      Parallelization: Wave 5 | Blocked by: — | Blocks: F2
      References: native/text_bridge.cpp:86-103; native/selftest_bridge.cpp:322-338
      Acceptance criteria: 发送路径无回调内磁盘写；报告仍可读。
      QA: happy = 报告正确且无回调 IO；failure = 无。Evidence .omo/evidence/task-32-wx-helper-optimization.json
      Commit: Y | perf(native): buffer completion report off the callback thread
      Recommended task executor category: deep — native.

- [ ] 33. P7：所有人格统一输出后处理（防提示注入外溢）
      What to do / Must NOT do: 为所有人格加**最小策略**的统一后处理：剥离代码块/链接语法、剥离控制字符、按 UTF-8 字节截断（≤1023）；非嘴臭路径也生效。MUST NOT 让被劫持的模型输出原样进群。**策略边界固定**：不做内容审查/敏感词库/语义过滤（显式 out-of-scope）。**先写 RED（2-3 个具名用例）。**
      Parallelization: Wave 5 | Blocked by: — | Blocks: F2
      References: wxbot_framework/personality.py:34-57; wxbot_framework/summary.py:79-83; wxbot_framework/dialogue.py:28-31; plugins/mention_chat/**init**.py; plugins/chat_summary/**init**.py
      Acceptance criteria: 注入含 Markdown/URL/控制符的回复被净化后再发送。
      QA: happy = 净化生效；failure = 旧实现原样发送则 RED。Evidence .omo/evidence/task-33-wx-helper-optimization.txt
      Commit: Y | fix(personality): sanitize all persona outputs
      Recommended task executor category: deep — security + cross-plugin.

- [ ] 34. P6：`response_guard` ABBA 死锁脚枪（文档 + 硬化）
      What to do / Must NOT do: 文档化"绝不在 members guard 内发起发送"；或改造 API 使其在 send 前重入安全。MUST NOT 留下可死锁的公开 `with` 形态。**先写 RED（复现 ABBA）。**
      Parallelization: Wave 6 | Blocked by: — | Blocks: F2
      References: wxbot_framework/sdk.py:196-202; wxbot_framework/members.py:318-324,358-364; plugins/chat_summary/**init**.py:36-46
      Acceptance criteria: 用**双线程 + 阻塞后端假件**并发 guard+send，断言不再死锁（超时断言）。
      QA: happy = 不死锁；failure = 旧实现死锁则 RED。Evidence .omo/evidence/task-34-wx-helper-optimization.txt
      Commit: Y | fix(sdk): remove guard/send lock-order hazard
      Recommended task executor category: deep — concurrency.

- [ ] 35. P16：`backend.py` 去重
      What to do / Must NOT do: 抽取 `NativeFileHelperBackend`/`NativeTextBackend` 的公共 native 路径初始化。MUST NOT 改变行为。
      Parallelization: Wave 6 | Blocked by: — | Blocks: F2
      References: wxbot_framework/backend.py:15-77
      Acceptance criteria: 行为等价（T38 测试 + `--check` 绿）。
      QA: happy = 等价；failure = 无。Evidence .omo/evidence/task-35-wx-helper-optimization.txt
      Commit: Y | refactor(backend): extract shared native init
      Recommended task executor category: quick — refactor.

- [ ] 36. L6：`main.py` 缺配置时给友好错误
      What to do / Must NOT do: `main.py:45` 捕获 `FileNotFoundError`/`JSONDecodeError` → 打印"未找到/无效配置，请先运行 tools/init_config.py"并 exit 1。MUST NOT 打印原始 traceback。
      Parallelization: Wave 6 | Blocked by: — | Blocks: F2
      References: main.py:31-77; tools/init_config.py
      Acceptance criteria: `python main.py --config /tmp/nope.json` 打印友好中文提示、无 traceback。
      QA: happy = 友好提示；failure = 旧实现 traceback 则 RED。Evidence .omo/evidence/task-36-wx-helper-optimization.txt
      Commit: Y | fix(main): friendly error on missing config
      Recommended task executor category: quick — small.

- [ ] 37. 覆盖：`dialogue.py` 测试
      What to do / Must NOT do: 为 `Dialogue.reply` 写测试（previous/current 组装、嘴臭 SHORT_SYSTEM 选择、非法回复拒绝）。MUST NOT 只 import 不实例化。
      Parallelization: Wave 6 | Blocked by: — | Blocks: F2
      References: wxbot_framework/dialogue.py:17-32; tests/test_personality.py:13
      Acceptance criteria: 新测试文件覆盖 `Dialogue.reply` 全分支，全绿。
      QA: happy = 全绿；failure = 无。Evidence .omo/evidence/task-37-wx-helper-optimization.txt
      Commit: Y | test(dialogue): cover Dialogue.reply
      Recommended task executor category: quick — tests.

- [ ] 38. 覆盖：`backend.py` 两个原生后端类测试
      What to do / Must NOT do: 用假 native 模块覆盖 `NativeFileHelperBackend`/`NativeTextBackend` 的 `__init__` 缺失目录报错、`start/stop/send_text/send_at_text/diagnostics/chats` 透传。MUST NOT 依赖真实微信。（注：两个后端类在 `runtime.py:31-32` 的生产路径会被实例化，但**测试从未实例化其类方法**——真正缺口是类方法覆盖。）
      Parallelization: Wave 6 | Blocked by: — | Blocks: F2
      References: wxbot_framework/backend.py:15-77
      Acceptance criteria: 新测试覆盖两类全方法，全绿。
      QA: happy = 全绿；failure = 无。Evidence .omo/evidence/task-38-wx-helper-optimization.txt
      Commit: Y | test(backend): cover native backends
      Recommended task executor category: quick — tests.

- [ ] 39. 覆盖：`runtime.execute()` 控制台命令测试
      What to do / Must NOT do: 覆盖 status/chats/members/history/plugins/enable/disable/reload/help/quit 与未知命令报错。MUST NOT 只测 `run()` 循环。
      Parallelization: Wave 6 | Blocked by: — | Blocks: F2
      References: wxbot_framework/runtime.py:87-125
      Acceptance criteria: 每条命令的日志/状态断言，全绿。
      QA: happy = 全绿；failure = 无。Evidence .omo/evidence/task-39-wx-helper-optimization.txt
      Commit: Y | test(runtime): cover console command dispatcher
      Recommended task executor category: quick — tests.

- [ ] 40. 覆盖：`ConfigStore.load()` 校验矩阵测试
      What to do / Must NOT do: 覆盖每个拒绝分支（backend/llm_config_file/members_config_file/wechat_account/chat_allowlist/wechat_pid/reconnect_seconds/plugin_queue_size/plugins 结构/enabled 类型/log_level/console）。MUST NOT 只测 happy path。
      Parallelization: Wave 6 | Blocked by: — | Blocks: F2
      References: wxbot_framework/config.py:20-85
      Acceptance criteria: 每个分支一条 `assertRaises(ValueError)`，全绿。
      QA: happy = 全绿；failure = 无。Evidence .omo/evidence/task-40-wx-helper-optimization.txt
      Commit: Y | test(config): cover load rejection matrix
      Recommended task executor category: quick — tests.

- [ ] 41. 卫生：`.gitignore` + README 测试说明 + 原生回滚说明
      What to do / Must NOT do: 修改 `tools/check_publication.py`，在其扫描跳过逻辑中显式排除 `.omo/` 与 `.code-review-graph/`（**必须在工具代码内跳过**——该工具只排除 `.git`、不读 `.gitignore`，仅改 `.gitignore` 无效）；`.gitignore` 增加 `.omo/`、`.code-review-graph/`；README 增加"测试运行命令 + 前置（或已自举）"；`native/README.md` 增加"如何回退到已发布 DLL"。MUST NOT 改动与评审无关的内容。
      Parallelization: Wave 6 | Blocked by: — | Blocks: F2
      References: tools/check_publication.py:23-28,37-40; .gitignore; README.md; native/README.md
      Acceptance criteria: 在存在 `.omo/`（含证据）与 `.code-review-graph/` 的树上，`python tools/check_publication.py` 返回 `passed:true`；`git status` 干净；README 有可复制的测试命令。
      QA: happy = 检查通过；failure = 无。Evidence .omo/evidence/task-41-wx-helper-optimization.txt
      Commit: Y | docs: ignore tool dirs, document tests and native rollback
      Recommended task executor category: writing — docs.

- [ ] 42. 用 MSVC 重建并提交两个 DLL（原生交付）
      What to do / Must NOT do: 在所有原生 .cpp 改动完成后，于 Windows 运行 `native/build_bridges.ps1`，跑真机冒烟（发送/接收/艾特），把 `native/build/selftest_bridge.dll` 与 `native/build_text/text_bridge.dll` 更新并提交（二进制），随后清理 `native/.build` 与中间产物。MUST NOT 留下 `.obj/.exe/.lib`（否则 `check_publication.py` 失败）。
      Parallelization: Wave 7 | Blocked by: T2,T3,T8,T9,T10,T11,T14,T26-T32 | Blocks: F3
      References (executor has NO interview context - be exhaustive): native/build_bridges.ps1:20-40; native/build/; native/build_text/; tools/check_publication.py:28,37-40; .gitignore:33-35
      Acceptance criteria（Windows 构建 + 代理核对）: 两个 DLL 更新且已提交；真机冒烟通过；干净树 `python tools/check_publication.py` 返回 `passed:true`。
      QA scenarios (name the exact tool + invocation): happy = 重建后提交 + 冒烟 + 发布检查通过；failure = 残留构建产物 → `check_publication` 返回 `passed:false`（RED）。Evidence .omo/evidence/task-42-wx-helper-optimization.json
      Commit: Y | build(native): rebuild and commit both bridge DLLs
      Recommended task executor category: unspecified-high — build + binary commit.

- [ ] 43. 建立真机探针执行与证据回传通道
      What to do / Must NOT do: 定义并文档化"探针在 Windows 机器上如何运行、JSON 证据如何取回 `.omo/evidence/`"（代理直达 / SSH-RDP / 用户运行脚本回传），并**核对真机 `Weixin.dll` 与 `compatibility.json` 指纹一致**；不一致则停下走 owner 决策。MUST NOT 伪造证据。
      Parallelization: Wave 1 | Blocked by: — | Blocks: T7
      References (executor has NO interview context - be exhaustive): native/run_selftest.py:146-287; native/compatibility.json; native/selftest_bridge.cpp:106-137
      Acceptance criteria（Windows 执行；代理核对回传证据）: `.omo/evidence/probe-channel.md` 记录通道 + 指纹比对结果；一致返回 ok，不一致明确阻塞。
      QA scenarios (name the exact tool + invocation): happy = 指纹一致且通道可用；failure = 指纹不一致 → 阻塞并报告。Evidence .omo/evidence/task-43-wx-helper-optimization.md
      Commit: N | —
      Recommended task executor category: unspecified-high — ops/coordination.

## Final verification wave

> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.

- [ ] F1. Plan compliance audit — 逐条核对每个实现 todo：References 是否按述实现、验收标准是否满足、`.omo/evidence/task-N-*.{json,txt,md}` 是否存在且有效；CI 两 job 绿。任一缺失 = 不通过。
      Recommended task executor category: unspecified-high — cross-checks the whole plan.
- [ ] F2. Code quality review — 独立 `ultrabrain`/oracle reviewer 复核 Python + C++ 改动：无回归、无新增裸 except、改动文件 `lsp_diagnostics` 干净、并发/内存安全改动有对应测试。发现按 blocker 资格判定，只修命中验收标准的项。
      Recommended task executor category: ultrabrain — high-rigor review.
- [ ] F3. Real manual QA — Windows 真机运行原生探针往返（发送/接收/艾特）+ 触发 N1/N4 的注入错误用例确认宿主存活；macOS 运行 `python -m unittest discover -s tests`（仅 Windows-only skip）+ `python main.py --check` + `python tools/check_publication.py`。捕获真实产物。
      Recommended task executor category: unspecified-high — hands-on QA.
- [ ] F4. Scope fidelity — 确认 Must have 全部完成、Must NOT have 无一越界（未重写注入架构、未改微信、未伪造原生验证、未做大扫除）。
      Recommended task executor category: unspecified-high — scope audit.

## Commit strategy

- **一个已验证增量 = 一个原子提交**；不做一个"大杂烩"收尾提交。
- Python 改动：先 RED 后 GREEN，测试通过后提交。原生改动：真机探针通过后提交。
- 提交前 `git log --oneline -20` 学习仓库现有风格（中英混合、`type(scope): summary`）。
- **不提交** `.omo/`、`.runtime/`、`logs/`、本地配置（`bot_config.json` 等）。

## Success criteria

- **自举**：全新 clone（无本地配置）在 Windows 全套单测绿、Linux 纯 Python 子集绿；CI 两 job 绿。
- **原生**：4 个逆向契约有真机判定记录；N1/N4 的注入错误不再崩溃宿主；艾特 `applied` 可校验。
- **Python**：关停不再无限阻塞；接收回调异常不丢消息；并发竞态（active/manager/guard）与事务原子性有测试守护。
- **安全**：所有人格输出统一净化；调试后门默认关闭。
- **覆盖/卫生**：`dialogue.py`/`backend.py`/`runtime.execute()`/`config` 校验有测试；`check_publication.py` 通过；`git status` 干净。
- **F1–F4 全部 APPROVE**。
