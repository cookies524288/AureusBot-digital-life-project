# 十项器官职责与接线

状态：器官职责与单轮图已专项复核

## 职责对照

| 职责 | 配置和候选 | 运行入口 |
| --- | --- | --- |
| `replyer` | `organs.replyer`，对话模型；默认由 `[heartbeat] reply_organ` 指向 | `ConversationFlow._reply` 生成对话回复，主动回复沿用相同绑定。 |
| `planner` | `organs.planner`，对话模型；默认由 `[heartbeat] plan_organ` 指向 | `ConversationFlow._plan` 按档位规划，主动发言也可调用。 |
| `memory` | `organs.memory`，对话模型；清空时沿用旧 `summarizer` | `MemoryConsolidator.run` 在提取开关启用、有材料且符合夜间整理条件时，从原文提取事实和偏好，保存前校验证据。 |
| `utils` | `organs.utils`，对话模型；既有任务绑定优先 | 小探索摘要、日记、人设成长和语言学习仍按各自守则启动。当前任务的专职器官未配置对话模型时，心跳把实际调用交给 `utils`。 |
| `fast_model` | `organs.fast_model`，对话模型；`[judge] organ` 有对话模型时优先 | `ConversationFlow._judge_emotion` 满足判断配置和触发条件时调用，专职判断器官空置时使用此模型顺序。 |
| `vlm` | `organs.vlm`，开启 `vision` 的对话模型 | `InboxMedia` 在心跳处理 QQ 来信时读取已缓存图片，生成给本轮对话的描述。 |
| `voice` | `organs.voice`，OpenAI 兼容服务商的 `transcription` 模型 | `InboxMedia` 在心跳处理 QQ 来信时获取转码音频，生成给本轮对话的转写。 |
| `embedding` | `organs.text_embedding`，`kind = "embedding"` | `ConversationFlow._rerank_memory` 对检索词和已召回候选做向量排序。停用或失败时沿用关键词召回顺序。 |
| `image_embedding` | `organs.image_embedding`，`kind = "image_embedding"`，Jina v4 | `inbox_images` 给入站图片建立向量，资源管理页使用已保存向量查找相似图。 |
| `web_search` | `[exploration] search_provider = "searxng"` 和 `search_endpoint`，默认关闭 | 小探索通过 `SearchProvider` 接口搜索；搜索服务配置跟随运行设置重启生效。 |

## 核对

- `test_text_roles_use_configured_routes_across_reload` 用假器官验证记忆绑定、专职优先、通用备用和清空恢复。路由发生在安全网关检查与记账之前。
- 器官关系与提示词目录根据保存后的模型路线显示任务分配。任务启用条件仍由各模块执行。
- 图片、语音、嵌入和搜索使用各自模块的模拟传输或合成数据验收。媒体接线见 `4e9928f`，嵌入见 `16f1d86`，相似图片见 `95f2746`，搜索见 `6246fe3`。

## 第 8 项和第 12 项复审（2026-11-23）

- `a6a7a32` 的模型类型来自配置 API，器官配置抽屉按职责筛选。评审发现网页搜索只能从设置页自行查找，现已在器官页加入「配置网页搜索（WEB_SEARCH）」直达探索设置。Chrome 1200 px 和 500 px 用合成配置验证了入口、搜索服务与地址字段、返回后保留未保存修改。
- `test_normal_tier_plans_then_replies_with_the_planned_intent`、`test_switching_planning_off_gives_one_call_at_normal_energy` 和 `test_a_planner_that_declines_leaves_a_private_record_and_one_line_of_notice` 分别验证规划接入回复、低档或关闭规划时直接调用回复器、沉默留下记录。`test_judges_before_planning_and_records_the_result` 验证判断顺序及入时间线。`test_qq_persists_allowed_conversations_before_notifying` 验证 QQ 先写收件箱再通知。结合 `Heartbeat.beat`、`InboxAttention` 和结果收集源码核对图中入口、唤醒、读取、发送权限和结束分支。图仍保留原器官卡片与模型绑定入口。
- `test_text_roles_use_configured_routes_across_reload` 实际经过假器官和安全网关，验证 `memory`、`utils`、`fast_model` 的路由及旧配置回退。`test_sleep_consolidation_threshold_and_extraction` 另行验证长期记忆整理的启动条件和提取写入。
- `test_qq_reads_saved_media_on_demand` 从模拟 OneBot、收件箱和缓存取件链路核对传给 `vlm`、`voice` 的图片与转码音频。`test_media_organ_routes_and_requests` 在模拟完成与转写接口上核对模型请求形状。这两项分别覆盖附件读取和适配器。
- `test_embedding_probe_returns_latency_and_requests_one_vector` 验证文本向量适配。检索词和候选传入 `text_embedding` 以及停用或失败时保持原顺序，当前由源码核对。`test_image_embedding_uses_jina_protocol_and_reports_usage` 和 `test_image_resource_returns_ranked_sources_only_to_signed_in_user` 分别验证 Jina 适配和登录后按保存向量查找相似图。
- `test_searxng_maps_json_results_in_order_and_limits_results`、`test_searxng_propagates_failure_to_exploration` 模拟搜索服务；`test_idle_exploration_records_start_fetches_second_page_and_saves_finding` 用独立假搜索验证探索调度。SearXNG 运行配置到适配器的装配由源码核对。
- 本轮执行的网页、QQ、规划、判断、探索、适配器和核心热重载专项共 178 项通过。在线服务与真实模型均未启用。
