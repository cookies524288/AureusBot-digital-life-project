# MaiBot 长期记忆逐项核对

状态：公开宿主和内置 A_Memorix 源码核对完成；按对照表逐项实施。

## 范围与证据

以 [迁移参考](../13-references-and-migration.md) 记载的 XiaoDou 部署版本为基线：MaiBot 1.3.15、A_Memorix 2.0.0，迁移标记 27 是库结构版本，不是插件版本。核对 [MaiBot v1.3.15](https://github.com/Mai-with-u/MaiBot/tree/062d45cf8bd4272ac57d7ec03fcf405e5a458211) 的固定公开提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211`；早期网页参考提交 `edf42446f123149c57fc79765dd5a8b004b8524c` 不代表正式部署。此提交的 [内置子树](https://github.com/Mai-with-u/MaiBot/tree/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix) 声明版本 `2.0.0`，[同步说明](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/docs/a_memorix_sync.md) 指向独立仓库的 `MaiBot_branch`；独立仓库的预发布标签不替代这份子树。未能从已知发行目录定位本机对应源码，因此只确认公开实现，不推断部署的本地补丁、功能启用状态或运行结果。没有读取私人资料、启动服务或调用真实模型。

上游证据：[知识库界面](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/dashboard/src/routes/resource/knowledge-base.tsx)、[管理接口客户端](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/dashboard/src/lib/memory-api.ts)、[记忆服务](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/services/memory_service.py)、[启发式注入](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/maisaka/memory/heuristic_injector.py)、[按需查询](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/maisaka/builtin_tool/query_memory.py)。

## 从入口到实现

[宿主路由](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/webui/routers/memory.py) 经 `MemoryService` 调用内置 [host_service.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/host_service.py)；管理请求通过 [admin_contracts.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/admin_contracts.py) 分派到运行时处理器，不是外部 `plugins/` 下的特判转发。下面只在追到实际动作时记为已核实实现。

| 行为 | 内置 A_Memorix 2.0.0 的处理器与边界 |
| --- | --- |
| 生命周期维护与删除 | [v5_admin_service.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/v5_admin_service.py#L17) 对关系实际执行强化、冻结、定期保护或固定保护，支持回收站与恢复。[memory_maintenance_service.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/memory_maintenance_service.py#L38) 的后台周期执行冻结/淘汰与孤立资源清理。[delete_admin_service.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/delete_admin_service.py#L23) 另有删除预览、软删除操作记录、恢复和按宽限期清理，并处理派生索引；不是只在界面隐藏。 |
| 反馈纠错与人工纠错 | 按需查询工具执行后，有命中且纠错已启用才由 `enqueue_feedback_task` 保存查询快照、排入延后任务。[feedback_correction_service.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/feedback_correction_service.py#L683) 到期读取会话后续反馈，分类后只对达到门槛的否定或纠正自动执行；可撤下旧关系、写入纠正关系，并按开关标旧段落、排入情景重建或画像刷新。`memory_feedback_admin` 实现列表、详情和从快照回滚，不是逐条有用/无用投票。[correction_admin_service.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/correction_admin_service.py#L35) 另有模糊修正预览、执行和回滚；不是原地编辑句子。 |
| 在线调优 | `memory_tuning_admin` 交给 [retrieval_tuning_manager.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/utils/retrieval_tuning_manager.py#L923)：排队运行评估，查询任务及各轮结果，可取消、导出报告，完成后可应用最优参数。应用默认改运行时，保留上一份参数快照供回滚；宿主的「应用最佳」接口可显式另行持久化，运行时回滚不等于恢复磁盘配置。 |
| 人物画像 | [profile_admin_service.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/profile_admin_service.py#L222) 可查询和刷新画像，独立写入/删除人工覆盖和别名；别名变动可排入刷新。证据纠错会先验证目标确实属于当前画像且可删，再走删除处理器，成功后可强制刷新画像；不是单独改显示文本。 |
| 知识包与完整包 | [bundle_admin_service.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/bundle_admin_service.py#L244) 实现导出、检视、导入、安装记录和卸载。`knowledge` 交换知识条目；`full` 另含选定范围相关的关系和实体状态、情景、事实证据与转换、画像快照及覆盖，并可带图像素材、向量等包成员。导入检查清单和成员校验值；`merge` 合并，`restore` 只收 `full` 且要求目标记忆库为空，不能就地覆盖旧库。卸载依据安装归属处理资源，不等于恢复整机；`full` 也不是 MaiBot 原始聊天、配置和全部持久状态的整库备份。 |

## 对照

| 行为 | MaiBot 的公开依据 | Aureus 已有能力与缺口 |
| --- | --- | --- |
| 查文字记录、实体、关系图和情景 | `searchMemoryRecords`、`getMemoryGraphSearch`、`getMemoryEpisodes`；知识库界面有记录和图谱浏览 | **部分**。`GET /api/resources/memory?kind=all` 按消息、段落、关系、事实、情景、其他经历列出；迁入消息与本地消息共用分类，保留原来源及证据。没有图谱漫游、独立实体浏览或情景维护任务。`test_message_catalog_uses_same_category_without_crossing_edit_ids` 覆盖混合分页。 |
| 预先召回、需要时查询、反馈 | `heuristic_injector` 与 `query_memory` 均经 `MemoryService.search`；查询侧可提交反馈任务 | **类似但不自动调权**。Aureus 的启发式和按需召回仅记录实际进入 Replyer 提示的命中，被 900 字预算截下的候选无评价入口。已登录维护者可在时间线对实际投递过文字的回复逐条标有用、无用或撤销；重复评价不追加重复事件，撤销只追加历史。回复与依据一并校验，沿用回复可见范围，不改原件、权限或排序；旧回复不补造依据。独立开关 `memory.recall_feedback_enabled` 控制入口与新留痕。`FeedbackStore` 仍只记录行动反馈，不参与这里的标注。 |
| 修正、检修、调参、反馈回滚 | 生命周期维护、纠错任务和调优管理分别有实际处理器；[生命周期状态](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/v5_admin_service.py#L135) 实际读取关系状态、汇总和不活跃原因 | **部分**。Aureus 可覆盖正文、停用或恢复召回；新增本地文字索引的只读检修，分项列出停用、移除和可见标签不一致（见下节）。不改写原件，不自动修复。不含上游关系生命周期维护、纠错/调优任务进度或在线调参；人工撤销不等于回滚自动调权或修正。 |
| 删除预览、删除执行、操作记录、回收站和恢复 | `previewMemoryDelete`、`executeMemoryDelete`、`getMemoryDeleteOperations`、`restoreMemoryDelete` 及回收站界面 | **边界不同**。按已确认的规则，只能批量移除本页明确选中的文字记忆；确认显示数量和范围，取消不写入。移除禁止原件及关联派生证据参与召回，不删原件、证据或时间线；恢复不改变原有停用、源状态、有效期或权限。`test_batch_removal_suppresses_real_recall_without_changing_original_states` 验证回到真实回复链的行为；`test_memory_batch_remove_and_restore` 验证 Chrome 窄屏上的确认、取消、跨页隔离、页数更新和原停用状态。无永久清理及独立的删除操作历史。 |
| 人物画像、人工覆盖、别名、证据修正 | `queryMemoryProfile`、`setMemoryProfileOverride`、`setMemoryProfileAliases`、`correctMemoryProfileEvidence` | **部分**。Aureus 读取画像、覆盖与别名原件及证据，保留未解析来源，按实例来源键和真实账号关联，不以昵称合并。当前人物档案索引与画像共用目录，但档案不是生成画像。浏览不自动新建/刷新画像，不提供同等写操作。`test_people_catalog_preserves_exact_accounts_and_pending_profiles` 检查合成身份和待解析资料。 |
| 图片、相似图、图像索引检修 | 知识库有图片重建、补录与重试入口；管理客户端定义图片维护请求与任务结果 | **部分**。Aureus 的图片目录统一浏览归档条目与已索引附件引用；能安全预览已归档图片，收件图相似检索只比较同一模型空间。平台仍持有的原件不下载、不伪造。不等同于补录、按套件导入或检修任务。`test_picture_catalog_filters_and_counts_the_unpaged_snapshot` 检查筛选计数。 |
| 事件流 | 上游有情景、画像、反馈和维护操作的专项记录 | **边界不同**。Aureus 记忆流是已有事件的只读投影，可按事件、会话和时间过滤；不把原始聊天或当前目录快照虚构成维护历史。`test_memory_flow_uses_real_events_and_filters_before_paging` 覆盖筛选先于分页。 |
| 知识包、完整包、合并/恢复、安装管理 | `knowledge/full` 均有导出与导入实现；`restore` 仅向空库恢复，另有安装记录和卸载 | **边界不同**。Aureus 的文字包是版本化 UTF-8 JSONL，仅交换有稳定来源的文字原件、证据和维护覆盖；相同条目跳过，有冲突或坏包整批回滚。普通包排除已移除条目，避免导入新库后重新参与召回；`test_removed_text_is_omitted_from_the_ordinary_package` 验证跨库行为。不是完整备份，不包含本地时间线、附件或图片，不替换现有库、没有安装卸载。`test_memory_file_roundtrip_and_conflicts_are_atomic` 覆盖往返、冲突和原子性。 |

## 本地文字索引只读检修

- 长期记忆 → 文字记录 → 只读检修。登录且完成密码设置后才可查询。参考上游将状态分项说明的做法，不移植其计算、写入或清理逻辑。
- 以时间线原件而不是派生索引上的标签确认可见范围，仅检查已经收进本地文字索引、原件还在且与当前网页用户可见范围相同的条目。一次快照只返回已检查总数、维护停用数、移除数和索引与原件标签不一致的数量；不同原因可重叠，不返回正文、人名、编号或来源。
- 排除未索引、原件缺失、无法确认可见范围及其他可见范围的条目，不统计排除项，避免通过计数泄露其存在。迁入原件、资料来源、附件、画像与召回排序均未检查。零条已检查或零异常不代表全库完整；索引标签不一致仅提示检查派生索引，不在此重建。
- 只读 SQLite 查询，不初始化、注册任务、改写原件、覆盖或状态，不调用模型。登录检查只核对有效期，不续期，也不清理过期登录。
- 合成验收：`test_inspection_checks_only_authorized_originals_without_writing` 覆盖未登录、默认密码、过期登录、伪造可见范围、异域条目冒用网页可见标签、缺失原件、不一致来源及数据库前后不变；`test_inspection_distinguishes_no_coverage_from_read_failure` 区分零覆盖、功能未开与库不可读，并确认不新建库。`test_memory_inspection_shows_counts_and_limits` 验证 390 px 浏览器中分项与局限提示、关闭后保留原列表。接口、浏览器及登录回归合计 20 项通过；涉及的 7 个 Python 文件通过 Ruff 与 Pyright，网页脚本通过 `node --check`。未做正式数据检修。

## 待设计

- 后续只在能确认可见范围的原件上扩展只读检修，例如待解析来源与缺附件；不将本次本地文字索引报告当成迁入原件或媒体完整性证明。
- 若增加批量修正、画像生成/覆盖、自动调权或索引任务，先决定人的授权、状态留痕、撤销范围和素材可见性。测试需覆盖跨会话隔离、同名人物和失败后原件不变。
- 永久删除原件、跨会话包及完整替换/卸载触及用户数据和只追加时间线。需用户另行决定保留与撤销规则，不能从 MaiBot 的按钮反推授权；另行评估所依赖插件源码的版本和许可证。
