# 长期记忆：文字包导入前检视

状态：待评审
范围：路线图 Phase 3 的资源管理后续；沿用 D99 的可逆移除边界。

## 先读

[资源管理](resource-management.md)、[长期记忆对照](maibot-memory-parity.md)、[开发守则](../11-dev-guide.md)；本地 `aureus/memory/transfer.py`、`aureus/web/memory_transfer_api.py` 和 `aureus/web/static/memory-flow-transfer.js`。

固定 MaiBot 1.3.15 内置 A_Memorix 2.0.0 [管理分派](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/bundle_admin_service.py#L244)、[装载校验](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/bundle_admin_service.py#L988)、[导入](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/bundle_admin_service.py#L1817)：检视走包装载与校验并返回摘要、图片向量兼容信息；导入再装载，另查目标安装范围、安装状态与合并/恢复条件，才写入。只借鉴分开检视与确认的流程，不移植代码。

## 目标和验收

- 实际长期记忆 → 导入导出页选文件即检视，完整校验后可取消或确认追加。坏包、文件变更、取消和迟到结果不得沿用旧确认。
- `POST /api/resources/memory/transfer/preview` 使用与旧导入相同的登录就绪条件、同源、媒体类型和 2 GiB 上传限制，共用落盘临时文件和流式解析。检视使用现有只读会话检查，不续期；扫描到包尾才返回，不连接本地记忆库、不修改记忆、不留存包或整包解析结果。
- 响应只有版本、包内条目总数及按来源表可可靠区分的固定分类计数，未知表合计为其他。无正文、来源键、证据细节或文件名。分类总和等于包内条数，不预估新增。确认把已检视的同一个浏览器 `File` 原样交给原 `POST /api/resources/memory/transfer`，旧接口无需先检视，仍按原样重新校验、原子追加、跳过相同或整包回滚。

## 改动一览与接口

记忆交换层在既有解析器之上增加文件摘要；Web 层增加检视入口及共用接收逻辑；导入导出页增加当前选件的检视状态、明确确认、取消及结果。无新增配置。固定的少量计数在 390 px 窄屏可读，不按文件条目增长。

## 测试与子步骤

1. 先补合成接口和 390 px Chrome 回归并观察旧版失败；实现数据检视和网页状态机，跑改动及相邻专项。
2. 检查改动、记录证据及与上游的差异，提交和交接给 Astra。该独立工作树不运行正式实例或操作主线。

## 自检清单

- [x] 混合包及仅读文件：`test_memory_package_preview_counts_all_rows_before_confirming_real_import`；空包、坏尾、版本、同源、类型和长度：`test_memory_package_preview_rejects_bad_tails_and_obeys_upload_guards`；生产组装的登录条件和不续期：`test_memory_package_preview_requires_ready_session_without_touching_it`。
- [x] `test_memory_package_preview_requires_current_file_confirmation_at_390px` 实点验证加载、坏尾提示、取消、换文件、取消选件、迟到响应、确认、无横向溢出和结果，确认前无写请求，上传的正是当前通过检视的选件。
- [x] `test_memory_package_browser_confirm_uses_real_transfer` 从 390 px 实际页面进入，通过真实 Web 路由确认并检查合成库，检视前后未写记录，确认后原件入库。`test_memory_package_preview_counts_all_rows_before_confirming_real_import` 和 `test_memory_file_roundtrip_and_conflicts_are_atomic` 覆盖重复跳过、冲突回滚和旧接口兼容。
- [x] 新增的 3 项接口和 1 项浏览器回归在旧实现上因缺入口/选文件不检视先红；改动后记忆交换、资源浏览、导航、只读检修和 Web 组装 **54 项通过**。5 个改动 Python 文件通过 Ruff 规范/格式、Pyright；8 条架构规则、`node --check` 和 `git diff --check` 通过。留待主审收尾跑全量；未接触正式数据、实例或主线。

## 对照结论

检视通过只表示当前 JSONL 可解析且完整，不能判断目标库冲突或新增数，不是上游安装预检。上游的 `knowledge`/`full` 用带清单和校验值的容器，另有图片向量兼容提示、安装范围及归属、卸载和仅限空库的 `full` 恢复；本步均不实现。上游 `full` 可保存选定范围的关系及实体状态、情景、事实转换及证据、画像、图像和向量等成员；Aureus 文字包不承诺恢复这些子系统的运行状态，也不是整机备份。

## 不做

不查询目标库来判断重复、冲突、可见性或可召回性；不提供 `knowledge`/`full` 互通、空库恢复、安装归属、卸载、关系及画像状态、图像、向量或时间线备份。不访问正式数据、不调用模型、不推送。

## 待设计问题

无。

## 验收意见

待复审。
