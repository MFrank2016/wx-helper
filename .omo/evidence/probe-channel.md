# T43 — 真机探针执行与证据回传通道

## 通道（已定案）
- **执行方**：用户（Windows x64 + 微信 4.1.15.13 + MSVC x64）。
- **回传方**：用户把探针输出 JSON 放入本仓库 `.omo/evidence/`，代理据此判定（T7）。
- **代理不直达 Windows 机器**（macOS 无法编译/运行原生层）。

## 步骤（在 Windows 仓库根目录执行）

### 1. 构建桥接 DLL（需 MSVC x64 开发者命令环境）
```powershell
native\build_bridges.ps1 -Target all
```

### 2. 指纹核对（必须先通过，否则整个原生阶段作废）
```powershell
# 期望 Length == 201552944
(Get-Item "<微信安装目录>\Weixin.dll").Length
# 期望 10f8e995453e2da46d4f2b5080cd6da1f13cc5147746adc119ceae38cb039de5
(Get-FileHash "<微信安装目录>\Weixin.dll" -Algorithm SHA256).Hash
```
- **一致** → 继续第 3 步。
- **不一致** → **停下**，把实际大小/哈希与客户端版本回传，走 owner 决策（换版本安装包，或评估是否更新 `compatibility.json` 指纹——后者属评审范围外）。

### 3. 运行契约探针（一次插桩发送，回传四项判定）
```powershell
python native\run_probe.py > .omo\evidence\task-1-wx-helper-optimization.json
```
> `native/run_selftest.py` 在加载 DLL 前会**自行**校验指纹；不匹配会返回「微信核心 DLL 指纹不匹配，拒绝加载」，无需人工先查。
> 探针会向**文件传输助手**发送一条 `WXBOT_PROBE`（仅自己可见，安全）。

### 4. 回传
把 `.omo\evidence\task-1-wx-helper-optimization.json` 内容贴回给代理。

## 探针输出含义（供 T7 判定 → 决定 T8–T11 的分支）
| 字段 | 契约 | 判定 |
|---|---|---|
| `contracts.completion_refcount.builder_consumed` | N3 | `1` = 构建器消费引用（保留现逻辑）；`0` = 未置空（改引用计数） |
| `contracts.completion_refcount.completion_destroys` | N3 | 观察释放次数（>1 异常） |
| `contracts.request_string_move_or_copy.request_body_length` | N6 | `== 原文长度` = 深拷贝；`0`/变化 = 移动 |
| `contracts.scheduler_function_copy.lease_destroys` | N12 | `1` = 正常拷贝语义；`>1`/崩溃 = memcpy |
| `contracts.error_allocator_header.native_status` | N2 | 分配器头证据 |
| （无结论） | — | inconclusive → 采用最保守分支并标注「未验证」 |

## 指纹比对结果
- **待用户在 Windows 上执行后回填。**

## 状态
- 通道：**已文档化**（本文）。
- 指纹比对结果：**待用户回传**。
- 探针 JSON：**待用户回传**。
