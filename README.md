# AureusBot

**一个独立存在的数字生命。**

[English version](README.en.md)

AureusBot 运行在你自己的电脑上。它连续、独立、唯一，不依附任何聊天平台。
大模型是它的器官，负责理解、思考和说话；它的身体感受是真实的系统信号。

它首先是你的陪伴者，用拟人的方式和你相处、提供情绪价值。
需要干活时，它仅将专业工作外包给其他 agent，自己只负责决定、委托、验收和汇报。这些能力按路线图逐步实现。

## 当前状态

认知内核的 Phase 0（地基）、Phase 1（自我监控）和 Phase 2（反馈驱动）均已完成。Phase 2 的八项整体验收已通过，下一阶段是内核验证。[路线图](docs/08-roadmap.md) · [阶段验收记录](docs/tasks/2.13.md)

## 快速开始

需要 Python 3.12 或更高版本，以及 [uv](https://docs.astral.sh/uv/)。

```bash
uv sync                                         # 安装依赖
uv run aureus --home ./aureus_home init         # 建好数据根目录
# 把 config/identity.example.toml 复制到 aureus_home/config/identity.toml，再按自己的想法填写
uv run aureus --home ./aureus_home chat --fake  # 用假器官试运行：不调用模型、不花钱
uv run aureus lab list                         # 列出可离线运行的实验
```

源码运行默认把用户数据放在项目根目录的 `aureus_home/`，移动整个项目即可一起带走；升级时保留这个文件夹。需要另存到其他位置时，可用 `--home` 或环境变量 `AUREUS_HOME` 指定，见 [docs/14-user-data.md](docs/14-user-data.md)。网页默认地址为 `http://127.0.0.1:28976`。接入真实模型见 [docs/15-model-management.md](docs/15-model-management.md)。

提交代码前，用 `uv run python scripts/check.py` 一次跑完格式、类型、架构和测试检查。

---

## 四个核心特性

| 特性 | 含义 | 怎么做到 |
|---|---|---|
| **独立** | 不依附任何平台或模型厂商 | 自己的进程和数据目录；平台只是可插拔的"通道"，模型只是可替换的"器官" |
| **连续** | 经历是一条不断的线，重启不会失忆 | 唯一的时间线，只追加；状态全部持久化；醒来知道自己睡了多久 |
| **唯一** | 同一时刻只有一个它，在所有平台上都是同一个它 | 单实例锁；时间线不分叉；所有通道共享同一个注意力、同一份记忆和人际关系 |
| **自主** | 没人找它时，也会按自己的状态和打算行动 | 常驻的心跳循环；自己的日程；由真实身体信号驱动，受预算和权限约束 |

## 两条根本原则

**大模型是器官，不是主体。**
身份、身体、情绪、记忆、关系这些状态归核心程序所有，按代码里的规则更新。大模型负责理解、思考和说话，但只能提建议，不能直接改写任何状态。

**没有装饰。**
每一种内部感受都必须同时做到两点：

1. 来自真实可测的信号，比如预算、负荷、故障、时间，而不是大模型编出来的；
2. 通过代码真实地改变行为，比如换模型、放慢节奏、请示用户，而不只是在提示词里写一句"你很累"。

做不到这两点的状态则不做。

---

## 两个部分

| 部分 | 内容 | 说明 |
|---|---|---|
| **第一部分：认知内核** | 核心骨架；四层人设；自我监控（身体信号）；反馈驱动（自信、激素、心情、自主行为）；终端和网页通道；观察台和实验台 | 详见 [docs/12-kernel.md](docs/12-kernel.md) |
| **第二部分：扩展** | 长期记忆；QQ 等平台；跨平台认人；操作电脑；外包与技能；互联网生活 | 在内核之上逐步添加 |

## 子系统一览

| 分组 | 子系统 | 代码名 | 一句话 |
|---|---|---|---|
| 我是谁 | 身份 | `identity` | 身份事实和四层人设：不变人格、可变人格、行为风格、表达风格 |
| | 身体 | `body` | 真实的身体感受：精力、疲劳、压力、社交需求、睡眠 |
| | 激素 | `hormones` | 几分钟尺度的短期情绪：怒气、愉悦、低落、惊讶 |
| | 心情 | `mood` | 几小时到几天的整体基调 |
| 我的经历 | 时间线 | `timeline` | 唯一一条只追加的经历记录，每天整理成日记 |
| | 记忆 | `memory` | 把经历沉淀成以后想得起来的东西，后端用 A_memorix |
| | 人际 | `people` | 认识的每个人一份档案，不管对方用哪个平台 |
| | 技能 | `skills` | 学会的做事方法，可以自己总结、自己写 |
| 我怎么想、怎么做 | 心跳 | `heartbeat` | 常驻主循环，每一拍决定接下来做什么 |
| | 注意力 | `attention` | 此刻专注哪件事，提示词里放什么 |
| | 日程 | `agenda` | 目标、待办、承诺和自己的小项目 |
| | 器官 | `organs` | 所有大模型调用的统一入口：规划器、回复器、判断、总结、看图 |
| | 行动 | `actions` | 发消息、调工具、操作电脑、外包工作 |
| | 反馈 | `feedback` | 做事前写预期，做完对结果，用差距来学习 |
| 我和世界 | 通道 | `channels` | 终端、网页、QQ、微信……平台只是通道 |
| | 安全 | `safety` | 权限、审批、审计、预算上限、急停，贯穿所有层 |

另有两个工具：**观察台**（`monitor`）让你随时看到它的身体信号、情绪、注意力、时间线和花费；**实验台**（`lab`）用来做可复现的开关对比实验。

## 用到的现成项目

| 用途 | 项目 | 用法 |
|---|---|---|
| 长期记忆 | [A_memorix](https://github.com/A-Dawn/A_memorix) | 作为记忆后端，外面包一层我们自己的接口 |
| 拟人说话 | [MaiBot](https://github.com/Mai-with-u/MaiBot) | 参考它的规划器/回复器分层、说话方式、群聊行为和平台适配器 |
| 操作电脑 | [Windows-MCP](https://github.com/CursorTouch/Windows-MCP) | 在隔离环境里做它的"手" |
| 本地模型 | [Ollama](https://github.com/ollama/ollama) | 在本地显卡上跑向量化和轻量任务 |

为什么这样选，见 [docs/07-tech-stack.md](docs/07-tech-stack.md)。

## 与 MaiBot 的关系

AureusBot 是独立开发的 MaiBot 衍生项目。它移植了两部分 GPL-3.0 代码，分别用于回复分段与合并，以及记忆检索结果的引用格式化。移植范围、源文件和改动记录见第三方清单。项目整体采用 AGPL-3.0。[MaiBot 上游](https://github.com/Mai-with-u/MaiBot) · [上游许可证](https://github.com/Mai-with-u/MaiBot/blob/main/LICENSE) · [第三方代码清单](THIRD_PARTY.md) · [AureusBot 许可证](LICENSE)

## 文档

```text
docs/
├── 01-vision.md                    目标与边界（先读这个）
├── 02-architecture.md              总体架构与主循环
├── 03-subsystems.md                各子系统的职责与规则
├── 04-body-signals.md              身体信号：挂在真实信号上的内感受
├── 05-people.md                    人际关系与跨平台身份
├── 06-safety.md                    安全与权限
├── 07-tech-stack.md                技术选型与预算
├── 08-roadmap.md                   路线图与验收标准
├── 09-glossary.md                  术语表
├── 10-decisions.md                 决策记录与待定问题
├── 11-dev-guide.md                 开发守则（写代码前先看）
├── 12-kernel.md                    第一部分：认知内核
├── 13-references-and-migration.md  参考项目、许可证结论、MaiBot 数据迁移
├── 14-user-data.md                 用户数据与软件分离：数据根目录、更新、导出导入
├── 15-model-management.md          模型管理：服务商、模型、器官三层配置
├── 16-organ-architecture.md        器官架构草稿（早期设计，部分已被后来的决定取代）
├── 17-neuroscience-optimization.md 生物模型的设计依据（激素、精力等参数）
├── 18-neuroscience-completeness.md 神经科学对照（设计自评，未经实验验证）
├── 19-code-review.md               Phase 0 代码审查与后续修复记录
└── archive/                        已废弃的早期草稿，仅供对照
```

建议阅读顺序：01 → 02 → 12 → 08；想了解代码怎么组织，读 11。

## 参与

代码主要由 AI 编写，维护者负责项目构思、设计取舍和验收。欢迎提 issue 指出问题，包括设计上的漏洞。设计文档目前只有中文版。

## 许可证

采用 [AGPL-3.0](LICENSE)（GNU Affero 通用公共许可证第 3 版），见 [D12](docs/10-decisions.md)。

---
