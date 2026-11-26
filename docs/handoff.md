# Aureus 交接 · 2026-11-26（D135–D141）

## 当前状态

D135–D141 与 3.54–3.58 的实现、审查和修复已完成并合入本地 `main`。本轮按维护者要求直接普通推送 `origin/main`（本会话最后一步）。静态检查 ruff、lint-imports、pyright 通过；全量 pytest 120 failed、3279 passed、2 skipped，120 项全部是既有基线（109 个旧页面脚本、10 个 QQ 通道/迁移、1 个 CLI）。隔离实例浏览器验证设置页文案通过。

## 本轮完成

- **D135 / 3.54**：模型请求默认超时 120 秒、判断默认 120 秒、失控拦截每分钟限流默认关闭（模型测速独立上限同样默认不限）；网页模型测速/供应商测速按服务商超时推算，普通接口仍 10 秒。3.54 仍待维护者复审（实现已推送，维护者此前表示自行做完整复审）。
- **D136 / 3.55**：QQ 默认允许所有群和私聊；`allowed_group_id` 保留为可选的收紧项；示例、13 号文档、设置页同步。
- **D137 / 3.56**：at、全名、昵称（`alias_names`）提及在 QQ 保存前写入 `mentioned`；提及立即唤醒并进入优先未读，不抢占用户/特别关心；规划器仍按 D95 决定回不回。
- **D138 / 3.57**：人多的群防抖最多等待 20 秒（`inbox_debounce_max_wait_seconds`），从这一批最早一条消息算起；旧配置窗口大于 20 秒时自动取窗口本身；中断不再重置重开计数；20 秒上限只对群聊生效。
- **D139 → D141 / 3.58**：审查提出的记忆可见范围过滤被维护者否决，D141 记录“记忆保持连续，不按会话过滤”，群成员召回私密记忆的风险由维护者接受。D139 已标注撤销。
- **D140 / 3.58**：空闲驻留时别的会话的提及会结束驻留并规划；有待发分段时先投递再切换；`Priority.MENTION` 让用户/特别关心先于仅提及；`_planner_restarts` 只在正常完成清零；`on_inbox_message` 异常不杀 QQ 读取任务；`_allows_route` 默认范围核对账号；`special_contact` 只在非用户普通私聊；时间线批量去重 + 表达式索引、收件箱 mentioned 部分索引、优先/提及合并查询。

## 审查记录

- `/health`：8.8/10（partial coverage；typecheck 10、lint 10、tests 7；deadcode/shell/gbrain 未配置）。
- `/plan-eng-review`：报告在 `$GSTACK_STATE_ROOT/projects/<slug>/main-eng-review-20261126-171919.md`；关键修复已合入。
- `/review`：7 个专项 + 1 个对抗审查。Security 无发现；Design 3 条文案已修；Testing 6 条缺口已补；Simplification/Performance 建议合并查询与索引已做；State Machines/对抗发现跨会话提及丢失、优先级、生命周期、通道异常、账号核对等问题，已在 D140 修复。Codex 外部审查因本机 PreToolUse 只读守卫拦截命令超时，记 missing coverage，不作为通过。
- 已接受/未做：D141 记忆连续（隐私风险接受）；所有群 + 提及 + 默认无限流带来的无上限花费风险（需要时设 `[safety] max_calls_per_minute` 或 `monthly_token_limit`）；短昵称/URL 误命中与 SnowLuma 字符串 at 解析待观察；收件箱自动保留期限未做；16 个既有 `ruff format` 文件未清理；旧页面脚本测试未更新。

## 测试基线

- 全量（2026-11-26，`E:/Temp/aureus-final-pytest.log`）：120 failed、3279 passed、2 skipped；失败全为既有基线。
- 相关专项：370 + 1230 项通过；QQ 通道 467 passed、10 failed（既有基线）。
- `ruff check .`、`lint-imports`（8 kept、0 broken）、`pyright`（0 errors）通过；`ruff format --check` 仍有 16 个既有文件，本轮新增行已格式化。
- 浏览器：隔离实例 `E:/Temp/aureus-qa-d140`（`--fake`，端口 18790）登录/改密码/设置页验证通过；`.claude/launch.json` 有 `aureus-qa-d140` 配置。

## 新会话从这里开始

1. **实机验收**：停掉当前旧进程，用根目录 `Aureus.exe` 重启（单实例锁；不要与源码实例并存）。
2. 按下面观察点逐项看；有问题记录任务文档「验收意见」。

## 维护者实机验收观察点

- **模型**：模型测速不再 10 秒断开；MAGPIE 测速通过；设置页模型页字段完整。
- **QQ 范围**：所有群和私聊都能收到并回复；如需收紧，在 `[qq] allowed_group_id` 填群号。
- **提及**：群成员 @ 或写全名/昵称会立即唤醒并进入优先队列；不打断正在进行的用户/特别关心会话；重启后未读提及能补收。
- **人多的群**：连续来信时最多约 20 秒开始规划并回复；私聊不受这个 20 秒上限影响。
- **记忆**：记忆保持连续（D141），不按会话过滤；群成员可能问出私密记忆，这是已接受的风险。
- **设置页**：QQ 群说明是“允许的 QQ 群（可选）· 不写表示允许所有群和私聊”；心跳有“防抖最多等待（秒）· 从这一批最早的一条消息算起”。
- **其它**：D135 的 120 秒超时/限流默认关闭；D138 的旧配置兼容。

## 环境与坑

- 项目 `.venv` 已重建，基础 Python 在 `E:\Tools\uv-python\cpython-3.13.18-windows-x86_64-none`。
- 正式实例当前由源码启动在 28976；`index.html` 在进程启动时读入内存，改前端后必须重启进程，刷新网页不够。
- 多个隔离测试实例可能遗留在临时数据根（`E:/Temp/aureus-ui-review*`、`E:/Temp/aureus-qa-d140`，端口 1877x/18790）；与正式实例无关，可随时停止。
- `.claude/launch.json` 有多个临时配置（未跟踪）。
- `E:\Temp\pytest-of-cooki\pytest-current` 是无权访问的联接；跑 pytest 用 `PYTEST_DEBUG_TEMPROOT` 指到独立目录。
- 推送 GitHub 需要本机代理：`git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push origin main`。
- 不真实调用模型；测试用假器官、手动时钟；不读 `aureus_home/` 的 identity/secrets。

## 可选后续

- [3.52](tasks/3.52-model-test-and-reply-latency.md) 剩余三项（短回复跳过 LLM 分段、按会话并发、流式首包阶段计时/上下文瘦身）。
- 学习工作台实时刷新（3.43 遗留）；预取开关进设置页（3.27 遗留）。
- 旧页面脚本测试更新（维护者 2026-11-26 决定暂不更新）。
- 收件箱自动保留期限；短昵称/URL 误命中；SnowLuma 字符串消息 at 解析。
