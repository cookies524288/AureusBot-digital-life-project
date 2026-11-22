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

### 2.1 底线：不碰用户的个人数据

- 只读 MaiBot 公开的源码和文档，弄清数据格式。不打开、不查询任何人本机 MaiBot 的数据库、配置、日志和聊天记录。
- 导入工具做成由用户自己运行的命令；我们用编造的假数据测试。
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
- **可见范围**：MaiBot 的 `session_id` 是聊天流编号，表达里有真实聊天内容。导入时每条都带上来源和可见范围，默认不当作全局；用户逐批确认后才放开。不确定时朝安全方向倒。

### 2.4 导入工具的做法

- 分两段：先读出来，转成 AureusBot 自己定义的中间格式（带版本号的 JSON Lines）；再通过对应子系统的接口写入，因为状态写入必须走子系统接口。第一段现在就能做，第二段等子系统建好再接。
- 以只读方式打开 MaiBot 的数据库（SQLite 的 `mode=ro`），并建议用户先停掉 MaiBot 或对副本操作。
- 先预览（条数、来源分布），用户确认后才写入。可以重复运行，按来源 ID 去重；每条记录标明来源 `maibot-import`。
- 工具放在 `tools/migrate/`；动手前先更新 11 号文档的目录图。
