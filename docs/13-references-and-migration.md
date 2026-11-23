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

- **人格与表达**：目标使用已配置的 Aureus 身份和人设。迁移只取数据库里学到的表达和黑话；MaiBot 的整个配置（包括人格文本和多套回复风格）排除在迁移范围外。
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

### 2.6 QQ 通道与网页访问

在正式运行目录的 config/aureus.toml 中，将 [qq] 下的 allowed_group_id 设置为带引号的群号。该设置同时限制接收和发送，放行指定群及已绑定维护者的普通私聊，重启后继续生效。修改端点后，可在 Aureus 聊天终端输入 /restart 重新加载。Windows 上如果 SnowLuma 显示 listen EACCES，先核查 TCP 排除范围，再将桥与 Aureus 改到同一个可监听的回环端口。

在 Aureus 数据根目录的 `aureus.toml` 设置 `[qq]`。默认关闭，部署时显式填写 `enabled`、SnowLuma 的正向 WebSocket `endpoint` 和机器人 `account_id`。如果端点启用了令牌，用 `aureus keys set AUREUS_KEY_QQ_ONEBOT` 录入，并填写 `token_key = "AUREUS_KEY_QQ_ONEBOT"`。令牌通过既有密钥接口读取，握手同时提交 Bearer 头和 OneBot 的查询参数。端点与账号由部署者提供，回环以外的连接使用 WSS。连接后用 `get_login_info` 核对机器人账号，再接受消息或发送回复。具体协议对照见[第三方清单](../THIRD_PARTY.md)。

网页设置中的「QQ 账号关联」要求先登录并改掉默认口令。填要绑定的 QQ 号，将网页生成的命令从该账号的普通 QQ 私聊发送给机器人，再回到同一网页会话确认。验证码五分钟内有效、单次使用，可在网页撤销，退出时作废。已有其他人物档案的号码交由人际整合处理。验证码命令不进入对话或器官上下文。已验证账号的普通私聊按用户消息处理，群聊与临时私聊按外部对话处理。消息仍进入同一时间线与记忆链。

网页后端默认只绑定回环，需要登录并设置自定义口令。外网使用可考虑以下路径，均保留应用登录与网页同源检查。

- 单人远程访问可使用经鉴权的私有隧道，将客户端回环端口转发到后端回环地址。检查网页和 WebSocket 经同一入口访问。
- 多设备访问可使用带访问控制的 HTTPS 反向代理。后端继续只绑定回环，代理验证 TLS 证书，转发原始 Host、HTTPS 协议头和 WebSocket 升级。限制受信任的转发头来源，为 HTTPS 会话 cookie 加 Secure，并用部署地址核对登录、Cookie、同源检查与 WebSocket。
- `[web] allow_remote` 开放的是明文 HTTP。公网直连该端口不满足 TLS 要求。3.6 的回环验收不包含公网部署或真实 QQ 切换。

## 3. 网页资源管理参考

参考 MaiBot 公开桌面管理界面的搜索、分页、详情和证据呈现。其表达与黑话页面分别提供搜索、范围筛选、分页及详情操作，长期记忆的文字记录页提供查询、过滤和证据详情。Aureus 使用现有语言库及记忆索引，已登录用户可以只读查询本地经历和迁入原件。

- [表达页面源码](https://github.com/Mai-with-u/MaiBot/blob/edf42446f123149c57fc79765dd5a8b004b8524c/dashboard/src/routes/resource/expression/index.tsx)
- [黑话页面源码](https://github.com/Mai-with-u/MaiBot/blob/edf42446f123149c57fc79765dd5a8b004b8524c/dashboard/src/routes/resource/jargon/index.tsx)
- [长期记忆查询源码](https://github.com/Mai-with-u/MaiBot/blob/edf42446f123149c57fc79765dd5a8b004b8524c/dashboard/src/routes/resource/knowledge-base/tabs/MemoryRecordsTab.tsx)
- [MaiBot 记忆管理手册](https://docs.maibot.chat/manual/webui/memory-management)
