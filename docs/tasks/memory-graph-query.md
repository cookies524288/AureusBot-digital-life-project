# 路线图 3.11 后续：关系图只读搜索与浏览

状态：待评审
设计：D99、D100；只消费[关系图结构化来源](memory-graph-source.md)已经明确获准的结果。

## 先读

- [长期记忆核对表](maibot-memory-parity.md)、[D99/D100](../10-decisions.md)、[开发守则](../11-dev-guide.md)。
- `aureus/memory/graph.py`、`aureus/memory/index.py`、`tests/cli/test_maibot_memory_graph.py`。

## 目标和验收

在记忆子系统提供独立只读入口：实体名、谓词搜索，浏览一个实体的单跳关系，查实体或关系详情，给出查询内可见总数和分页位置。不提供正式网页入口或新的授权机制。

## 改动一览

- `aureus/memory/graph_query.py`：对已授权投影做搜索、排序、分页和详情。
- `aureus/memory/index.py`、`aureus/memory/__init__.py`：接入并导出必要的查询结果类型。
- `tests/memory/test_graph_query.py`：合成授权、撤权、状态、分页和只读回归。

## 接口

- `MemoryIndex.query_graph(*, allowed_sources, query='', neighbor_of=None, offset=0, limit=50)` 返回 `MemoryGraphPage`（`covered`、`items`、`total`、`next_offset`）。条目是已有实体节点，或 `MemoryGraphRelationDetail`（关系原件和有方向的两端实体）。`offset` 非负，`limit` 为 1 至 100，否则抛 `ValueError`。
- `MemoryIndex.graph_detail(source, *, allowed_sources)` 返回对应实体或关系详情；无权、已失效或不存在时统一返回 `None`。输入源键只作定位，不授予权限。

## 配置

无新增配置。调用方每次必须从可信路径显式传入独立授权的原件集合；没有授权就传空集合。

## 行为细则和边界情况

- 先由现有 `read_graph` 校验原件授权、来源状态、有效期、停用、移除及端点/证据，之后才检索排序计数。关系、端点、证据段落和关联行仍各自授权；独立获准的关系可以没有可见证据。
- 空关键词浏览全部可见实体和关系。非空关键词忽略首尾空白和大小写，按实体名或谓词做精确、前缀、子串匹配；同等级先实体后关系，再以规范文字和完整来源键稳定排序。不匹配原始哈希、证据或未授权字段。
- 单跳仅返回可见的入边和出边，每条关系只列一次，保留方向；不合并重名实体、不自动补反边或继续找第二跳。锚点不存在或未获准时返回与空授权同样的空页，不暴露覆盖信息。
- 无授权或旧库无结构化表时为无覆盖；有授权覆盖但未命中时总数为零。总数只统计匹配的可见条目，先计数再切页。数据库错误沿用现有只读入口行为。

## 测试与子步骤

1. 用合成夹具覆盖授权后的搜索、详情、单跳和分页；验证隐藏精确命中不改变总数、次序或页边界。
2. 验证逐类撤权、停用/移除/恢复/有效期和空库；跑图来源、查询及 D99/D100 相关专项。按本次授权范围不重跑全量。

## 自检清单

- [x] 同名实体不合并；隐藏精确命中不影响可见排序、总数、分页或详情。`test_search_ranks_only_visible_matches_before_counting_and_paging` 通过。
- [x] 关系、两端、段落和关联行撤权独立生效；单跳保留显式方向。`test_detail_and_neighbors_respect_independent_grants` 六种撤权与 `test_neighbors_keep_directions_and_use_full_source_keys` 通过。
- [x] D99 停用、移除和恢复及有效期先于结果；空授权、缺乏结构化覆盖和有效授权零命中可区分。`test_removal_and_restore_do_not_override_disabled_graph_items`、`test_validity_boundaries_filter_graph_before_search`、`test_readonly_query_distinguishes_legacy_and_authorized_zero_matches` 通过。
- [x] 只读，不接网页、不改变 D100 召回，不修改正式数据。图来源专项以及 `test_batch_removal_suppresses_real_recall_without_changing_original_states`、`test_private_memory_and_disclosure_reminder_reach_group_replyer` 通过。

### 实现与验证记录（2026-11-24）

- 实现提交 `4f9b004`；查询和详情共用 `MemoryIndex.read_graph()`，不新增数据库读写路径。只使用主树既有虚拟环境，以开发工作树为 `PYTHONPATH`，显式设置 `PYTHONUTF8=1`。
- `python -m pytest -q --basetemp <独立目录>` 联合运行 `tests/memory/test_graph_query.py`、`tests/cli/test_maibot_memory_graph.py` 和上列 D99/D100 两项：20 项通过，1 项因夹具从原件详情而非维护列表取编号失败。只修正测试夹具，随后在新目录独立复跑 `test_removal_and_restore_do_not_override_disabled_graph_items`，1 项通过。按本步范围未重跑已通过用例或全量；共 21 项专项分别通过。
- 联合夹具：`E:/Temp/aureus-graph-query-regression-e5b94c80d758447c949d736340aba906`；单项复跑夹具：`E:/Temp/aureus-graph-query-restore-dad51117e05344258e9c0c67e19f704f`。
- 四个改动 Python 文件通过 `ruff check`、`ruff format --check` 及 `python -m pyright --venvpath <主树>`；修正后的测试文件单独复查也通过。`import-linter` 8 条架构规则全部通过；`git diff --check` 通过。

## 坑

Windows 使用主树已有 `.venv`、开发树 `PYTHONPATH` 和独立 `--basetemp`；挂钩只读查询不能为了空库而初始化表。

## 不做

不做网页/API 暴露、授权授予或撤销机制、图写入、正式回填、图生命周期、模型调用或召回变更。本任务不读取私有数据、不合入正式 main、不启停实例、不推送。

## 待设计问题

本步无阻塞。正式图谱授权另议，不以本步替代。

## 验收意见

待 Astra 复审。
