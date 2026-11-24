# 路线图 3.9 后续：默认数据目录切换预演

状态：待评审
设计：D28、D31、D38 与交接文档「最终切换」；不重开 3.9 的历史迁移验收。

## 先读

- [交接：最终切换](../handoff.md#下一步)、[用户数据](../14-user-data.md)、[开发守则](../11-dev-guide.md)。
- `aureus/core/portability.py`、`aureus/identity/service.py`、`aureus/cli/windows_launcher.py` 与 [Windows 启动器](windows-launcher.md)。

## 目标和验收

只用 Windows 合成目录预演正式目录停机后迁往项目默认 `aureus_home`。确认最近的 SQLite 提交、嵌套媒体、配置、隐藏文件和空文件夹完整；验证失败不切换、不清理两端、不尝试合并。保留原目录并写清失败回退。

## 改动一览

- `scripts/check_home_cutover.py`：现有 Windows `robocopy.exe` 的离线校验入口，不另写复制实现；复用 Aureus 单实例锁同一个文件和 FileLock 协议，避免删除源锁文件。
- `tests/test_home_cutover.py`：Windows 合成回归；使用真正的 Robocopy 和 `_launcher_home()` 路径选择，不启动内核或连接网络。
- `docs/default-home-cutover.md`：可重复预演、停机与复制前提、正式切换的人工步骤和回退说明。

## 接口和行为

- `python -m scripts.check_home_cutover SOURCE TARGET --confirm-stopped`：两端显式使用已有的绝对目录；目标必须是空的 `aureus_home`。只有维护者确认正常停机后才能传确认参数。入口不推断正式目录，不查找或操作运行进程，不更动 `AUREUS_HOME` 或 `aureus-home.txt`。
- 两端路径相同、互相包含、任一参数的上级或任一树内有链接、重解析点或非普通条目、源没有核心库、目标不为空时拒绝。目标非空不清理。源单实例锁无法取得时直接拒绝；不是用锁文件在不在判断运行状态。维护者仍要排除其他 SQLite 写入器和外部自动重启。
- 锁持有期间调用 Robocopy，保留文件与目录数据、属性和时间；唯一排除项为源根目录的运行锁。递归校验目录项与文件 SHA-256，只输出总数和结果，不解析私有数据、不打印路径清单、摘要或内容。复制或核对结果不确定时留下原目录和目标供人工处置，不启动或切换。
- `aureus export/import` 的公开包故意排除密钥等材料，不适用于这次完整目录迁移，也不使用 `--force`。

## 测试与子步骤

1. 交付校验入口；专项先验证真实复制、最新 SQLite 写入、目标不为空、同目录/互包含和重解析点拒绝。
2. 核对启动器优先级、切换与回退演练；补齐操作和复核记录。全量检查留到整批收尾，不重复基线全量。

## 自检清单

- [x] `test_copy_checks_latest_stopped_home_and_launcher_fallback`：持有源锁且有最新 WAL 提交时拒绝复制；正常关库释放锁后，目标通过 SQLite 完整性检查并能读取最新记录。
- [x] 同一用例验证隐藏属性、嵌套文件、空目录和原目录；源锁标记保留，目标不复制源根锁。
- [x] `test_refuses_nonempty_target`、`test_refuses_overlapping_roots`、`test_refuses_junctions`、`test_rejects_same_size_file_corruption` 通过；无停机确认亦拒绝。
- [x] 同一集成用例及 `tests/cli/test_windows_launcher.py` 验证环境、临时指针、默认路径优先级；校验失败不切，回退可恢复原路径。
- [x] [操作手册](../default-home-cutover.md) 记录正常停机、串行校验、同一实例验收和不丢数据的回退；只运行合成夹具，未操作正式数据、路径指针、网络或 Aureus 进程。

### 校验记录

- 提交 `514f5b4` 定义预演与验收，`a977da7` 实现并验证离线复制和校验。
- `pytest tests/test_home_cutover.py tests/cli/test_windows_launcher.py tests/test_portability.py tests/core/test_export_import.py`：56 项通过；只运行相关专项，未重跑全量。改动文件 Ruff、格式和 Pyright 通过；8 条架构规则通过。
- 夹具根目录：`E:\Temp\aureus-home-cutover-sol-33d4a98f\test_copy_checks_latest_stoppe0`。源为其 `active home 合成`；默认目标为 `fixture project 合成\aureus_home`。命令行另用该根下全新的 `cli-smoke\aureus_home`，未确认停机退出 1、确认后退出 0 并核对 8 个文件。

## 不做

不访问正式数据与指针，不启动、停止或重启任何 Aureus 实例，不调用模型或 QQ，不修改正式分支或推送。不转移源码目录外依赖，不做数据格式转换、自动选目录、删除原数据或双向同步。

## 待设计问题

无。

## 验收意见

待 Astra 复审。
