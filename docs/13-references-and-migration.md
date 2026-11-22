# 13 · 参考项目与 MaiBot 数据迁移

> 2026-11-15 新增。记录参考项目和许可证结论，方便以后查阅。

---

## 1. 参考项目

许可证是 2026-11-15 读取各仓库的 LICENSE 文件或 package.json 核实的。维护状态当时没能核实（GitHub 接口被限流），使用前要重新确认。三种用法（依赖、移植、参考）见 [11-dev-guide.md](11-dev-guide.md) 第 1.3、1.4 节。

| 借鉴什么 | 项目 | 许可证 | 怎么用 |
|---|---|---|---|
| 表达方式、黑话、拟人说话 | [MaiBot](https://github.com/Mai-with-u/MaiBot) | GPL-3.0 | 已移植回复分段与记忆引用格式，详见[第三方代码清单](../THIRD_PARTY.md) |
| 记忆 | A_memorix | AGPL-3.0 | 作为依赖，外面包一层我们的接口（D4），Phase 3 接入 |
| 自主行动 | OpenClaw | MIT | 可以移植；它是 TypeScript 项目，实际只参考设计 |
| 自主行动（备选） | VCPToolBox | CC BY-NC-SA 4.0 | 有「非商业」限制，与 AGPL-3.0 不兼容：只读思路，不复制代码 |
| 自我进化 | Hermes Agent（Nous Research） | MIT | 可以移植；它的技能兼容 agentskills.io 标准，Phase 5 参考 |
| 激素、情绪 | digital-life-core | package.json 写 MIT，仓库里没有 LICENSE 文件 | TypeScript 项目，只参考设计，不复制代码 |

digital-life-core 值得借鉴的设计：用 `genome.json` 声明每种激素的基线、半衰期和上下限，激素随时间衰减回基线；再用「气质」几个旋钮整体调节敏感度和恢复速度。我们的激素 `hormones`（D20）按同样的思路，用配置声明而不是写死在代码里，开关和消融实验也更方便。

---

## 2. 迁移 MaiBot 的数据

### 2.1 来源与授权

- 版本兼容以目标版本的源码和实际库结构为准。2026-11-22，维护者授权 Phase 3 为迁移南北绿豆读取该实例的相关数据。授权仅用于此次迁移；持续集成和代码审查使用模拟数据。
- 导入命令可由维护者运行，也可在授权的本机独立目标目录执行。源数据只读，公开仓库不收录原始数据、聊天片段或账号。密钥沿用独立配置流程。
- 表达方式的 `content_list` 里存的是真实聊天片段，迁移前要让用户知道这一点。

### 2.2 MaiBot 的数据格式（读公开源码得到，2026-11-15）

| 内容 | 位置 | 要点 |
|---|---|---|
| 表达方式 | 主数据库表 `expressions` | `situation`（情景）、`style`（风格）、`content_list`（JSON 字符串数组，真实聊天片段）、`count`、`last_active_time`、`create_time`、`session_id`（空表示全局）、`checked`（是否人工审核）、`modified_by`。另有向量索引文件，是派生数据：导入后重建，不迁移 |
| 黑话 | 主数据库表 `jargons` | `content`、`meaning`、`evidence_messages`（JSON，证据消息引用）、`session_id_dict`（JSON，各聊天流里的次数）、`count`、`is_jargon`、`is_complete`、`is_global`、`last_inference_count`、`created_by`（AI 或 MANUAL）、创建与更新时间 |
| 长期记忆 | A_Memorix：配置 `config/a_memorix.toml`，数据 `data/a-memorix/` | 和我们 Phase 3 要接入的是同一个项目。它自带导入指南和脚本（如 `migrate_maibot_memory.py`）；MaiBot 一侧是 2.0.0，版本差异到 Phase 3 再核对 |
| 人设 | 公开的人设导入协议 v1（`format` 为 `maibot.personality`） | 字段有 `personality`、`behavior_style`、`reply_style`、群聊和私聊提示词等 |

### 2.3 对应到 AureusBot

- **人设**：`personality` 作为可变人格的初稿，`behavior_style` 对应行为风格，`reply_style` 对应表达风格。不变人格和身份事实只有用户能改，不从导入文件里取。群聊、私聊提示词暂时没有对应项，原样存档。
- **表达方式、黑话**：AureusBot 里的存放位置还没设计，所以先做中间格式（见 2.4）。
- **统一记忆库**：群聊、私聊和缺少会话编号的历史资料全部保留。导入与运行时按内容、说话人、时间和来源证据组织；说话人或时间原本缺失时保留缺失状态。原始聊天流编号可随源记录存档。

### 2.4 导入工具的做法

- 分两段：先读出来，转成 AureusBot 自己定义的中间格式（带版本号的 JSON Lines）；再通过对应子系统的接口写入，因为状态写入必须走子系统接口。第一段现在就能做，第二段等子系统建好再接。
- 以只读方式打开 MaiBot 的数据库（SQLite 的 `mode=ro`），并建议用户先停掉 MaiBot 或对副本操作。
- 先预览（条数、来源分布），用户确认后才写入。可以重复运行，按来源 ID 去重；每条记录标明来源 `maibot-import`。
- 迁移工具放在 `aureus/tools/migrate/`，安装 Aureus 后可运行 `aureus migrate-maibot inspect <SQLite 文件>`。

### 2.5 Phase 3 目标部署基线（2026-11-22）

维护者确认，目标实例为 XiaoDou（南北绿豆）。只取活动 MaiBot 主库、活动 A_Memorix 库和属于这个实例的附属数据。旧 MaiBotData 在此次范围之外。活动主库采用 WAL，统计与导出均需要包含已提交的 WAL 记录。主库版本标记为 45，A_Memorix 迁移标记为 27，两者独立于软件发布版本。

部署版本为 MaiBot 1.3.15、A_Memorix 2.0.0、snowluma-adapter 1.1.8 和 SnowLuma 2.1.1。部分运行文件带本地改动。行为以安装版本为准，移植前逐文件核对来源与许可。群聊适配器使用本机 WebSocket，已有客户端正在收发，最终接管需另行确定测试群及切换窗口。

| 功能 | 活动部署的调用路径 | 对照内容 |
|---|---|---|
| 规划和回复 | `maisaka/reasoning_engine.py::run_loop` → `chat_loop_service.py::chat_loop_step` → `builtin_tool/reply.py::handle_tool` → `get_replyer` → `MaisakaReplyGenerator.generate_reply_with_context` | 当前选显式 reply；保留目标消息、reply_reference 与 expression_intent；测试沉默与多人插话 |
| 记忆 | Planner 启发式 `build_injection_message` 和显式 `query_memory.handle_tool` 均经 `MemoryService.search` 接入 A_Memorix | 分别验证主动注入、按需查询和 Replyer 可见的引用；记忆库的段落、事实和 episode 保留来源与证据链 |
| 黑话 | `_refresh_jargon_reference_message` → `jargon_context_matcher.build_jargon_reference_message` → `match_jargons_for_context` | 结合 jargons、会话高频词和近期消息，按作用域选词供 Planner 使用；学习入口为 jargon_learner |
| 表达 | Replyer 构造上下文时调用 `maisaka_expression_selector.select_for_reply`、`expression_vector_index.select_candidates` 和表达选择器 | 先限定会话，以表达意图检索，再选择并写入回复上下文；学习入口为 expression_learner |

当前部署关闭 retro prompt，Replyer 的即时消息历史不直接包含 Planner 的全部工具输出。功能验证需要检查 Replyer 真正收到的证据。核心库还包括 person_info、chat_sessions、mai_messages，人物识别保留原平台和账号，原始会话记录可供消息追溯。附属人际状态、关系边、图片记忆与插件效果分别核对。Aureus 在统一记忆库内按人物、时间和情景召回，当前投递地址由通道处理。
