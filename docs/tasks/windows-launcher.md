# Windows 启动器

状态：待评审（2026-11-22，自检通过）

## 目标和验收

- 双击项目根目录的 `Aureus.exe` 后，使用该目录已准备好的 uv 虚拟环境启动 Aureus，网页开始监听后打开默认浏览器，启动时无终端窗口。
- 本机浏览器登录并改掉默认密码后，侧栏可打开同一实例的交互终端。关闭终端窗口后网页继续运行，可再次打开终端；终端里的 `/quit` 正常关闭 Aureus。
- 原有 `aureus chat` 保留主动打开终端的行为。网页按钮仅在 Windows 启动器模式下对本机开放。

## 范围

Windows 图形启动入口、现有终端通道的本机窗口和网页按钮。启动器使用现有数据根目录解析和单实例锁。隔离验收只用临时数据和假器官。

## 使用

在项目目录运行 `uv sync` 准备环境，并按 README 准备数据目录。双击根目录的 `Aureus.exe`。数据根目录沿用 `AUREUS_HOME` 或当前检出下的 `aureus_home/`。如要重新构建，在 PowerShell 中运行 `./scripts/build-launcher.ps1`。

离线试运行可以在 PowerShell 中执行 `./Aureus.exe --fake`。

## 自检清单

- [x] Windows 图形程序产物可启动，网页监听成功后才打开浏览器。隔离验收从其他工作目录启动真实 `Aureus.exe --fake`，本机网页上线，启动后没有终端子进程。
- [x] 登录、默认密码、本机、同源检查通过，普通启动隐藏入口。接口回归 `test_terminal_open_requires_local_same_origin_and_ready_login` 通过，旧启动和页面专项通过。
- [x] 能打开终端、重复点击、关闭重开，从终端正常退出。隔离 Chrome 登录后实际点击侧栏按钮，桌面和窄屏无横向溢出；Windows 终端测试验证重复打开、关闭后网页在线、重开及 `/quit` 正常退出。
- [x] 完整 `uv run --no-sync python scripts/check.py` 通过。850 个文件格式合格，Ruff、Pyright 和 8 条架构规则通过，pytest 2303 通过、1 跳过。专项 72 通过。

## 待设计问题

无。
