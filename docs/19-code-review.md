# 19 · Phase 0 代码审查记录

> 审查日期：2026-11-18（第十五会话）
> 审查范围：Phase 0 全部代码（`aureus/`、`scripts/check.py`）、关键测试与设计文档中的承诺
> 审查方式：**只读**逐行阅读 + 5 个可复现的验证实验；**本次没有修改任何代码**
> 测试基线：`uv run python -m pytest -q` → 629 passed, 1 skipped（20.6 秒）
> 后续修复任务见第 7 节；需要用户拍板的事项见第 8 节。

---

## 1. 结论摘要

代码整体质量高于常见的 AI 生成项目：分层清楚、类型注解齐全、注释解释了「为什么」、测试数量真实。
但存在 **3 个能让安全承诺失效的问题**，其中 2 个已用脚本实测复现，不是推测：

| 编号 | 问题 | 严重程度 | 状态 |
|---|---|---|---|
| S1 | 月度 token「硬上限」可被并发调用绕过 | 严重 | 已实测（上限 100，实际用掉 290） |
| S2 | 服务商返回的用量字段为 `None` 时按 0 记账，还标记为「已知」 | 严重 | 已实测 |
| S3 | 「数据库层禁止修改和删除」可被 `INSERT OR REPLACE` 绕过 | 严重 | 已实测 |

另外有 5 个高优先级问题（导入包没有大小上限、多文件导入不是原子操作、取消时用量不入账、
litellm 内部重试成功时漏记、主密钥明文落盘与读改写竞态），以及 8 个中等问题、7 个质量类问题。

**一句话结论**：安全机制的方向都对，但**边界和并发**是这套代码最薄弱的环节——这正是 AI 生成代码最典型的短板。

---

## 2. 严重问题

### S1. 月度预算「硬上限」可以被并发绕过

- **位置**：`aureus/safety/gateway.py:93`（注释）、`:124`（读账本）、`:133`（比较上限）、`:150`（记账）
- **代码事实**：

```python
# gateway.py:93
# 从这里到调用器官之间没有 await，所以并发的调用不会同时通过上面的检查而漏记。

# gateway.py:124-134
used = self._ledger.tokens_used_in_month(self._clock.now())
limit = self._config.monthly_token_limit
if limit is not None and used >= limit:
    raise BudgetExceededError(...)
```

检查（第 124 行）和记账（第 150 行）之间隔着 `await self._bus.send(request)`。这段时间里另一个调用读到的是同一个「已用」数字，于是两边同时通过检查、同时发请求、最后都记账——**账本是记全了，但钱已经花超了**。

- **实测**：上限 100，先记 90 token，再同时发起两个各花 100 token 的调用。
  结果：两个都返回成功，账本合计 **290 token**，超出上限 190。
- **影响**：`monthly_token_limit` 目前在本机是 `None`（不限），但一旦用户按文档设置上限，会以为「到了就停」，实际不是。
- **AI 失败模式**：并发盲区 + 注释把「不漏记」误当成「不超支」。
- **置信度**：确定（实测）。
- **修复方向**：调用前按 `max_tokens` **预留**额度，结束后按真实用量结算并释放差额；或者让「检查→占位」之间没有 `await`。见任务 F1。

### S2. 用量字段为 `None` 时按 0 记账，并标记为「已知」

- **位置**：`aureus/organs/llm_usage.py:44-52`（`extract_usage`）、`:30-32`（`_as_count`）；`aureus/organs/llm_adapter.py:307`（`nothing_used`）、`:317`（cost）
- **代码事实**：

```python
# llm_usage.py:44-52
usage = getattr(response, "usage", None)
if usage is None:
    return TokenUsage(known=False)      # 只有「整个 usage 缺失」才算未知
prompt = _as_count(getattr(usage, "prompt_tokens", 0))
completion = _as_count(getattr(usage, "completion_tokens", 0))
```

```python
# llm_usage.py:30-32
def _as_count(value: object) -> int:
    # 服务商返回的字段可能缺失、是 None 或负数：一律当 0
    return value if isinstance(value, int) and value > 0 else 0
```

如果服务商返回的是 `usage = {"prompt_tokens": None, "completion_tokens": None}`，三个数都会变成 0，
但 `known` 仍是 `True`——于是**不会走保守估算**，预算按 0 算，费用按 0 算（`nothing_used` → `cost=0.0`）。
实际这次请求是花了钱的。

顺带：`_as_count(12.0)` 返回 0（实测），部分服务商用浮点返回用量时会被静默丢弃。

- **AI 失败模式**：边界遗漏 + 注释宣称「不让坏数据污染账本」，实际效果是「让坏数据变成免费」。
- **置信度**：确定（实测）。
- **修复方向**：`usage` 对象存在但字段缺失/全为 `None`/不是整数时，一律视为 `known=False` 并走保守估算。见任务 F2。

### S3. 「数据库层禁止修改和删除」可被 `INSERT OR REPLACE` 绕过

- **位置**：`aureus/safety/ledger.py:128-141`、`aureus/timeline/database.py:117-132`；被违背的承诺在 `docs/06-safety.md:142`
- **代码事实**：只建了 `BEFORE UPDATE` 和 `BEFORE DELETE` 两个触发器：

```sql
CREATE TRIGGER IF NOT EXISTS no_update_ledger
BEFORE UPDATE ON ledger BEGIN
    SELECT RAISE(ABORT, 'Ledger records are immutable: UPDATE is not allowed');
END
```

SQLite 的 `INSERT OR REPLACE` 会先删除冲突行再插入，而**默认配置下 REPLACE 不触发 DELETE 触发器**
（只有开了 `recursive_triggers` 才触发）。所以普通 SQL 就能改掉一条审计记录。

- **实测**：`UPDATE` / `DELETE` 被正确拦住（报 `no`）；`INSERT OR REPLACE` 把原行覆盖成了新值。
- **AI 失败模式**：文档与实现不符 + 「以为建了触发器就不可变」的错误假设。
- **置信度**：确定（实测）。
- **修复方向**：加 `BEFORE INSERT` 触发器拒绝已存在的主键；同时把 06 号文档改成诚实说法。见任务 F3。

---

## 3. 高优先级问题

### H1. 导入包没有大小/条目上限；manifest 在包内，不构成「防篡改」

- **位置**：`aureus/core/portability.py:185-234`
- **事实**：
  1. manifest 就在同一个 zip 里，改包的人可以同时改 manifest 与 sha256。它能发现**传输损坏**，不能判断**来源可信**。现在的报错文字是「包可能损坏或被改过」，后半句给人错误安全感。
  2. manifest 声明多大就校验多大（`getinfo(name).file_size != size`），然后 `bundle.read(name)` 整个读进内存——**没有单文件、总量、条目数上限**。恶意 zip 可以声明一个 5 GB 的 `data/aureus.db` 把内存吃光（压缩炸弹）。
- **AI 失败模式**：把「校验和」误当成「认证」+ 漏掉资源上限。
- **置信度**：确定（代码事实）；压缩炸弹未实际构造。
- **修复方向**：加单文件/总大小/条目数上限；报错文字改成「用于发现损坏，不用于判断来源」。见任务 F4。

### H2. 多文件导入不是原子操作

- **位置**：`aureus/core/portability.py:237-270`
- **事实**：先写进 `data/.import-*/` 暂存目录（这步是对的），但最后是**逐个 `replace`**：

```python
for name in files:
    destination = paths.home / name
    destination.parent.mkdir(parents=True, exist_ok=True)
    (staging / name).replace(destination)
```

第 3 个文件时断电/被占用，前 2 个已落地，数据变成「新旧混合」，比完全没导入更难排查。
另外备份名只精确到秒（`before-import-%Y%m%d-%H%M%S.zip`），同一秒内两次 `--force` 会覆盖上一份备份。
- **置信度**：确定。
- **修复方向**：先全部改名成 `.bak`，全部替换成功后再删；失败回滚；备份名加毫秒或序号。见任务 F4。

### H3. 器官调用被取消时，用量不入账

- **位置**：`aureus/heartbeat/main_loop.py:508-544`（关闭时取消）、`aureus/safety/gateway.py:94-108`（只在返回/异常时记账）
- **事实**：`_record_abandoned` 自己写着「这次可能已经用掉的 token 没有记进账本」。这是有意的取舍（先保证关得掉），但意味着 Ctrl+C 或关闭超时反复发生时，预算会持续偏低。叠加 S1 后，实际花费可能明显高于账本。
- **置信度**：确定（行为确定，影响取决于使用习惯）。
- **修复方向**：至少在 `aureus usage` 里显示「有 N 次调用被放弃，费用未知」，让账本的「不全」可见。见任务 F5。

### H4. litellm 内部重试成功时只上报最后一次用量（很可能）

- **位置**：`aureus/organs/llm_adapter.py:328-330`（`num_retries`）、`:343-356`（失败路径）、`:357-367`（成功路径）
- **事实**：失败路径按 `1 + max_retry` 估算，是**保守的**；但成功路径只读取最后一个响应的 `usage`。如果 litellm 内部重试过，前面那几次若被计费，不会体现在账本里。
- **AI 失败模式**：「重试看不见就按最多算」只做到了一半。
- **置信度**：很可能（未构造真实重试）。
- **修复方向**：关掉 litellm 内部重试、改在器官层自己重试（每次都有 `CallAttempt` 可记）；或在成功路径也按最坏情况补记。见任务 F6。

### H5. 主密钥明文落盘 + 密钥库读改写竞态

- **位置**：`aureus/common/vault.py:110-116`（主密钥明文 JSON）、`:35-51`（原子写注释）、`:155-167`（`_save`）
- **事实**：
  1. `secrets.key` 里是明文主密钥。这符合 D36（不加口令）的设计，但 `_write_atomically` 的注释也承认 mkstemp 的 0600 **只在类 Unix 上有效**；Windows 上没有额外收紧权限，依赖数据根目录本身的 ACL。数据根目录若在共享盘或 `C:\` 下，同机其他账户可能读到主密钥与密钥库。
  2. `_save` 是「读—改—写」，两个 `aureus keys set` 同时运行可能丢掉一条。
- **置信度**：确定（平台差异）/ 很可能（竞态）。
- **修复方向**：Windows 上显式设置 ACL（或用 DPAPI 保护主密钥）；`_save` 加文件锁。见任务 F7。

---

## 4. 中优先级问题

| 编号 | 问题 | 位置 | 说明 |
|---|---|---|---|
| M1 | 日志脱敏是「精确串黑名单」 | `aureus/common/logging_setup.py:95-105` | 只挡启动时已知的密钥原文；改过的、URL 编码的、第三方库自己拼的挡不住。方向对，能力被高估。 |
| M2 | 心跳里同步写 SQLite | `aureus/heartbeat/main_loop.py:283-295`、`:369-379`、`:438-444` | 直接违反项目守则「心跳循环里不能有阻塞操作」。SQLite 默认锁等待实测可达 **5.5 秒**，会顶掉 60 秒的 tick。文档承认了（「以后发现卡再放进线程」），但与守则冲突。 |
| M3 | `_as_count` 丢弃浮点、放过 bool | `aureus/organs/llm_usage.py:30-32` | `12.0 → 0`（实测）；`_estimate_usage` 挡了 bool，这里没挡。 |
| M4 | 时间线不归一化到 UTC | `aureus/timeline/database.py:164-183` | 存的是 `isoformat()` 原样。现在所有时钟都返回 UTC 所以没事；将来有人传本地时区，按字符串比较月份就会错。账本那边做了 `ensure_aware`（`ledger.py:70-71`），两边不一致。 |
| M5 | 用户内容原样拼进对话行 | `aureus/heartbeat/context.py:123-126` | 用户发一行 `\n你：……` 就能伪造发言。目前只有终端可信用户；Phase 1 接网页后是真问题。 |
| M6 | 锁文件提示「可以删除」 | `aureus/identity/service.py:129-131` | filelock 是建议锁，删文件不解除互斥，容易让用户误判。 |
| M7 | `SnapshotsConfig` 少了 `strict=True` | `aureus/common/config.py:102-108` | 与另外三个配置类不一致；`max_total_megabytes = "1024"` 会被静默转换，和「安全设置写错要报错」的初衷相反。 |
| M8 | 快照清理的 `stat()` 异常没接 | `aureus/core/snapshots.py:30-51` | 某个文件恰好被删/没权限时启动会抛异常；注释说「删不掉只警告」，但只覆盖了 `unlink`。 |

---

## 5. 低优先级问题（质量与「AI 幻觉」痕迹）

1. **大量未接真实来源的状态**（违反「没有装饰」原则）：`on_positive_social_interaction`、`on_social_rejection`、`on_error_or_failure`、`on_goal_achieved`、`consume_energy_for_*`、`generate_behavioral_constraints`、`priority_modifiers`、`risk_aversion`、`social_motivation`、`working_memory_items`、`planning_depth`、`energy_baseline`、`energy_recovery_rate_sleep` 都没有调用方。其中 `sleep_debt_hours` 永远是 0，却占稳态超载 **20% 权重**（`aureus/core/biomodel.py:371`）。
2. **注释与实现不符**：`fatigue_from_phase` 说「1 = 结束（需要休息）」，但 `sin(π·1) = 0`（`biomodel.py:128-134`）；melatonin / alertness 声称 0–2 范围，实际是 `[-0.6, 1.0]` 和 `[0.3, 0.9]`（`:77-88`）；`_interpolate` 的回绕分支永远走不到（`:90-111`）；`trigger_*` 的 `reason` 参数被忽略（`:377-399`）。
3. `AllostasisModel.burnout_risk` 返回英文字符串和 `days_until_collapse`（「还有几天崩溃」）这种没有依据的数字（`biomodel.py:669-706`）；`emotion_label` / `mood_text` 没有调用方。
4. `Secrets.llm_api_key` 是遗留字段，现在只用于日志脱敏（`aureus/common/config.py:151`）。
5. 交接文档写 `aureus_home/config/model_config.toml`，代码读的是 `models.toml`（`aureus/common/paths.py:107`）——要么文档过时，要么本机文件名不对，需确认。
6. ~~README 写计划 AGPL-3.0，仓库里没有 `LICENSE`~~：已在第十四会话解决（加入 AGPL-3.0 全文，GitHub 已识别）。
7. `CognitiveObserver` 里的激素基线硬编码 `0.5/0.4`，与 biomodel 默认值重复（`aureus/core/cognitive_layer.py:125-128`、`:167-172`），改一处忘另一处会悄悄漂移。

---

## 6. 已确认没问题的部分

- **导出不含密钥**：`exportable_paths` 是显式候选列表，不含 vault / key / `.env`，并跳过符号链接与数据根目录之外的文件（`aureus/core/portability.py:60-78`）。
- **导入路径穿越防护到位**：拒绝 `..`、反斜杠、盘符/冒号、绝对路径、白名单外路径、重复条目；先校验后落地（`:172-182`、`:185-211`）。
- **UPDATE / DELETE 触发器确实生效**（实测），CHECK 约束挡住负数。
- **密钥库**：Fernet 带完整性校验；主密钥丢失或库被篡改时报错，**不会回退到明文 `.env`**（`aureus/common/vault.py:180-195`、`aureus/organs/api_keys.py:83-86`）。
- **单实例锁在启动前获取**，时间线/账本在锁之后打开（`aureus/core/runtime.py:104-121`）。
- **litellm 参数不是幻觉**：`num_retries`、`api_base`、`openai/responses/` 前缀、`token_counter(model="")` 都实测存在（`llm_adapter.py:39-44`、`:324-342`）。
- **测试基线真实**：629 passed / 1 skipped。

---

## 7. 修复任务（按建议顺序）

> 每完成一项：补测试 → `uv run python scripts/check.py` 全绿 → 提交一次 git。
> 修之前先读第 8 节需要拍板的三个问题，避免做完又返工。

### F1. 预算并发（对应 S1）——最高优先

- 目标：并发调用时，本月的实际用量不允许超过 `monthly_token_limit`（允许最后一次调用带来的有限超出，并在文档写清楚）。
- 做法（二选一，拍板后定）：
  - 甲：`_admit` 里按 `route.max_tokens`（或配置的每次上限）**预留**额度，记进账本前不释放；`_finish` 按真实用量结算，退回差额。
  - 乙：给网关加一把 `asyncio.Lock`，让「读账本 → 判断 → 记账」串行；实现简单，代价是同一时刻只能有一个器官调用（Phase 0 心跳本来就是串行的，可以接受）。
- 验收：新增测试——上限 100、先记 90、并发两个各 100 的调用，断言「最多一个成功」且最终用量 ≤ 100 + 允许量；且原有 629 条测试全绿。
- 涉及：`aureus/safety/gateway.py`、`aureus/safety/ledger.py`（可能需要新增预留表或内存账）、`tests/safety/test_gateway.py`。

### F2. 用量未知的判定（对应 S2 / M3）

- 目标：任何「拿不到可信整数用量」的情况都按保守估算记账，不得记 0。
- 做法：`extract_usage` 在 `usage` 存在但三个字段都不是正整数时返回 `known=False`；`_as_count` 增加 bool 排除（浮点按四舍五入或直接判为未知，二选一并写注释）。
- 验收：新增测试覆盖 `usage=None`、字段为 `None`、字段为 `12.0`、字段为 `True`、字段为负数 五种情况，断言 `known=False` 且估算值 > 0；`aureus usage` 显示这笔为「估算」。
- 涉及：`aureus/organs/llm_usage.py`、`aureus/organs/llm_adapter.py`、`tests/organs/test_llm_usage.py`。

### F3. 不可变语义（对应 S3）

- 目标：时间线与账本在正常 SQL 路径下真的改不了。
- 做法：加 `BEFORE INSERT` 触发器，主键已存在时 `RAISE(ABORT)`；确认 `INSERT OR REPLACE` 会被拒。同步改 `docs/06-safety.md:142` 的措辞，说明「触发器防常规改写；能直接改数据库文件的人仍然能改，审计靠导出备份」。
- 验收：新增测试——`INSERT OR REPLACE` 同 id 必须抛错；原有 append-only 测试保持通过。
- 涉及：`aureus/timeline/database.py`、`aureus/safety/ledger.py`、`tests/test_timeline.py`、`tests/safety/test_ledger.py`、`docs/06-safety.md`。

### F4. 导入加固（对应 H1 / H2）

- 目标：一个恶意或损坏的 zip 不能吃光内存，也不能留下半套数据。
- 做法：
  1. 加常量上限：单文件大小、总解压大小、条目数；超出直接拒绝并说明原因。
  2. 报错文字改成「用于发现损坏；来源不可验证，请只导入自己的包」。
  3. 落地改为两阶段：全部写进目标同目录的临时名 → 全部成功后统一替换；失败按已替换列表回滚。
  4. 备份文件名加毫秒或递增序号。
- 验收：新增测试——声明大小超限的包被拒；模拟第 2 个文件失败时，目标目录保持原样；同一秒连续两次导入不会覆盖备份。
- 涉及：`aureus/core/portability.py`、`tests/core/test_export_import.py`、`docs/14-user-data.md` 第 4 节。

### F5. 让「账本不全」可见（对应 H3）

- 目标：用户能看到有多少次调用没有记账。
- 做法：`aureus usage` 增加一行「有 N 次调用被放弃/取消，这些费用未知」；数据来源可以是时间线里的 `organ_call_abandoned` 事件（按本月过滤）。
- 验收：新增测试——写一条 abandoned 事件后，usage 输出包含该提示。
- 涉及：`aureus/cli/inspect_commands.py`、`tests/cli/test_inspect_commands.py`。

### F6. 重试与记账（对应 H4）

- 目标：重试产生的每一次真实请求都能进账本，或者至少不漏记。
- 做法（拍板）：关掉 litellm 的 `num_retries`（设为 0），在 `LLMOrgan._call` 外面自己重试 `provider.max_retry` 次，每次生成独立 `CallAttempt`；或者在成功路径按 `max_retry` 补记估算并标记 `estimated=True`。
- 验收：新增测试——第一次失败、第二次成功时，账本里有两条记录（一条失败、一条成功）。
- 涉及：`aureus/organs/llm_adapter.py`、`tests/organs/test_llm_adapter.py`。

### F7. 密钥文件权限与竞态（对应 H5）

- 目标：Windows 上主密钥也只有本人可读；并发 `keys set` 不丢数据。
- 做法：Windows 上用 `icacls` 或 `pywin32` 设置仅当前用户可读（失败时警告，不静默）；`_save` 用单独的锁文件做互斥。**注意**：这会触碰 D36 的「不加口令」设计，改之前问用户。
- 验收：测试并发两次 set，两个名字都在；权限测试在 Windows 上断言 ACL（可用 `icacls` 输出做断言，存在跳过分支时写明原因）。
- 涉及：`aureus/common/vault.py`、`tests/common/test_vault.py`、`docs/15-model-management.md` 第 7 节。

### F8. 心跳阻塞（对应 M2）

- 目标：心跳每一拍不因磁盘等锁而卡住。
- 做法：把时间线/账本的写入移到 `asyncio.to_thread`，或给 SQLite 连接显式设置较短的 `timeout` 并把锁冲突当成一次可重试的失败（写回队列，下一拍再试）。
- 验收：模拟另一个连接长时间持锁时，心跳 tick 的实际耗时有上限（测试用假时钟/短超时断言）。
- 涉及：`aureus/heartbeat/main_loop.py`、`aureus/timeline/database.py`、`aureus/safety/ledger.py`。

### F9. 中低优先级批量（M1、M4–M8、第 5 节）

- M1：文档把日志脱敏的能力写准确（「挡已知密钥原文」），或在启动后对第三方日志器做一次性的 handler 屏蔽。
- M4：`TimelineDatabase.append` 里加 `ensure_aware(timestamp)`，与账本一致。
- M5：给对话行加转义/前缀（比如把用户内容里的换行换成 `\n` 字面量），防止伪造发言。
- M6：改锁文件提示（删锁文件不解除互斥，建议确认进程）。
- M7：`SnapshotsConfig` 加 `strict=True`。
- M8：`prune_snapshots` 的 `stat()` 包 `try/except OSError`。
- 第 5 节：按「没有装饰」原则删除或接入未使用的状态；修正注释范围；`burnout_risk` 改成中文或删掉；`llm_api_key` 决定去留；确认 `models.toml` 文件名。

> **完成情况（2026-11-18）**：F1–F8 已完成。F9 里 M1、M4–M8 已完成并有测试；第 5 节第 2 条（注释与实现不符）、第 5 条（`model_config.toml` 是用户本机的旧文件，代码读 `models.toml` 没有问题，不用改）、第 7 条（激素基线集中成常量）已完成。
> **第 5 节还剩第 1、3、4 条，需要用户拍板**：删不删未接真实来源的生物模型接口（`on_*`、`consume_energy_for_*`、`burnout_risk`、`emotion_label`、`mood_text`、`circadian_factor_for_melatonin/alertness`，目前只有测试在调用），以及 `Secrets.llm_api_key` 去留。另外发现：`circadian_factor_for_alertness` 的实际曲线是午夜最高、中午最低，和它原来的说明「早晨高」相反；因为没有代码用它，没改行为，只改了说明。

---

## 8. 需要用户拍板的三件事

1. **预算超支怎么算**：
   - 甲：允许单次超额（实现简单，语义是「到点后不再发起新调用」）；
   - 乙：严格预留（可靠，但要引入预留额度这个概念和对应的表/内存结构）。
2. **导入包要不要当成可信来源**：
   - 只导入自己的包 → H1 的「真实性」部分可以降级为文档说明；
   - 会接收别人给的包 → 需要签名，或至少在 CLI 里明确提示风险。
3. **主密钥要不要加口令**（会改动 D36）：
   - 不加：`secrets.key` 与 `secrets.vault` 放在一起，等于「锁和钥匙放同一个抽屉」；
   - 加：启动时要输入口令，命令行/常驻进程需要额外设计。

---

## 9. 方法与局限

- 逐行读了 `aureus/` 下全部模块、`scripts/check.py`，并核对了 `docs/` 里关于安全、导出导入、密钥的关键承诺。
- 做了 5 个可复现实验：并发预算、`INSERT OR REPLACE` 绕过触发器、用量字段解析、litellm 参数真实性、SQLite 锁等待（实测 5.5 秒）。
- **没有**跑真实模型调用、没有做模糊测试或渗透测试；并行开的 6 个审查子任务多数中途失败，结论以本次人工复核为准。
- 标「很可能」的地方是按代码与库行为推断、没有构造出真实场景的部分；其余为「确定」（含实测）。
- 本次审查**没有修改任何代码或测试**。
