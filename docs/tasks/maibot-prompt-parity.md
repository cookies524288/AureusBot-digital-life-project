# MaiBot 部署基线提示词核对

状态：待评审；属于[网页维护批次追加验收](resource-page-ux.md)。

## 基线与许可

依[部署记录](../13-references-and-migration.md#25-phase-3-目标部署基线2026-11-22)，基线为 [MaiBot 1.3.15 固定提交](https://github.com/Mai-with-u/MaiBot/tree/062d45cf8bd4272ac57d7ec03fcf405e5a458211) 的公开简体中文模板，不代表本机补丁或生效配置。核对该提交的根 [LICENSE](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/LICENSE) 为 GPL 第 3 版，模板目录未见单列许可证；按 [THIRD_PARTY.md](../../THIRD_PARTY.md) 已记录的 GPLv3 第 13 条与本项目 AGPL-3.0 合并规则处理。本轮只取已核实的公开源码片段，不读取个人配置、人设文本、数据库、聊天记录、提示词覆盖文件或凭据，不运行真实模型。

## 实际请求对照

| Aureus 实际任务和顺序 | MaiBot 1.3.15 公开源码 | 实施与不可直接替换之处 |
| --- | --- | --- |
| 规划：`ConversationFlow._plan` → `build_plan_prompt`。`system` 放身份、人设和本轮允许的身体等资料；`user` 放工作记忆、历史、来信、黑话或再审材料、任务、JSON 及可选建议字段。 | `prompts/zh-CN/maisaka_chat{,_focus,_passive}.prompt`、`src/maisaka/context/planner_messages.py`、`src/maisaka/builtin_tool/reply.py`。活动模板可用工具选择目标、等待、终止和查询记忆。 | 只移植「判断局面、规划、说明依据」这一条共通指令；仍由代码选定本轮，输出一个 JSON 建议，不引入工具，不请求内部推理或虚构编号。系统内容、消息角色与协议不等价。 |
| 回复：`ConversationFlow._reply` → `build_reply_request`。`system` 仍不含行为风格；已筛选历史里的 Aureus 发言是 `assistant`，其余历史和来信是 `user`；之后另附一条 `user` 工作记忆、召回、人物/语言和规划参考，最后一条 `user` 承载发言、分段和再审要求。 | `prompts/zh-CN/maisaka_replyer.prompt`、`src/chat/replyer/maisaka_generator_base.py::_build_request_messages` 和 `_build_history_messages`。上游现代路径为 `system` → 多角色历史 → 若干 `user` 参考及最终要求；`retro_prompt.py` 是可选旧式组织。 | 按现代消息顺序接入，保留本项目回话范围、历史标识和覆盖键，不复制平台编号或私有配置，不引入复古选项。生产与网页预览共用回复构建器，随后由适配器渲染同一模板快照。主动联系仍使用自己的任务。 |
| 视觉图片描述：`InboxMedia._describe` → `vlm`，可选调用，提交图片和文字任务；结果仅用于本轮材料。 | `prompts/zh-CN/image_description.prompt`。 | 普通图片使用对应简体中文模板原文；表情识别仍单列，见下文。图片字节、授权、传输格式和模型绑定不变。 |
| 表情识别／登记：`InboxMedia._describe` 的视觉结果；表情选择：`EmojiSelector._text` 文本候选，词义向量可选。 | `emoji_content_analysis.prompt` 的静态图/GIF 拼图及纯文本标签、`emoji_selection.prompt` 的视觉拼图与 `emoji_index` JSON；部署表情插件另外使用文本选择。 | Aureus 不提取 GIF 拼图和五个标签，也不为文本选择制造编号拼图；保留现有描述、索引、登记和成功发送回执。旧插件材料不是 MaiBot 根许可下的同一文件。 |
| 收件会话选择：`select_conversations` 使用独立的候选 JSON、顺序和私聊优先规则，不传完整会话或人设。 | 上游焦点规划与 `context/history` 工具选择材料，未找到同样的未读候选请求。 | 不替换，本地模型只能从已授权候选中建议读哪个会话。 |
| 工作记忆：`WorkingMemory` 合成短会话摘要；回复切点：`ReplyDraft.prompt`；情绪：`ConversationFlow._judge_emotion`。 | `context_compaction{,_focus}.prompt` 属规划器会话压缩，其五键 JSON 和原工具结果不同；上游分段算法已单独登记；未找到同样的独立情绪/受冲判断输出。 | 不用上游五字段替换本地摘要格式，不让切点模型重写原文，不改判断的情绪枚举、强度和可信度约束。 |
| 记忆整理：`MemoryConsolidator.run` 编号批次、证据校验后写入；`ConversationFlow._rerank_memory` 做向量重排。 | `a_memorix_chat_summary.prompt` 提取增量事实、实体、关系、图片引用；`heuristic_memory_impression.prompt` 是另一类印象任务；已移植的召回展示见清单。 | 夜间提取与上游增量写入格式、证据及触发不同，不移植完整指令。向量重排没有对话提示词；只送当前查询及已经筛选的候选文本。 |
| 语言学习：`learn_language` 按可信会话分批，以一次请求抽取增量表达和黑话；规则为 `system`，从 1 编号的样本各占一条 `user`，最后一条 `user` 是输出要求。 | `src/learners/expression_learner.py`、`jargon_learner.py` 同样逐条用 `user` 提交编号样本；上游另有 `learn_style.prompt`、`learn_jargon.prompt`、`jargon_inference*.prompt` 及导入模板。 | 只对齐角色和消息顺序，保持合并的 `proposals` 协议；不复制独立请求或多阶段释义。只接受实际展示且有原件支持的批内编号。细节见[会话分批与证据](language-session-evidence.md)。 |
| 主动 `initiative_plan` / `initiative_reply`、探索摘要、日记、人设回顾。 | 无对应的本项目真实社交需要、外部资料隔离、以日记为依据的人设审核和仅用户可改身份约束。 | 维持原流程及上下文可见性；主动回复复用共通的正文指令，但不冒充上游主动工作流。搜索器本身是 SearXNG 查询，不注入聊天提示。 |
| `voice` 转写、`text_embedding`、`image_embedding`、模型连通性探测。 | 上游可有同类能力，未确认有等价会话提示。 | 转写和向量适配是专用载荷；通用模型探测及缺省系统消息是连接机制，不对应任何聊天模板。实验能量调用使用专用测试请求，不算正式聊天。 |

## 验收

- [x] `test_integrated_prompt_scenes_match_captured_http_and_template_edits` 用 `httpx.MockTransport` 捕获最终 `messages`，验证规划、回复的角色、片段顺序、协议和覆盖优先级；真实心跳产出的流式规划请求由模拟服务按流式协议返回。
- [x] `tests/channels/test_qq_media.py` 用合成 OneBot 媒体核对普通图片、表情和语音各自使用的提示和载荷；不发真实 QQ 消息。
- [x] `tests/web/test_prompt_api.py`、`tests/heartbeat/test_context.py`、`tests/heartbeat/test_planning_flow.py`、`tests/heartbeat/test_inbox_reply_review.py` 和 `tests/heartbeat/test_reply_splitting.py` 核对模板展示、上下文可见性、建议解析、再审与分段。合计七个专项文件 74 项通过；四个改动的 Python 文件 Ruff 检查、格式检查和 Pyright 均通过。未跑全量，未验证真实模型语气或本机私有改动，不称全链等同。

## 现代回复器布局复审补充

- [x] `test_integrated_prompt_scenes_match_captured_http_and_template_edits` 捕获最终 HTTP 请求，逐条对照网页预览和角色顺序，确认历史、来信和参考各出现一次；在同一联测里用真实心跳的合成会话验证本群历史可见、另一群和用户私聊的原文不可见，外部聊天和参考不放入首条 `system`。
- [x] `test_interrupted_group_plan_keeps_review_and_summary_in_its_conversation`、`test_actual_planner_and_replyer_get_saved_summary` 和 `test_pending_replies_are_split_at_working_memory_capacity` 验证保留工作记忆、再审和收到新消息时的规划行为。不带平台会话地址的历史按可见范围筛选；未确认是用户的私聊不读取旧私聊历史。
- [x] 16 文件联合专项先通过 184 项；剩余 6 项旧夹具与回放断言修正后，与新增的 HTTP 隔离一起复跑，13 项均通过。另跑媒体、流式读与取消、分段及 QQ 语言回放，26 项通过。该轮 23 个 Python 文件 Ruff、格式检查和 Pyright 均通过。未调用真实模型或发送真实 QQ。

## 语言学习布局

实际请求和预览使用同一构建器，预览只填合成样本。`test_integrated_prompt_scenes_match_captured_http_and_template_edits` 捕获模拟 HTTP 请求，验证系统规则、逐条资料和末尾输出要求；正文中的 `[id:99]`、`[speaker:SELF]`、`$samples` 原样留在单条 `user` 资料内。原 `language_learner_prompt` 的覆盖键和占位符仍有效；`$samples` 现在是固定的后续样本格式说明，`$output_format` 指向最后一条消息。已有自定义文案若写的是「以下 JSON」或「样本已经内嵌」，维护者可改为「后续逐条提供样本」；本次不读改正式覆盖。
