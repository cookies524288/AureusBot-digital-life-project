# 路线图 3.11 补充：长期记忆移除与恢复记录

状态：待评审
设计：D99（可逆移除）、D77（自检和复审）。

## 先读

- [长期记忆对照](maibot-memory-parity.md)、[批量操作回归](memory-batch-browser-regression.md)、[开发守则](../11-dev-guide.md)。
- `aureus/memory/{resources,index,inspection}.py`、`aureus/web/resource_api.py`、`aureus/web/static/resource-management.js`。
- MaiBot 1.3.15 固定提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的 [客户端](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/dashboard/src/lib/memory-api.ts) 和内置 A_Memorix 2.0.0 的 [删除服务](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/delete_admin_service.py)：客户端按条数与模式读删除操作、按操作编号读详情；执行端在同事务内保存计划与实际操作，另走清理及恢复流程。这里只借鉴维护历史，不移植代码和删除语义。

## 目标和验收

在长期记忆的「操作记录」页查看实际成功的批量移除和恢复。复用资源分页控件，显示类型、发生时间和当前范围内实际变更条数、总页数，清楚区分没有记录、功能关闭和读取失败。

## 接口与行为

- `memory_edits` 是当前覆盖，不是审计；记忆流也没有这项事实。不从旧状态、导入或时间线补造历史。新操作在原 `BEGIN IMMEDIATE` 内追加一条真实事实，保存经索引与原件双向定位的本地时间线键、动作和记忆时钟时间；当前状态比对未全匹配时整批拒绝，重投不会重复记成功。记录写入报错时和移除标记一起回滚。
- `GET /api/resources/memory/operations` 由已登录、已改默认密码的维护者读取；采用 `offset`、`limit` 和现有 `items`、`total`、`next_offset`、`enabled` 约定。读取不续期、不写库，不因库读取失败返回假空列表。以当前网页通道范围及本地时间线原件的范围判定每个成员，先过滤再统计和分页；不信派生索引标签、迁入来源键或客户端声明。没有可确认可见成员的批次不显示；混合批次仅显示可见成员数。对外不返回目标键、正文、来源、全批数量或跨范围顺序号。
- 条目定位沿用 `kind` 和 `entry_id`，其中本地索引的行号与原时间线编号须一致；导入原件仍可按原路径维护，因没有等价的原件可见范围，操作记录不统计它们。网页不新增修改权限或其它状态流。
- `memory.removal_operations_enabled` 默认开启；关闭时停止新增记录和读取，仍可正常执行批量移除/恢复，不清除已有记录。操作日志是从部署启用时起的附加维护事实，不随文字包转移。

## 改动一览

- 记忆层：批量状态写入、操作记录读写和专用开关。
- 网页接口和静态页面：只读分页查询，复用现有资源页布局。
- 测试：维护接口与真实 SQLite 事务；实际 Chrome 390 px 测试分页、空态、失败与重试。更新长期记忆对照。

## 子步骤

1. 先提交设计与自检标准。
2. 实现、运行冻结基线上的定向检查，记录实测结果并提交；全量检查交由收尾集成安排。

## 自检清单

- [x] `test_memory_operations_show_only_visible_changes_before_paging`：过滤后计数和分页，同一时刻按写入顺序倒序；索引标签仿冒和请求参数不扩大权限，返回仅类型、时钟时间和可见成员数。
- [x] `test_memory_batch_remove_and_restore` 检查取消不发写请求；`test_memory_operations_show_only_visible_changes_before_paging` 检查重投、失效或跨网页来源请求不留成功记录、旧覆盖不回填；`test_memory_operation_failure_rolls_back_and_switch_keeps_maintenance` 注入 SQLite 写失败并核对整库无变动，成功路径保留原件、证据、时间线和停用。`test_batch_removal_suppresses_real_recall_without_changing_original_states` 覆盖实际召回和原状态。
- [x] HTTP 两项覆盖未登录、默认密码、过期会话、空、关闭和缺库；关闭记账不妨碍原批量恢复。
- [x] `test_memory_operations_show_states_and_paginate_at_390px` 覆盖类型、时间、数量、总页数、翻页、关闭、空态和失败重试；浏览器整文件与检修、文字包、资源接口、召回回归合计 70 项通过，另有新 HTTP 专项 2 项通过。
- [x] 更新对照；Ruff 检查、格式、Pyright（0 错误）、`node --check`、架构检查（8 项）与 `git diff --check` 通过。未重跑全仓检查，未操作正式进程、库、分支、真实模型或远程。

## 交付环境

基线 `7c41b98`，独立分支 `codex/memory-operations`。使用正式工作树已有 Python 环境、开发树 `PYTHONPATH`。合成夹具位于 `E:/Temp/aureus-memory-operations-7c41b98-dev/pytest`、`pytest-http` 和 `pytest-regression`；Chrome 临时资料也位于该临时根目录，浏览器测试结束后按夹具正常回收。

## 不做

不上游操作 ID 详情、删除模式、快照回滚、级联或派生索引清理任务、永久删除、图谱授权、迁入范围推断、日志导出或回填。

## 待设计问题

无。

## 验收意见

待复审。
