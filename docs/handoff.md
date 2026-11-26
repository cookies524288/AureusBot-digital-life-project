# Aureus 交接 · 2026-11-26（D135–D143）

## 当前状态

D135–D143 与 3.54–3.60 的实现、审查和修复已完成并合入本地 `main`，按维护者要求直接普通推送 `origin/main`。静态检查 ruff、lint-imports、pyright 通过；全量 pytest 121 failed、3320 passed、2 skipped。121 项均为既有的 `tests/web` 旧页面脚本/浏览器、QQ 通道/迁移和 CLI 基线（其中 1 项 `test_resource_browser` 为浏览器波动，较上次多 1；改动区域没有新增失败）。隔离实例浏览器确认设置页新档位；本机 chinese-clip 实测 GIF/WebP 转 PNG 后返回 200。

## 本轮完成

- **D142 / 3.59**：消息防抖与打断对齐 MaiBot。`[heartbeat] message_debounce_mode`（off/fast/natural/steady，默认 `steady`）、`planner_message_steering`（默认 true）、`planner_steering_max_interrupts`（默认 4，0–20）；等待规则安静/活跃（30 秒阈值）、同人/换人、裸图 4 秒、纯表情不延长、整批 8 秒、@/提及 1 秒；`MessageDebouncer` 从 MaiBot `src/chat/debounce/controller.py` 移植（GPL-3.0，THIRD_PARTY）；旧 `inbox_debounce_*` 字段映射兼容；设置页中文档位「关闭/快速/自然/沉稳」。
- **D143 / 3.60**：图片嵌入前统一转 PNG。GIF/WebP 取第一帧，其它格式转 RGB PNG，PNG/JPEG 原样；损坏图不发送、报「图片格式不支持」；转换在工作线程；回环图片嵌入不走代理。根因是本机 chinese-clip 嵌入服务对 GIF/WebP 返回 500；修复后 GIF/WebP/PNG/JPEG 实测均 200。
- **D135–D141 / 3.54–3.58**：模型超时/限流默认值、QQ 默认所有群和私聊、@/名字/昵称提及唤醒、繁忙群防抖上限、记忆保持连续（D141）、跨会话提及/优先级/性能/生命周期修复；细节见各任务文档和 D135–D141。

## 下一步（当前任务）

**Aureus 重启后残留进程导致无法再启动（待设计/实现，拟 D144/3.61）。** 现状：`Aureus.exe`（C#）启动 `pythonw -m aureus.cli.windows_launcher --tray --owner-pid`，Python 侧单实例锁 `aureus.lock`、30 秒启动等待（`windows_launcher._STARTUP_WAIT_SECONDS`）和网页附着；3.49 已做过附着/重启窗口修复，但维护者仍经常遇到重启后旧进程残留、新实例显示「另一个 Aureus 实例正在运行」。日志显示旧内核关闭时 uvicorn 优雅关闭超时并取消任务；旧进程可能超过 30 秒才释放锁，或真正挂住。需要：健康检查后再附着、启动等待覆盖正常关闭、旧进程无响应时带确认地结束它（进程树），并让 C# 启动器在重启/退出时对子进程设看门狗；改 C# 后要重建根目录 `Aureus.exe`。不要直接杀用户正式实例；先在隔离数据根验证。

## 维护者实机验收观察点

- **模型**：模型测速不再 10 秒断开；MAGPIE 测速通过。
- **QQ**：所有群和私聊可收发；@/全名/昵称提及立即唤醒；繁忙群最多约 8 秒开始规划（steady 档）；私聊同一档位；重启后未读提及补收。
- **防抖/打断**：设置页「消息防抖档位」默认沉稳（steady），可选关闭/快速/自然/沉稳；「快速反应模式」开启；「打断重来上限」4；规划器流式，新消息可打断重来，用尽后完成回复。
- **图片嵌入**：GIF/WebP 表情和图片不再让 `image_embedding` 连续失败/被暂停；设置页模型测速与索引正常。
- **记忆**：保持连续，不按会话过滤（D141）；群成员可能问出私密记忆，是已接受的风险。
- **其它**：D135 的 120 秒超时/限流默认关闭；D136 所有群默认；D138/3.57 的旧防抖参数已被 D142 取代。

## 测试基线

- 全量（2026-11-26，`E:/Temp/aureus-final2-pytest.log`）：121 failed、3320 passed、2 skipped；失败为既有基线（109+ 旧页面脚本/浏览器、QQ 通道/迁移、CLI）。
- 本轮新增/更新：`tests/heartbeat/test_debounce.py`、`test_busy_group_debounce.py`、`test_inbox_attention.py`、`test_inbox_reply_review.py`、`tests/common/test_config.py`、`tests/organs/test_llm_adapter.py` 等；`tests/heartbeat` 927 passed、`tests/organs` 274 passed。
- `ruff check .`、`lint-imports`（8 kept）、`pyright`（0 errors）通过；`ruff format --check` 仍有 16 个既有文件，本轮新增行未新增格式问题。
- 浏览器：隔离实例 `E:/Temp/aureus-qa-d140`（fake，端口 18790）设置页确认档位下拉/快速反应/打断上限；无控制台错误。

## 环境与坑

- 项目 `.venv` 已重建，基础 Python 在 `E:\Tools\uv-python\cpython-3.13.18-windows-x86_64-none`。
- 正式实例当前由源码启动在 28976；`index.html` 在进程启动时读入内存，改前端后必须重启进程，刷新网页不够。本轮改动尚未在正式实例实机验收。
- `Aureus.exe` 由 `scripts/build-launcher.ps1` 从 `scripts/WindowsLauncher.cs` / `LauncherWindow.cs` 构建；改 C# 后必须重建，且正式实例运行时 exe 会被占用。
- 多个隔离测试实例可能遗留在临时数据根（`E:/Temp/aureus-ui-review*`、`E:/Temp/aureus-qa-d140`，端口 1877x/18790）。
- `.claude/launch.json` 有多个临时配置（未跟踪）。
- `E:\Temp\pytest-of-cooki\pytest-current` 是无权访问的联接；跑 pytest 用 `PYTEST_DEBUG_TEMPROOT` 指到独立目录。
- 推送 GitHub 需要本机代理：`git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push origin main`。
- 不真实调用模型；测试用假器官、手动时钟；不读 `aureus_home/` 的 identity/secrets；本机 MaiBot 只读公开/安装源码的防抖与打断部分，不读其数据库/日志/聊天。

## 可选后续

- 3.52 剩余三项（短回复跳过 LLM 分段、按会话并发、流式首包阶段计时/上下文瘦身）。
- 学习工作台实时刷新（3.43 遗留）；预取开关进设置页（3.27 遗留）。
- 旧页面脚本测试更新（维护者 2026-11-26 决定暂不更新）。
- 收件箱自动保留期限；短昵称/URL 误命中；SnowLuma 字符串消息 at 解析。
- D141 记忆连续带来的隐私风险由维护者接受。