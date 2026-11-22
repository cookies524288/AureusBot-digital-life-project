# Phase 3 · 米色 Logo

状态：待评审

以网页主页内容区浅色主题背景 `--bg: #fdf6e3` 为准。原白底替换成米色。凡带外环的版本，用无描边米色圆盘替代外环，圆外透明，保留原图主体及比例。已有透明无环版可继续用于深色主题。

## 实现与自检

- [x] 核对 4 份 SVG 的主体路径和原比例，验证 48、96 像素 PNG 和 9 帧托盘 ICO 仅把纯白替换成 `#fdf6e3`。圆盘 SVG 无描边，启动器 ICO 9 帧、favicon ICO 4 帧和 [192 像素预览](../../assets/logo/previews/logo-pixel-192.png)均核对了米色底、透明圆外和无黑环。预览以最近邻放大四倍，深色用的无环透明源图保持原样。
- [x] 在独立工作树用 `./scripts/build-launcher.ps1` 重建 `Aureus.exe`。核对 exe 的 9 个 Windows 图标资源及内嵌托盘 ICO 与对应资源逐字节一致。用临时数据和 `Aureus.exe --fake` 启动，Chrome 实际走通登录、修改密码和深浅导航，验证网页 favicon 字节，并从网页正常退出隔离实例。生产进程持续运行。
- [x] 中英文 GitHub 首页沿用无 Logo 布局，两份 README 与任务基线一致。网页 Logo、favicon 和启动器相关专项通过。执行 `uv run --no-sync python -m pytest -q tests/web/test_server.py tests/web/test_models_api.py tests/cli/test_chat_command.py`，80 项通过。

Chrome 截图覆盖登录、修改密码和深浅导航，并与 favicon、启动器和托盘各取一帧合成预览。浅色为米色圆盘，深色为透明底白色线条，托盘保留无环像素方底。预览合图保存在独立工作树的 `.venv/logo-browser/logo-preview.png`。

实现提交 `4aceb63`。独立环境用 `uv sync --locked --offline --all-extras` 准备依赖，设置独立的 pytest 临时目录后执行 `uv run --no-sync python scripts/check.py`。Ruff、Pyright、8 条架构规则通过，pytest 2367 项通过、1 项跳过。暂存差异通过 `git diff --cached --check`。

## 验收意见

待复审。
