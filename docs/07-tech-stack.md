# 07 · 技术选型与预算

> 用什么技术、哪些现成项目、怎么分配每月的 API 预算。
> 外部项目的状态以 2026 年 11 月 15 日为准。它们变化很快，使用前要重新确认。

---

## 1. 基本技术

以下工具都按 [11-dev-guide.md](11-dev-guide.md) 第 1 节的标准挑选，维护状态于 2026-11-15 确认。

### 1.1 运行时

| 用途 | 选择 | 理由 |
|---|---|---|
| 语言 | Python 3.12+ | 与项目声明的最低版本一致；器官调用、配置与异步任务使用 Python 生态 |
| 并发 | asyncio | 单线程事件循环，底层基于 select/epoll 等系统调用 |
| 数据库 | SQLite（Python 标准库 `sqlite3`） | 数据保存在本地文件。时间线追加使用单写线程；异步查询交给读线程，提交后的监听器回到事件循环执行 |
| 数据结构与配置 | pydantic | 带自动校验的数据结构；配置文件读进来时自动检查 |
| 调用模型 | litellm + openai（Python SDK） | litellm 是统一的多协议调用库（MIT 协议），支持 OpenAI、OpenAI Responses、Anthropic、Gemini 等 100+ 家，同一套代码切换模型只需改配置；openai SDK 由 litellm 在内部调用 |
| 网络 | httpx、websockets | 调用 API、连接平台 |
| 网页通道 | FastAPI + WebSocket | 聊天需要服务器主动推送消息（它主动找你） |
| 观察台 | Streamlit | 纯 Python 写网页仪表盘，不用学前端；只读数据库。放在 pyproject 的可选依赖组 `monitor`，不装不影响核心；用 `aureus monitor` 启动，只绑本机（D64） |
| 单实例锁 | filelock | 成熟的跨平台文件锁，支持 Windows |
| 时区 | zoneinfo（标准库）+ tzdata | 按 IANA 时区名处理夏令时；Windows 没有系统时区库，靠 tzdata 补上 |
| 日志 | loguru | 比标准库的 logging 简单 |

### 1.2 开发工具

| 用途 | 选择 | 说明 |
|---|---|---|
| 依赖管理 | uv | 速度快，一条命令装好环境，并锁定版本 |
| 格式化与检查 | ruff | 自动统一代码格式，查出常见错误 |
| 类型检查 | pyright | 根据类型注解，在运行前查出类型错误 |
| 架构检查 | import-linter | 自动检查"谁不能用谁"，守住架构 |
| 测试 | pytest | 自动检查代码有没有坏 |
| 版本控制 | git | 每一步都能回退 |

### 1.3 实验分析

| 用途 | 选择 | 说明 |
|---|---|---|
| 数据处理 | pandas（暂不装，D82） | 从数据库读出实验数据、整理成表 |
| 画图 | matplotlib（可选依赖 `lab`，D82） | 实验图表 |
| 指标 | scikit-learn（暂不装：Brier 和校准曲线自己算，D82） | 现成的 Brier 分数、校准曲线等 |
| 统计检验 | scipy（暂不装，等 E6，D82） | 实验结果的显著性检验 |

---

## 2. 外部项目

| 项目 | 用来做什么 | 许可证 | 状态与注意 |
|---|---|---|---|
| [A_Memorix](https://github.com/A-Dawn/A_memorix) | 资料迁移与部分检索、整理算法 | AGPL-3.0 | Aureus 自主管理记忆原件及派生索引，不需要启动上游服务或安装同名依赖；移植范围见[第三方代码清单](../THIRD_PARTY.md) |
| [MaiBot](https://github.com/Mai-with-u/MaiBot) | 参考：拟人说话方式、群聊行为、QQ 官方和微信 ClawBot 适配器 | GPL-3.0 | 非常活跃。只学习和移植，不作为运行依赖 |
| [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) | 操作电脑（Phase 4） | MIT | 活跃；通过 MCP 调用 |
| [Ollama](https://github.com/ollama/ollama) | 本地模型服务 | MIT | 活跃 |
| [Mindcraft](https://github.com/mindcraft-bots/mindcraft) | 玩 Minecraft（Phase 6） | MIT | 活跃 |
| [OpenClaw](https://github.com/openclaw/openclaw) | 参考：多平台网关、陌生人配对、技能生态 | MIT | 非常活跃。它以大模型 agent 为主体，理念和我们不同，只参考不依赖 |
| Agent S、微软 UFO、cua | 操作电脑的备选方案 | Apache-2.0 / MIT / MIT | Phase 4 再评估 |

---

## 3. 明确不用的

| 不用 | 原因 |
|---|---|
| 作为 MaiBot 插件来做 | MaiBot 为每个聊天单独创建运行实例，人物按平台账号区分，决策者是大模型。这和"唯一、跨平台的个体"正好相反，插件改不了这个结构 |
| 非官方 QQ 协议（NapCat 等） | NapCat 于 2026-11-12 停止维护，原因未公开，社区推测与法律压力有关；非官方协议还有风控封号的风险 |
| UI-TARS-desktop | 已归档，停止更新 |

---

## 4. 模型分工

具体用哪家的模型，Phase 1 实测价格和拟人效果后再定，写进配置，随时可换。

| 器官 | 需求 | 放在哪 |
|---|---|---|
| 回复器 `replyer` | 中文好、表达自然、像人 | 云端，中档价位 |
| 规划器 `planner` | 判断准、能规划 | 云端，调用次数少 |
| 判断 `judge` | 便宜、快 | 本地小模型或最便宜的云端模型 |
| 总结 `summarizer` | 便宜，量大 | 本地小模型或便宜的云端模型，夜间批量 |
| 看图 `vision` | 能看懂截图 | 优先本地视觉模型，不够再用云端 |
| 向量化 | 给记忆检索用 | 本地，常驻 |

**16 GB 显存的消费级显卡能做什么**：常驻一个中文向量模型；按需跑一个 7B–14B 的量化小模型，负责判断和总结；以后可以试本地视觉模型。注意笔记本显卡发热和耗电，用户打游戏时要让出来——这正是"主机占用"信号的作用。

**人设注入的成本**：不变人格和可变人格要注入器官（见 [03-subsystems.md](03-subsystems.md)）。部分服务商给缓存命中的提示词前缀折扣，但不能只凭提示词排列就认定命中或变快。3.66 保留原有提示词结构，按实报缓存 token 与阶段计时观测，见 [15-model-management.md](15-model-management.md)「回复链路测量」。

---

## 5. 预算

**强制规则**（代码执行，见 [04-body-signals.md](04-body-signals.md)）：

- 月度硬上限由配置设定；
- 每日额度 = 剩余预算 ÷ 剩余天数；
- 每日额度的 15% 只能用于回应用户。

**分配参考**（Phase 1 后按实际数据调整）：

| 用途 | 占比 |
|---|---|
| 和人说话（规划器 + 回复器，含用户储备） | 60% |
| 自主活动（主动联系、探索、日程） | 15% |
| 整理（日记、记忆、印象） | 10% |
| 看图和操作电脑（Phase 4 起） | 10% |
| 机动 | 5% |

两个最"吃钱"的场景是**群聊**和**看屏幕**：群聊不能每条消息都调用模型，要像 MaiBot 那样先用规则判断该不该开口；看屏幕要严格限频，优先用本地模型。外包给编程 agent 的费用另算，每次单独审批。

---

## 6. 许可证

- 采用 AGPL-3.0（见 D12 和仓库根目录的 LICENSE 文件）。A_memorix 是 AGPL-3.0，从 MaiBot 移植的代码是 GPL-3.0，两者可以合并使用，整体用 AGPL-3.0 最省事。

---

## 7. 开发环境

- 在 Windows 11 上开发和测试；Phase 4 的虚拟机方案基于它自带的 Hyper-V
- 编辑器：推荐 VS Code
- uv、git、Ollama
