# Aureus 交接 · 2026-11-26（3.61 待评审）

## 当前状态

3.61「启动器残留进程冲突」已按 D144 实现并自测，本地 `main` 上有 4 个提交，状态待评审；未推送。正式实例（端口 28976，Aureus.exe + pythonw，owner-pid 39900）全程未停止、未覆盖，根目录 `Aureus.exe` 仍是旧构建。

提交：

- `3594010` 启动器：附着前检查网页健康，启动等待改为 180 秒（D144）
- `0819141` 单实例锁：记录持有者 PID 标记，导出包排除锁与标记（D144）
- `b7f60cc` 启动器：等待超时后核对并确认结束残留实例（D144）
- `cc3f717` 启动器：托盘重启和退出时看门狗结束挂住的子进程（D144）

## 本轮完成

- **健康检查**：`_open_running_web` 附着前用 `httpx.get(url, trust_env=False, timeout=1)` 检查根地址；非 200、超时、连接失败都不附着，继续走单实例锁/启动。
- **启动等待**：30 → 180 秒。等待期间每 0.1 秒重试附着/抢锁，不另起内核；超时后读 `<数据根>/aureus.lock.pid`，用 PowerShell 核对进程存在、命令行含 `-m aureus.cli.windows_launcher`、创建时间早于 pid 文件修改时间；通过后 MessageBox 是/否确认，点「是」才 `taskkill /T /F` 结束该进程树。拒绝或核对不符返回原来的单实例锁错误，不杀进程。接管成功后重新给一轮等待，避免连续弹框。
- **锁 PID 标记**：`_LockContextManager.__enter__` 持锁后写当前 PID，`__exit__` 先删标记再放锁；写失败只记日志，不影响锁。导出包排除 `aureus.lock` 与 `aureus.lock.pid`。
- **C# 看门狗**：托盘点「重启」后 180 秒、点「退出」后 30 秒子进程还没退出，就 `taskkill /T /F` 结束子进程树；主动结束不算启动失败，重启继续 `--restarted`、退出返回 0；普通崩溃仍走原失败页。
- **手测**：隔离数据根 `E:/Temp/aureus-361-home`（端口 18773、假器官）实测双击附着、网页重启、退出；用假子进程实测 C# 看门狗，重启 180.6 秒、退出 32.3 秒。详见 `docs/tasks/3.61-launcher-stale-process.md` 的「手测记录」。

## 下一步（当前任务）

1. Opus 复审 3.61；通过后更新 `docs/08-roadmap.md` 状态。
2. 维护者停掉正式实例后运行 `scripts/build-launcher.ps1` 重建根目录 `Aureus.exe`（正式实例运行中 exe 被占用，不能覆盖）。
3. 用新 exe 重启做验收；观察点见下。

## 维护者实机验收观察点

- **双击**：已有实例健康时只附着打开网页，不出现失败页；网页已无响应时不附着，走锁/启动。
- **正常重启**：旧实例关闭可能超过 100 秒；新启动器最多等 180 秒，不另起内核，锁释放后正常启动/附着。
- **旧实例挂住**：180 秒后弹「Aureus 启动」是/否框并显示进程号；点「是」才结束该进程树并继续启动；点「否」或核对不符时显示原来的「另一个 Aureus 实例正在运行」，不结束任何进程。
- **托盘**：点「重启」后子进程 180 秒内没退出会被看门狗结束并继续启动；点「退出」后 30 秒内没退出会被结束，启动器以 0 退出，不显示启动失败。
- 不改数据根、预算、权限、急停和单实例锁语义；未确认时不结束进程。

## 测试与静态检查

- 相关 pytest：`tests/cli/test_windows_launcher.py`、`tests/cli/test_debug_restart.py`、`tests/test_identity.py`、`tests/core/test_export_import.py`、`tests/test_portability.py` 共 110 passed。
- `ruff check .` 通过；`lint-imports` 8 kept、0 broken；`pyright` 0 errors、0 warnings。
- 未跑全量 pytest；既有全量基线仍是 121 failed（旧页面脚本/浏览器、QQ、CLI），与本轮无关。

## 环境与坑

- 正式实例当前由源码启动在 28976；本轮未重启，根 `Aureus.exe` 未重建。
- pytest 临时目录用 `--basetemp=E:/Temp/pytest-aureus-361`；`E:/Temp/pytest-of-cooki/pytest-current` 是无权访问的联接。
- 手测目录：`E:/Temp/aureus-361-home`、`E:/Temp/aureus-361-harness`、`E:/Temp/aureus-361-stub`；临时 exe 已从项目根删除，仓库里只有源码。
- `windows_launcher.py`、`service.py`、`portability.py`、`WindowsLauncher.cs`、`tests/cli/test_windows_launcher.py`、`tests/test_identity.py` 保持 CRLF；新测试 LF。
- 推送 GitHub 需要本机代理：`git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push origin main`。

## 可选后续

- 原有可选项不变：3.52 剩余三项（短回复跳过 LLM 分段、按会话并发、流式首包阶段计时/上下文瘦身）、学习工作台实时刷新、预取开关进设置页、旧页面脚本测试更新、收件箱自动保留期限、短昵称/URL 误命中、SnowLuma 字符串消息 at 解析。
