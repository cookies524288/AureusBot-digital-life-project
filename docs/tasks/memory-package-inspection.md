# 长期记忆：文字包导入前检视

状态：待实现
范围：路线图 Phase 3 的资源管理后续；沿用 D99 的可逆移除边界。

## 先读

[资源管理](resource-management.md)、[长期记忆对照](maibot-memory-parity.md)、[开发守则](../11-dev-guide.md)；本地 `aureus/memory/transfer.py`、`aureus/web/memory_transfer_api.py` 和 `aureus/web/static/memory-flow-transfer.js`。

固定 MaiBot 1.3.15 内置 A_Memorix 2.0.0 [管理分派](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/bundle_admin_service.py#L244)、[装载校验](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/bundle_admin_service.py#L988)、[导入](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/bundle_admin_service.py#L1817)：检视走包装载与校验并返回摘要、图片向量兼容信息；导入再装载，另查目标安装范围、安装状态与合并/恢复条件，才写入。只借鉴分开检视与确认的流程，不移植代码。

## 目标和验收

- 实际长期记忆 → 导入导出页选文件即检视，完整校验后可取消或确认追加。坏包、文件变更、取消和迟到结果不得沿用旧确认。
- `POST /api/resources/memory/transfer/preview` 使用旧导入的登录、同源、媒体类型和 2 GiB 上传限制，共用落盘临时文件和流式解析。扫描到包尾才返回；不连接本地记忆库、不修改记忆、不留存包或整包解析结果。
- 响应只有版本、包内条目总数及按来源表可可靠区分的固定分类计数，未知表合计为其他。无正文、来源键、证据细节或文件名。分类总和等于包内条数，不预估新增。确认把已检视的同一个浏览器 `File` 原样交给原 `POST /api/resources/memory/transfer`，旧接口无需先检视，仍按原样重新校验、原子追加、跳过相同或整包回滚。

## 改动一览与接口

记忆交换层在既有解析器之上增加文件摘要；Web 层增加检视入口及共用接收逻辑；导入导出页增加当前选件的检视状态、明确确认、取消及结果。无新增配置。固定的少量计数在 390 px 窄屏可读，不按文件条目增长。

## 测试与子步骤

1. 先补合成接口和 390 px Chrome 回归并观察旧版失败；实现数据检视和网页状态机，跑改动及相邻专项。
2. 检查改动、记录证据及与上游的差异，提交和交接给 Astra。该独立工作树不运行正式实例或操作主线。

## 自检清单

- [ ] 混合包、空包和有坏尾的包按预期处理；登录、同源、类型和长度限制有效；不读取/写入目标库，响应不泄露正文。
- [ ] 390 px 实际网页覆盖加载、失败、取消、换文件、迟到预览、确认与结果；无确认时不调用写接口。
- [ ] 确认走真实导入；测试证实重复跳过，冲突整批回滚，旧接口保持兼容。
- [ ] 专项先红后绿，记录检查结果与与上游的剩余差异。

## 不做

不查询目标库来判断重复、冲突、可见性或可召回性；不提供 `knowledge`/`full` 互通、空库恢复、安装归属、卸载、关系及画像状态、图像、向量或时间线备份。不访问正式数据、不调用模型、不推送。

## 待设计问题

无。

## 验收意见

待复审。
