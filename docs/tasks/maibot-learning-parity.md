# MaiBot 学习功能逐项核对

状态：源码审计完成；学习页已接通无损表情维护、语言包预览和导入导出、三类资源批量移除与恢复、表达人工审核、包记录会话映射和按明确绑定的会话浏览。

## 范围与证据

上游为公开标签 [MaiBot v1.3.15](https://github.com/Mai-with-u/MaiBot/tree/062d45cf8bd4272ac57d7ec03fcf405e5a458211)，完整提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211`。它与 [Phase 3 部署记录](../13-references-and-migration.md#25-phase-3-目标部署基线2026-11-22)的软件版本一致，但本地补丁、实际启用配置和真实模型效果不由本审计推断。旧文档引用的 `edf4244` 是原网页参考，不替代此版本。只检查公开源码和 Aureus 的代码、合成测试；没有读取本机 MaiBot 配置、数据库、日志或聊天记录，没有真实模型、QQ 收发或生产试验。

以下上游 `UI` 指 `dashboard/src/routes/resource/`，上游 `API` 指 `src/webui/routers/`。Aureus 的管理界面在 `aureus/web/static/resource-management.js`，端点在 `aureus/web/resource_api.py`，下表简称「学习页」「资源接口」。标记「类似」表示用户目的有对应，但操作、数据边界、规则或效果不同；「缺」表示未找到同等入口；「边界不同」不得作为全量移植。表中的测试是 Aureus 已存在的合成覆盖（另见末尾本次复跑）；没有测试的缺口不由上游测试代替验收。

## 黑话

| 行为 | 上游源码 | Aureus 对照与结论 | 合成证据 / 待验 |
| --- | --- | --- | --- |
| 查词、列表分页、详情、口径计数 | `UI/jargon/index.tsx`、`JargonList.tsx`、`JargonDialogs.tsx`；`API/jargon.py` 的 `/list`、详情、`/stats/summary` | **类似**。`learningView`、`browse_learning` 提供词、释义、例句查询和分页，详情展示原件/状态。没有上游统计总览、来源贡献统计和逐组证据消息回查。 | `test_learning_resources_filter_and_page_real_observations`、`test_learning_pagination_and_category_switch`；待测统计口径、保留/停用及证据源。 |
| 搜索范围、有效黑话/非黑话、全局/单会话/多组 | `UI/jargon/index.tsx`，`API/jargon.py::apply_jargon_list_filters`、`get_chat_list`、`get_jargon_groups` | **部分**。可浏览单个已知会话明确绑定的黑话，可与文本、启用状态合用；共享给多个会话的包记录在同一列表不重复。「全部资料」不是全局授权。没有独立的全局、共享组、旧会话维度和有效黑话/非黑话归类；停用不等于标记为非黑话。 | `test_learning_browse_combines_explicit_conversation_and_filters`、`test_learning_conversation_filter_replaces_pages_and_ignores_stale_responses`。 |
| 新建、改内容、改所属范围、删除、批量改类和删除 | `UI/jargon/JargonDialogs.tsx`；`API/jargon.py` 的创建、更新、删除、`/batch/delete`、`/batch/set-jargon` | **部分**。`add_learning`、`update_learning` 可新建和以本地覆盖修改、停用单项，原件仍保留；未见可编辑作用域、批处理或删除。上游覆盖/删除不能直接照搬到只追加和保留原件的方案。 | `test_resource_filters_pages_and_reads_evidence`；待补范围和批处理，先定撤销及删除授权。 |
| 独立黑话包导入导出、来源/目标会话映射和作用域去重 | `UI/jargon/JargonDialogs.tsx`；`API/jargon.py` 的 `/export`、`/import`、`scopes_overlap` | **部分**。学习页支持 `maibot.jargon.export` v1 JSON 导出、无写入预览及确认导入。保留原始目标信息；可手工绑定已知收件箱会话，未映射不参与召回，不覆盖同名现有记录。范围不随语言包导出。 | `test_language_package_preview_round_trip_and_deduplication`、`test_explicit_language_scope_filters_real_planner_and_replyer_requests`。 |
| 从近期会话采样、并发/批次限制、收集证据与推断含义 | `src/maisaka/runtime.py` 学习轮及 `src/learners/jargon_learner.py`、`jargon_miner.py`：会话锁、已知词跟踪、证据分组/清洗、推断门槛、上下文推断与比较 | **边界不同**。`main_loop`、`language_runner.py`、`LanguageStore.observe/publish` 按已进入时间线的会话片段，经自身心跳、网关、去重和证据最低门槛学习。没有一比一迁入已知词缓存、会话证据组、独立推断/重推规则或插件钩子。 | `test_language_normal_inbound_across_group_and_private_after_restart`、`test_jargon_stays_out_of_retrieval_until_evidence_exists`；待测证据修订及多阶段释义。 |
| 答复前按作用域和近期文本召回，频次/命中位置排序 | `src/maisaka/jargon_context_matcher.py::match_jargons_for_context`、`_load_scoped_jargon_candidates`、`_load_high_frequency_terms`；`src/config/official_configs.py::JargonConfig` | **类似且范围不同**。`aureus/memory/language_matching.py` 和 `conversation._jargon_reference` 支持多义词、使用频次、位置及字数上限；包记录先按当前真实路由选择明确授权的会话，非包记录维持既有行为。排序、缓存及注入时点不宣称一致。 | `test_jargon_match_keeps_senses_and_uses_frequency_and_position`、`test_explicit_language_scope_filters_real_planner_and_replyer_requests`。 |

## 表达方式

| 行为 | 上游源码 | Aureus 对照与结论 | 合成证据 / 待验 |
| --- | --- | --- | --- |
| 当前会话/分组/全局/旧会话浏览、统计、搜索排序、详情分页 | `UI/expression/index.tsx`、`ExpressionList.tsx`、`ExpressionScopeStatus.tsx`；`API/expression.py` 的 `/list`、`/chats`、`/chat-targets`、`/groups`、`/stats/summary` | **部分**。可按已知会话明确绑定筛选，并与文本、启用状态、人工审核组合后分页；只改变浏览，不改变召回。没有上游分组、全局、旧格式或非当前账号会话的等效筛选或统计；未映射不视为全局。 | `test_learning_browse_combines_explicit_conversation_and_filters`、`test_learning_conversation_filter_replaces_pages_and_ignores_stale_responses`。 |
| 新增、修改、删除、批量删除、清空指定范围 | `UI/expression/ExpressionDialogs.tsx`；`API/expression.py` 的创建、更新、删除、`/batch/delete`、`/clear` | **部分**。学习页支持新增、覆盖和停用，不破坏迁入原件；没有选择会话作为写入目标、批量删除/停用或按会话清空。清空需另行授权，不能从上游交互推导。 | 资源接口合成测试覆盖查询与维护字段；待测安全的批量停用、恢复与保留来源。 |
| 人工检查、拒绝、快速审核队列、批量审查、审核理由和自动过滤日志、从日志批准 | `UI/expression/index.tsx`、`ExpressionReviewLogPanel.tsx`、`hooks/useExpressionReview.ts`、`dashboard/src/components/expression-reviewer.tsx`；`API/expression.py` 的 `/review/*` 和 `/review-status`；`src/learners/expression_review_store.py` | **部分**。Aureus 可按待审、通过、拒绝筛选，或打开快速待审队列；单条/批量审核保存理由和历史，拒绝必填理由且排除回复候选，不删除原件。旧表达默认为待审，仍按原规则召回；通过不跨过停用、移除或待映射。上游单条拒绝只撤掉 `checked`，公开默认不按 `checked` 筛候选；上游批量拒绝删除表达。未移植自动过滤日志、自评或从自动日志批准；本地审核历史不写入 MaiBot v1 语言包。 | `test_expression_reviews_are_append_only_and_gate_reply_candidates`、`test_expression_quick_queue_filters_and_preserves_review_history`。 |
| 表达包导入导出、旧库上传预览和会话匹配 | `UI/expression/hooks/useExpressionImportExport.ts`、`ExpressionDialogs.tsx`；`API/expression.py` 的 `/import`、`/export`、`/legacy-import/*` | **部分**。学习页支持 `maibot.expression.export` v1 JSON 导出、预览与确认；用可选的 `aureus_source_chat_name` 保留每条包记录的原会话名称。可预览并手工绑定已知收件箱会话，不会将未知会话当全局。未实现网页旧库上传。 | `test_language_package_preview_round_trip_and_deduplication`、`test_learning_scope_shows_preview_and_cancel_never_confirms`。 |
| 观察、提案、过滤、训练记录、人审/自评、相似表达合并 | `src/maisaka/runtime.py`，`src/learners/expression_learner.py::_run_learning_batch`、`_filter_expressions`、`_check_expression_before_upsert`、`_find_similar_expression`，`src/config/official_configs.py::ExpressionConfig` | **边界不同**。`language_runner` 和 `LanguageStore` 保存经过验证的建议与可重复命中的来源证据，保留现有表达及例句；没有确认等效于上游独立评估、过滤日志或会话维度相似表达写入。 | `test_observing_existing_expression_keeps_example_and_counts_unique_messages`、`test_language_normal_inbound_across_group_and_private_after_restart`；待测近义合并与审核边界。 |
| 按规划意图、对话语境、说话者、语气/场景择取；可选向量匹配、聚类浏览和向量空间管理 | `src/chat/replyer/maisaka_expression_selector.py`、`expression_vector_index.py`、`src/services/expression_vector_service.py`；`UI/expression/ExpressionClusterBrowser.tsx`、`ExpressionVectorSpaces.tsx`；`API/expression.py` 的 `/clusters*`、`/runtime/vectors/spaces*` | **部分**。`conversation._expression_reference`、`language_matching.py` 使用主题、说话者、规划意图、避用及字数上限。未发现上游表达向量候选索引、聚类成员浏览、构建进度或分空间维护；其他记忆索引/表情向量不计为表达向量。上游公开默认向量关、自评开、仅审核项关。 | `test_expression_topic_speaker_avoid_and_limit`；上游另有 `pytests/maisaka/test_replyer_expression_selection.py`，未运行。 |

## 表情

| 行为 | 上游源码 | Aureus 对照与结论 | 合成证据 / 待验 |
| --- | --- | --- | --- |
| 搜索、状态/格式筛选、排序、分页、密度调整、详情、全库统计 | `UI/emoji/index.tsx`、`EmojiBrowser.tsx`、`EmojiList.tsx`、`MobileEmojiGrid.tsx`、`EmojiDialogs.tsx`；`API/emoji/routes.py` 的 `/list`、详情、`/stats/summary` | **类似，口径不同**。学习页支持按描述、标签、状态、实际图片格式筛选，按最近收集、使用最多、收集最多或最近使用排序；同值以内容摘要和来源稳定排列。格式以摘要校验通过的本地字节为准，缺件或校验失败不计入格式结果，筛选后才分页。全库登记、未登记、禁用、已移除计数不随搜索、筛选、排序或翻页改变。仍保留缩略图密度、详情和按授权读取，不等同于上游按扩展名筛选或使用独立缩略图缓存。 | `test_emoji_catalog_filters_actual_format_before_paging_and_keeps_global_counts`、`test_emoji_filters_sort_and_keep_global_counts_on_narrow_screen`、`test_emoji_density_switches_visible_thumbnail_size`。 |
| 上传单张/批量、可选直接登记；修改描述、登记/取消、禁用、删除和批处理 | `UI/emoji/EmojiDialogs.tsx` 和 `index.tsx`；`API/emoji/routes.py` 的 `/upload`、`/batch/upload`、`PATCH`、`/register`、`/ban`、`DELETE`、`/batch/delete` | **部分**。学习页支持单张或一次选择多张图片并逐张上传，确认前显示数量，逐张报告成功、已存在或失败，完成后保留筛选并刷新列表及全库统计；取消不请求，上传中禁用再次提交。复用单张已认证、同源及逐文件校验入口；上传检查图片实际内容（PNG、JPEG、GIF 或 WebP，最多 5 MB）；新素材先留在未登记，填写描述后再登记。重复上传不覆盖原件、不改来源及计数，也不解除禁用；恢复保留原有登记状态。没有上游独立批量接口、直接登记上传或永久删除。 | `test_emoji_sequential_upload_keeps_existing_metadata_after_bad_file`、`test_emoji_batch_upload_cancel_mixed_results_and_filter_persistence`、`test_emoji_catalog_upload_and_maintenance_preserve_originals`、`test_emoji_upload_edit_search_and_status_maintenance`。 |
| 缩略图缓存统计、清理、预热、清空；后台缓存回收 | `UI/emoji/EmojiCacheMaintenancePanel.tsx`；`API/emoji/routes.py` 的 `/thumbnail-cache/*`；`src/emoji_system/emoji_cache_cleanup.py` | **缺上游同类入口**。Aureus 读取摘要校验的媒体并按网格缩放；未见独立缩略图缓存、维护面板或 MaiBot 的未登记/无文件/孤儿保留任务。上游公开默认每 6 小时检查、保留 30 天。 | 上游有 `EmojiCacheMaintenancePanel.test.tsx`；Aureus 目前不适用，没有等效回收测试。 |
| 普通聊天收图、标记表情、按哈希去重、保留历史使用状态 | `src/emoji_system/emoji_manager.py::ensure_emoji_saved`、`load_emojis_from_db`、`register_emoji_to_db`；`src/config/official_configs.py::EmojiConfig` | **类似**。`aureus/channels/onebot.py` 从标记的 QQ 图片进入 `EmojiStore.collect`，收集合并按 SHA-256，旧条目恢复登记/禁用/使用计数。不是同一文件布局、缓存与频道约定。 | `test_qq_collects_marked_emoji_and_preserves_moderation`、`test_deduplicates_collections_and_tracks_confirmed_use`、`test_import_keeps_source_status_and_counts_when_repeated`。 |
| 后台描述、合规审查、登记额度、定时替换、文件完整性、自定义插件钩子 | `src/emoji_system/emoji_manager.py` 的 `build_emoji_description`、`review_emoji_for_registration`、`periodic_emoji_maintenance`、`replace_an_emoji_by_llm`、`check_emoji_file_integrity`、`register_emoji_hook_specs` | **识别已对齐，生命周期仍不同**。QQ 标记表情经现有媒体安全入口使用上游识别指令，GIF 抽不同帧拼接，静态图用单张；有效分项标签入库，目录可搜索。登记不覆盖已有禁用、移除或人工编辑。未发现整个库周期扫描、64 张额度/满额替换，或独立可选内容审查和插件钩子。 | [QQ 表情识别与标签](emoji-recognition-labels.md)；后续仍需验收容量、主动登记、审查及无损淘汰。 |
| 目录展示工具、选图、原会话投递、回执统计 | `src/maisaka/builtin_tool/show_emoji_list.py`、`send_emoji.py`、`src/maisaka/context/emoji_candidates.py`、`src/emoji_system/maisaka_tool.py` | **类似且策略不同**。`EmojiSelector` 从最终规划建议择取；默认文字选择，可启用向量匹配或切换到 [视觉拼图选择](emoji-visual-selection.md)（待评审）。三条路径保留 Aureus 权限、可发送状态、表情前置/后置和成功回执。视觉模式保序，不随机补选；仍无上游 `show_emoji_list` 工具。 | `test_default_uses_text_and_skips_missing_or_invalid_selection`、`test_semantic_selection_refreshes_incrementally_and_falls_back`、`test_visual_grid_filters_candidates_and_maps_labels_to_the_same_order`、`test_qq_emoji_reply_uses_final_plan_and_counts_only_platform_success`。 |
| QQ 用户端自带表情管理（与学习资源库不同）和第三方 Text Selector | `dashboard/src/routes/chat/UserEmojiManager.tsx`、`API/user_emoji.py`；第三方插件另见 [3.14](3.14.md) | **不计做移植**。上游独立账户上传/删除/选表情不是学习页资料库；Aureus 没有该个人表情面板。3.14 的文字/语义选择是独立实现，不能由行为相似推断与插件或上游原生相同。 | 上游有 `pytests/webui/test_user_emoji_routes.py`；Aureus 仅测试模拟 OneBot 的学习库发送。 |

## 显式会话映射方案与验收

- 上游黑话从持有 `is_global` 或 `session_id_dict` 与相关会话相交的记录中取候选；表达从当前及配置共享组的 `session_id` 中取候选，按上游规则另行处理全局标记和无会话行（`_load_scoped_jargon_candidates`、`_resolve_expression_group_scope`、`_load_all_expression_candidates`）。Aureus 不照搬通配符或把无会话行解释为全局。
- 新导入的语言包记录只保存原件。维护者可在预览后明确选择一个已知会话，或选择多个已知会话共享；未选择、取消或撤销绑定时不进入召回。相同词形的不同包记录各自保有范围与释义，范围先于候选排序/去重。旧的非包记录保持现有可见性；绑定不改停用、移除、审核和原始来源，导出仍以原始包信息为准。
- 验收使用两个群与一个私聊，带同名异义和不同表达，捕获实际 Planner 与 Replyer 请求；只有明确选择的会话可看到对应文本。检查未知目标被拒、预览和取消不写入、解除绑定停止召回，浏览器确认前展示会话清单。

## 建议实施顺序

1. 包记录的会话隔离和明确共享已实现，以两个群、一个私聊和同词异义验证 Planner/Replyer 收到的文本。全局授权、通配符和既有非包材料改用新范围不属于本次变更。
2. 黑话、表达和表情统一提供批量移除与恢复；恢复撤销移除标记，不改变语言启用、待映射、审核状态或表情登记、禁用状态。专项 `test_language_batch_restores_prior_recall_state`、`test_removed_emoji_is_not_selected_and_recovery_keeps_moderation`；不物理删除原件。语言会话筛选、表情独立批量接口仍是差异项。
3. 表达人工审核已接入本地候选筛选。先确认维护者确实需要向量聚类、视觉直选、永久删除或周期媒体淘汰，再决定是否做，不以完整移植名义把上游所有默认项接入生产。

现状可称「已支持学习数据迁移、浏览、部分维护及实际召回/表情投递」，不能称「已完全移植 MaiBot 学习」。

## 在线语言学习：会话分批与证据

[会话分批与提示词证据](language-session-evidence.md)保存可信路由与逐条来源；[在线语言学习](language-extraction-stages.md)在此基础上分别请求 `learn_style` 和 `learn_jargon`。黑话在同一会话累计 4 / 8 / 25 / 100 条不同来源时，进行上下文、仅词条和比较三阶段判定；无释义候选不召回，表达可归纳句式但仍须引用有效编号。平行抽取各自消费来源，重复投递不重复增加证据。

与 MaiBot 的有意差异：在线学习按真实来源会话隔离证据，管理页可按来源筛选；新条目召回不要求当前会话与来源相同，私聊所得允许作为群聊的内部回复材料。提示要求不泄露来源和私人细节；习得语言参考不附来源原文。既有导入包仍按另一套明确授权规则，不把来源标签转换为授权。未移植上游表达自评、过滤日志、向量和相似写入，以及黑话插件、已知词缓存和按上游消息组重建历史；阈值、证据窗口和持久记录也不等价于完整上游运行机制。
