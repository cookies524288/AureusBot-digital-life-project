# 第三方代码清单

移植（复制过来再改）的代码都登记在这里；只依赖的库见 `pyproject.toml` 和 `uv.lock`。规则见 docs/11-dev-guide.md 第 1.3 节：移植的文件开头也要注明来源、原许可证和改动。

| 本项目的文件 | 来源 | 原许可证 | 我们改了什么 |
|---|---|---|---|
| `aureus/heartbeat/speech.py` | MaiBot `src/chat/utils/utils.py` 里的 `_split_into_sentence_segments`、`_merge_processed_segments_to_max_count`、`protect_kaomoji`、`recover_kaomoji`，https://github.com/Mai-with-u/MaiBot/blob/main/src/chat/utils/utils.py | GPL-3.0 | 去掉按概率合并的随机步骤，改成确定性的；只在句号、感叹号、问号、分号和换行处拆；条数超限时均匀合并而不是返回默认回复；去掉对配置、平台、日志和 LLM 断句的依赖，改成纯函数 |
| `aureus/heartbeat/recall.py` 的 `format_recall` | MaiBot `src/maisaka/builtin_tool/query_memory.py` 里的 `_build_replyer_memory_reference`，https://github.com/Mai-with-u/MaiBot/blob/edf42446f123149c57fc79765dd5a8b004b8524c/src/maisaka/builtin_tool/query_memory.py | GPL-3.0 | 保留查询、命中数、编号行和空白归一；改用本项目的 MemoryHit、当地时间和人物称呼；补无命中说明和 900 字整条裁剪；不接入它的工具框架和记忆后端 |
| `aureus/memory/language_matching.py` 的黑话匹配 | MaiBot 安装包 1.3.15，`src/maisaka/jargon_context_matcher.py` 的 `_normalize_match_text`、`_contains_mechanical_match`、`_calculate_match_score`、`match_jargons_for_context`，https://github.com/Mai-with-u/MaiBot | GPL-3.0 | 沿用小写和空白归一、子串、频率和上下文位置计分、十条上限。以词与释义为去重键，保留各义的证据。 |
| `aureus/tools/migrate/maibot_language.py` 的表达清洗 | MaiBot 安装包 1.3.15，`src/learners/expression_style_utils.py` 的 `strip_expression_style_usage_prefix`、`is_prompt_example_expression_style`，https://github.com/Mai-with-u/MaiBot | GPL-3.0 | 召回索引移除「使用」前缀并排除提示示例，原值和排除原因仍保存在语言库。 |
| `aureus/memory/history.py` 的 `search_history` | 安装版 A_Memorix 2.0.0 的 `core/storage/metadata_fact.py::list_current_person_fact_claims` | AGPL-3.0 | 复用有效状态与左闭右开的时段条件，读取 Aureus 时钟，为记忆原件增加内容与人物过滤 |
| `aureus/memory/sparse.py` 的 `ranked_matches` | 安装版 A_Memorix 2.0.0 的 `core/storage/metadata_fts.py::fts_search_bm25` | AGPL-3.0 | 移植 SQLite FTS5 MATCH 和 BM25 的稀疏排位，接到 Aureus 原件及时间线索引。使用标准库 SQLite，脱离 MaiBot 运行 |
| `aureus/memory/consolidation.py` 的 `read_candidates` | 安装版 A_Memorix 2.0.0 的 `core/utils/memory_lifecycle_policy.py::retention_at` | AGPL-3.0 | 沿用指数半衰期公式，保留源证据，以 Aureus 时钟和可配置半衰期计算派生候选权重 |
| `aureus/tools/migrate/maibot_memory.py` 的 `_message_person` | 安装版 MaiBot 1.3.15 的 `src/person_info/person_info.py::get_person_id` | GPL-3.0 | 保持平台字段规范化和哈希规则，附逻辑实例名，匹配同源 A_Memorix 人物键 |
| `aureus/web/static/ark-pixel.woff2` | TakWolf/ark-pixel-font，2026-11-10 版，12 像素比例简体中文 WOFF2，https://github.com/TakWolf/ark-pixel-font | SIL OFL 1.1 | 原样随网页分发，许可证见同目录 `ark-pixel-OFL.txt` |
| `aureus/heartbeat/context.py` 的 `plan_task`、`reply_instruction`、`build_reply_request` | MaiBot 1.3.15 的 [maisaka_chat.prompt](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN/maisaka_chat.prompt)、[maisaka_replyer.prompt](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN/maisaka_replyer.prompt) 和 [maisaka_generator_base.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/chat/replyer/maisaka_generator_base.py) | GPL-3.0 | 原样取规划判断句、口语化指令行和现代 `_build_reply_instruction` 的正文指令；沿用现代回复请求的角色历史、参考和最终要求次序。保留本项目身份和参考格式、任务字段、上下文权限、工具隔离、分段与再审协议和覆盖键；未复制上游工具、可见回复出口或其系统模板整体。 |
| `aureus/heartbeat/inbox_media.py` 的 `_IMAGE_DESCRIPTION`（由 `build_image_description_request` 供收件识别和网页预览共用） | MaiBot 1.3.15 的 [image_description.prompt](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN/image_description.prompt) | GPL-3.0 | 普通图片的简体中文正文原样保留；网页只显示具名图片占位。 |
| `aureus/heartbeat/emoji_recognition.py` | MaiBot 1.3.15 的 [emoji_content_analysis.prompt](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN/emoji_content_analysis.prompt)、[emoji_manager.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/emoji_system/emoji_manager.py) 和 [utils_image.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/common/utils/utils_image.py) | GPL-3.0 | 原样使用简体中文指令，沿用差异帧水平拼接与标签分隔规则；补解码/画布上限，使用无溢出的 Pillow 差值统计，在本项目授权、登记和目录边界内使用。 |
| `aureus/heartbeat/emoji_visual_selection.py` 的选择指令 | MaiBot 1.3.15 的 [emoji_selection.prompt](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN/emoji_selection.prompt) 与 [send_emoji.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/maisaka/builtin_tool/send_emoji.py) | GPL-3.0 | 改编简体中文指令，参照上游的行列布局及一基编号重新绘制 PNG；按真实候选调整数量和说明，允许 0 并拒绝无效结果，不回退首项。 |
| `aureus/common/language_prompts.py` 的在线语言模板、`aureus/memory/language_online.py` 的释义档位 | MaiBot 1.3.15 固定提交的 [简体中文提示词目录](https://github.com/Mai-with-u/MaiBot/tree/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN)（`learn_style`、`learn_jargon`、`jargon_inference_with_context`、`jargon_inference_content_only`、`jargon_compare_inference`），以及 [jargon_miner.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/learners/jargon_miner.py) | GPL-3.0 | 根据上游协议和 4 / 8 / 25 / 100 档位重写本地模板、请求编排和存储。保留本项目网关、同会话证据和跨会话内部召回规则；未复制上游实现。 |
| `aureus/common/language_prompts.py` 的 `learn_style` 输出要求 | MaiBot 1.3.15 的 [learn_style.prompt](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN/learn_style.prompt) | GPL-3.0 | 原样取输出要求首行，明确不要输出重复内容；保留本地示例、证据编号规则和空结果协议，不复制群聊假设或上游固定示例。 |
| `aureus/heartbeat/language_runner.py` 的在线表达过滤 | MaiBot 1.3.15 的 [expression_learner.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/learners/expression_learner.py#L639) 和 [expression_style_utils.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/learners/expression_style_utils.py) | GPL-3.0 | 移植「使用」前缀清洗、风格与机器人名字或昵称完全相等及完整来源含 `SELF` 的过滤；保留本地「使用：」、编号映射、证据、去重和召回边界。未接入模型自评、插件、日志或上游存储。 |

## 表情周期维护行为参考

`aureus/heartbeat/emoji_maintenance.py` 与 `aureus/memory/emoji.py` 参考 MaiBot 1.3.15 的 [`periodic_emoji_maintenance` 和 `check_emoji_file_integrity`](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/emoji_system/emoji_manager.py#L1106)（GPL-3.0）。沿用等待 10 分钟后重查发送候选的机制；本地重新实现，不删除条目或原件，不扫描并登记陌生文件。

## 许可证核对记录

- MaiBot：2026-11-18 读了仓库根目录的 `LICENSE`，是 GNU GPL 第 3 版。GPL-3.0 第 13 条允许把它覆盖的作品和 AGPL-3.0 的作品合并成一个整体，本项目整体采用 AGPL-3.0（D12），所以可以移植。
- Phase 3.4 核对部署包 `maibot-1.3.15.dist-info/METADATA` 和 `licenses/LICENSE`，版本 1.3.15，随包许可证为 GPL 第 3 版。
- 该仓库根目录还有 `EULA.md` 和 `PRIVACY.md`，没有读过，没有核对它们对源码有没有额外限制。
- A_Memorix 2.0.0：安装源码随附的 `LICENSE` 为 AGPL-3.0。`LICENSE-MAIBOT-GPL.md` 对 MaiBot 单独授予 GPL-3.0 权利，本项目沿用默认 AGPL-3.0。

## 协议参考

SnowLuma 2.1.1 对接以本机安装的 snowluma-adapter 1.1.8 为基线，`_manifest.json` 标记 GPL-v3.0-or-later。Aureus 按下列接口行为独立编写通道，不随项目复制或打包适配器代码。

| Aureus 文件 | snowluma-adapter 1.1.8 对照路径与函数 |
|---|---|
| `aureus/channels/onebot_transport.py` | `transport.py` 的 `QQTransportClient._build_connect_url`、`_build_headers`、`call_action`、`_receive_loop`，以及 `runtime/router.py` 的 `bootstrap_adapter_runtime_state`、`_extract_self_id_from_login_response` |
| `aureus/channels/onebot.py` | `runtime/router.py` 的 `handle_transport_payload`、`handle_inbound_message`，`codecs/inbound/message_codec.py` 的 `build_message_dict`、`_require_message_segments`、`convert_segments_with_metadata` |
| `aureus/channels/onebot.py` | `profile.py` 的 `SNOWLUMA_PROFILE`，`codecs/outbound/message_codec.py` 的 `_build_send_action`，以及 `codecs/outbound/segment_encoder.py` 的 `_build_at_segments`、`_build_reply_segments` |

## Phase 3.4 行为参考

| MaiBot 1.3.15 源码 | 对照结果 |
|---|---|
| `src/maisaka/jargon_context_matcher.py` 的 `_load_scoped_jargon_candidates`、`_load_high_frequency_terms` | 上游要求正向黑话标记和非空释义，匹配时无需等最终推断完成。统一库的本地候选仍要达到首次推断的证据门槛，已标为黑话且有词和释义的导入候选可在没有可定位源消息时参与匹配（D92）。词频按规范化词合计次数、取最小正排名。 |
| `src/chat/replyer/maisaka_expression_selector.py::select_for_reply`、`_build_expression_query_text` 和 `src/maisaka/builtin_tool/reply.py` | 保留 Planner 的六个意图字段和在 Replyer 组装材料时注入参考的顺序。统一库按当前话题与意图重合度排序，用说话人和观察次数决胜。有情景和正文的表达可在原库缺例句时参与选择（D92）。选择是确定性的，无需额外的向量或选择器官调用。 |
| `src/learners/expression_learner.py::_filter_expressions`、`_upsert_expression_to_db` | 只从人类的本次入站材料学习，按消息编号核验证据，过滤自身和媒体标记，按相同情景和表达保存，原样重现追加证据。Aureus 的建议只接收在原始消息中实际出现的短语。 |
| `src/learners/jargon_learner.py::_extract_learning_sources_from_context`、`src/learners/jargon_miner.py::_should_infer_meaning` | 保留来自真实用户样本、排除参考文本和首次四条证据的规则。Aureus 按词和释义分别累积有编号的证据，可同时保留多义。后续推断阈值和上游会话暂存不在独立运行链中。 |
