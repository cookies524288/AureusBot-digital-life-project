# 路线图 3.4 / 3.11 后续：表达聚类快照持久化

状态：完成

设计：沿用 [D100](../10-decisions.md) 和[表达向量聚类计算](expression-vector-clusters.md)的发送及召回边界。

## 先读

- [开发守则](../11-dev-guide.md)、[MaiBot 学习核对表](maibot-learning-parity.md)、[表达向量聚类计算](expression-vector-clusters.md)。
- `aureus/memory/expression_clusters.py`、`aureus/memory/language.py`、`aureus/common/sqlite.py`、`tests/memory/test_expression_clusters.py`。
- 参考 MaiBot 1.3.15 固定提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的 `src/chat/replyer/expression_vector_index.py`，只借鉴派生快照和分空间保存，不移植其向量、构建器或文件格式。

## 目标和验收

明确请求保存后，重开数据库仍能按空间读取同一批成员；同空间替换完整生效或完整回滚，不影响其他空间。快照只附带定位和校验成员所需的数据，不能替代实时召回或逐项发送许可。

## 改动一览

- `aureus/memory/expression_clusters.py`：增加快照存取，不接入现有构建和召回流程。
- `tests/memory/test_expression_cluster_snapshots.py`：使用合成表达、假嵌入器和临时 SQLite 库验证行为。
- 学习核对表及交接：注明只覆盖显式存取，与正式功能区分。

## 接口

- `ExpressionClusterStore(db_path: Path)`：只打开显式传入的 SQLite 文件并建快照表，不读取默认数据目录。
- `save(snapshot: ExpressionClusters) -> None`：保存单个空间的当前快照；写入失败原样抛出 SQLite 错误，保留旧快照。
- `load(embedding_space: str) -> ExpressionClusters | None`：根据完整空间名读取；没有记录返回 `None`，遇到未知格式版本报 `ValueError`。
- 仅显式调用，暂不新增配置或状态开关。加载结果继续通过 `recall_members` 按当前状态、会话绑定和内容指纹过滤。

## 行为细则和边界情况

- 快照表每个空间最多一条，保存格式版本、快照编号、空间和簇数。成员表保存快照编号、簇号、成员顺序、表达键和内容指纹。没有原文、例句、原始消息或向量。
- 单个写事务先移除该空间的旧快照和成员，再写入新快照和成员。不同空间的相同簇号互不影响；不保留历史快照。读取一次查询完成，保证头和成员来自同一个数据库视图。
- 保存不会调用嵌入器，也不会写入语言条目或改变其会话授权。快照不提供任何对外发送授权。

## 自检清单

- [x] 新存储对象打开同一库能读取原快照，缺失空间返回 `None`，未知版本拒绝加载：`test_snapshot_reopens_with_exact_cluster_and_member_order`、`test_snapshot_rejects_unknown_format_version` 通过。
- [x] 两空间可同时保存含 0 号簇的不同快照；替换其中一份后另一份不变，且旧快照成员不残留：`test_snapshot_replacement_is_isolated_by_embedding_space` 通过，直接查询确认最终只有两份快照、两条成员。
- [x] SQLite 触发器在替换已开始后拒绝一条新成员，重开可读取完整旧快照：`test_snapshot_write_failure_rolls_back_removal_and_partial_insert` 通过；触发器先确认旧成员已删除、首个新成员已插入，再拒绝第二条，回滚后旧快照编号与成员均恢复。
- [x] 直接查询表结构与行，只含约定字段；加载后实时改写的表达仍被指纹过滤：`test_snapshot_contains_only_derived_references_and_rechecks_live_content` 通过；`test_loaded_snapshot_does_not_override_package_scope` 另验证会话绑定及撤销仍实时生效。
- [x] 持久化、聚类及语言专项共 33 项通过；两份改动 Python 文件 Ruff 格式/规范、Pyright（0 错误）通过，架构 8 条规则通过，`git diff --check` 通过。本次未跑全量，不能宣称现有全量基线通过。

## 交付记录（2026-11-24）

- 独立工作树 `E:/WorkShopE/Projects/Aureus-expression-snapshots-dev`，分支 `codex/expression-snapshots`，基线 `c90b987`。
- 任务书 `3d323db`；实现与六项持久化测试 `348ec1d`。
- 专项命令使用主树 `.venv/Scripts/python.exe` 执行 `-m pytest -q -p no:cacheprovider --basetemp E:/Temp/aureus-expression-snapshots-f76f904794c140668006246a826539ff/pytest tests/memory/test_expression_cluster_snapshots.py tests/memory/test_expression_clusters.py tests/memory/test_language.py tests/test_language_runtime.py`：33 项通过，19.83 秒。
- `PYTHONPATH` 指向独立工作树；`AUREUS_HOME` 指向该合成夹具根下的 `never-production-home`，不接正式实例。夹具保留待复审，未自动清理任何其他目录。前一次静态检查因 SQL 行长报错时尚未运行测试；已拆行修正后通过。
- 未改 `main_loop.py`、生产装配或配置；无新增依赖。没有读取正式数据、调用真实模型或 QQ，没有合并、重启、推送。

## 子步骤

1. 提交任务书；实现显式存取和事务测试。
2. 跑合成专项与静态检查；更新自检、学习核对表及交接，提交待复审。

## 不做

不接网页、回复、在线构建或正式数据库；不保存向量，不补建历史数据，不新增空间删除、导入导出或维护入口。不连接真实模型、QQ 或读取用户数据；本分支不合主线、不重启、不推送。

## 待设计问题

暂无。

## 验收意见

2026-11-24 Astra 复审通过：保存时在同一事务中替换指定空间，加载只取该空间的当前快照。独立复跑 `tests/memory/test_expression_cluster_snapshots.py`，6 项通过，覆盖重开、隔离及失败回滚。本步不包含浏览页面、生产建簇或部署。
