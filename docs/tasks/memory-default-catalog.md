# 路线图 3.11 补充：默认使用统一文字记忆目录

状态：待评审
基线：`f9c8239`；延续[资源管理](resource-management.md)与[长期记忆行为核对](maibot-memory-parity.md)。

## 先读

- `aureus/web/resource_api.py` 的 `browse_memory`、`aureus/memory/resources.py` 的统一目录和网页 `resource-management.js` 的文字查询。
- `tests/web/test_unified_resources.py`、`tests/web/test_resource_api.py`、`tests/web/test_assemble.py` 与[开发守则](../11-dev-guide.md)。

## 目标和验收

- `GET /api/resources/memory` 省略 `kind` 时与 `kind=all` 完全一致。网页当前显式传 `all`，无需改网页。只接受既有 `all`、`history`、`live`；显式的旧分支响应结构、筛选和排序不改。使用旧 `source_table` 的调用方须同时指定 `kind=history`，复核 `test_assemble.py` 和既有历史筛选专项。
- 复用 `require_ready` 和统一目录查询；不改私聊与群聊召回规则、图谱授权、原件或数据库结构。没有有效会话不返回资料；未入索引的本地事件与统一目录不支持的历史来源不因新默认值进入列表。统一目录的类别、筛选、计数和分页保持原样。
- 扩展 `test_message_catalog_uses_same_category_without_crossing_edit_ids`：使用同一查询词与 `messages` 标签匹配迁入、本地两条记录；页大小一，核对总数、两页顺序、等同显式 `all`，以及同编号编辑仍按类型定位。验证显式 `history`、`live` 仍可独立查询。

## 子步骤与自检

1. 先加合成回归并确认旧默认行为失败，再修改 API 的单处默认值及测试中的旧调用方。
2. 运行修改和相邻测试；核对改动文件的规范、类型和差异，补齐交接并提交待评审。

- [x] `test_message_catalog_uses_same_category_without_crossing_edit_ids` 在旧代码上因省略 `kind` 与 `all` 结果不符而失败：默认页误列图片原件。修改后，两页结果与显式 `all` 完全一致；显式 `history`、`live` 保持可查，统一目录不返回未入索引事件和图片原件，同编号编辑仍相互隔离。
- [x] 统一目录、资源接口、网页组装、历史记忆与记忆索引共 30 项通过；4 个改动 Python 文件的 Ruff 规范、格式和 Pyright 检查通过；8 条架构规则通过，`git diff --check` 通过。未跑全量，留待集成收尾。

## 不做

不改统一目录的访问边界、增设类型、迁移数据、操作正式实例、合并或推送。只用合成资料。

## 待设计问题

无。
