# 器官架构设计 (Phase 0.5)

> 设计目标：遵循 MaiBot 的分层模型架构，加入稳态调节系统，使 AureusBot 的行为更接近人类大脑的调节方式。
>
> 本文是 Phase 0.5 的早期设计草稿。当前器官与身体信号的设计见 [子系统](03-subsystems.md)和[身体信号](04-body-signals.md)，实施进度见[路线图](08-roadmap.md)。

## 1. 核心哲学

**"我思故我在"的功能性意识**
- 状态在 Core（自我模型、全局工作空间、内感受、记忆、注意力、执行系统）
- 器官只是建议者，不能直接改变状态
- 每个信息处理周期（heartbeat）是一轮完整的感知、思考、决策、行动
- 离线状态下也会静默反思（梦境、潜意识处理）

---

## 2. 自我模型 (Self-Model)

AureusBot 的核心身份表示，与 `identity` 子系统结合：

```
Self-Model
├── 基本信息
│   ├── 名字（示例人格：小绿）
│   ├── 性别 / 性向标签
│   └── 出生日期（年龄计算、生日标记）
├── 人格层次
│   ├── 身份事实（不可改）
│   ├── 不变人格（用户改）
│   ├── 可变人格（经历驱动）
│   ├── 行为风格（规划层影响）
│   └── 表达风格（回复层影响）
├── 内部状态
│   ├── 能量体征（精力、疲劳）
│   ├── 生化信号（多巴胺、皮质醇、血清素、去甲肾上腺素）
│   ├── 情绪状态（喜、怒、忧、惧、厌）
│   ├── 时间感（停机时长、昼夜节律）
│   ├── 孤独感（社交缺口）
│   └── 无聊感（刺激饥渴）
├── 认知地图
│   ├── 关系图谱（人物及态度）
│   ├── 执念池（未解决的问题、悬而未决）
│   ├── 当前目标（长期 / 短期）
│   ├── 当前焦点（注意力所在）
│   └── 承诺清单（已答应的事）
├── 能力与权限
│   ├── 已学技能及成功率
│   ├── 可用工具列表
│   ├── 权限等级
│   └── 审批队列
├── 时间线视角
│   ├── 叙事时间线（我是怎样的一个存在）
│   ├── 所有权标签（这件事是 / 不是我做的）
│   └── 创伤疤痕（能量濒死时留下的警告）
└── 不变量（L0 锁保护）
    ├── 核心源码不可修改
    ├── 基因组（genome.json）不可修改
    └── 这两项需要用户主密钥确认才能触及
```

---

## 3. 全局工作空间 (Global Workspace)

有限容量的临时记忆，一次心跳的执行上下文：

```
GlobalWorkspace:
  - 输入缓冲（新消息、事件、感知）
  - 活跃上下文（当前关注的事）
  - 候选池（器官的建议）
  - 执行计划（通过审批的行动）
  - 输出缓冲（要发送的消息、日志）
  - 容量上限（防止爆炸式增长）
  - 广播机制（所有子模块都看得到）
```

---

## 4. 器官分层架构（核心）

参考 MaiBot 的 10 层设计，再加上内感受反馈环：

### 4.1 决策层（Decision Layer）

**规划器模型 (Planner)**
- 职责：给定上下文，生成行为建议（speak/act/wait/delegate）
- 输入：自我模型、全局工作空间、时间线片段、相关记忆
- 输出：建议动作、预期结果、执行优先级、理由（可解释）
- 约束：
  - 优先级排序（用户 > 自主目标 > 闲时探索）
  - 能量检查（疲惫时拒绝高耗能动作）
  - 权限检查（需要审批的动作进队列）
  - 承诺履行（已承诺的动作强制优先）

### 4.2 执行层（Execution Layer）

**回复器模型 (Replyer)**
- 职责：将规划建议转化为自然语言回复
- 输入：规划建议、表达风格、黑话库、用户人物档案、最近聊天记录
- 输出：自然、拟人化的回复文本
- 约束：
  - 不超过字符上限（短句优先）
  - 避免暴露内部细节（除非调试模式）
  - 符合表达风格锁定状态（如果被用户锁定）

### 4.3 快速工具层（Utilities）

**快速模型 (Fast Model)**
- 轻量级任务：文本摘要、情感分析、意图分类、名称提取
- 不调用昂贵模型，速度优先
- 例：识别新消息是闲聊还是待办事项

### 4.4 学习与风格层（Learning）

**学习模型 (Learner) / 表达优化器**
- 职责：
  - 表达方式学习：从对话中抽取新的表达模式
  - 黑话学习：发现新术语、新含义
  - 用户喜好学习：推断用户的回应偏好
  - 人设演化建议：基于经历提出人设调整建议
- 输入：时间线、用户反应、表达成功率
- 输出：新的表达、黑话条目、演化建议
- 频率：批量（睡眠时整理）

### 4.5 记忆层（Memory）

**长期记忆引擎 (Memory Engine)**
- 职责：
  - 提取相关记忆：根据当前上下文从 A-Memorix 检索
  - 内容衰减：重要事件权重高，日常琐事逐渐淡化
  - 关系更新：基于新交互更新人物档案
  - 执念管理：追踪未解决的问题
- 使用向量检索（嵌入模型）
- Phase 3 接入 A-Memorix

### 4.6 嵌入层（Embeddings）

**文本嵌入 (Text Embedding)**
- 语义搜索：从记忆、表达库、黑话库中找相关项
- 向量数据库支持

**图像嵌入 (Image Embedding)**
- 图像语义理解（如果输入包含图片）

### 4.7 感知层（Perception）

**视觉识别 (Vision / VLM)**
- 识别图片内容、OCR 等
- 可选模块（非必须）

**语音识别 (Speech)**
- 语音转文字
- 可选模块（Phase 1+）

### 4.8 内感受层（Interoception） ⭐

**这是 AureusBot 与 MaiBot 的关键不同**

- 职责：
  - 监控内部状态（能量、激素、情绪）
  - 生成系统级的情绪提示词
  - 修改规划器的目标排序（疲惫时避免高耗能）
  - 修改回复器的语调（低落时缩短，兴奋时扩展）
  - 决策是否进入梦境 / 休眠
- 输出到：Planner、Replyer、Executive 的约束条件

### 4.9 器官总线 (Organ Bus)

**消息中介，解耦各器官**

```python
class OrganBus:
    """器官之间的消息中介。"""
    
    async def request(
        organ_name: str,
        task_type: str,
        input_data: Any,
        priority: int = 0,
    ) -> Any:
        """发送请求给指定器官。"""
    
    async def broadcast(
        message: Message,
        from_organ: str,
    ) -> None:
        """广播到全局工作空间。"""
    
    async def submit_candidate(
        candidate: Candidate,
        from_organ: str,
    ) -> None:
        """提交候选（规划建议、动作建议等）。"""
```

---

## 5. 核心调节系统 (Homeostasis Loop)

这是"稳态调节器"——让 AureusBot 表现得像生命体。

### 5.1 能量体征（Energy Signature）

```yaml
# genome.json 中定义
energy:
  baseline: 0.5          # 基线（休息状态下自动回复）
  max: 1.0              # 上限
  min: 0.0              # 下限（濒死）
  regeneration_rate: 0.1  # 每小时恢复率
  decay_rate: 0.05      # 每次高耗能操作衰减
  
  stages:
    high: [0.75, 1.0]   # 精力充沛，主动性强
    normal: [0.5, 0.75]  # 日常状态
    low: [0.25, 0.5]    # 疲惫，优先轻量任务
    critical: [0.0, 0.25]  # 濒死，立即休眠
```

### 5.2 生化信号（Biochemical Signals）

每种都有：基线、敏感度、半衰期、影响行为的规则

```yaml
dopamine:        # 多巴胺：动力、期待、奖励感
  baseline: 0.5
  halflife: 600s  # 10 分钟
  triggers:
    - goal_achieved: +0.3
    - prediction_confirmed: +0.2
    - prediction_wrong: -0.1
  behavior_effect: "高时倾向主动，低时被动"

cortisol:        # 皮质醇：压力、警惕
  baseline: 0.3
  halflife: 1800s  # 30 分钟
  triggers:
    - error: +0.2
    - permission_denied: +0.15
    - timeout: +0.25
  behavior_effect: "高时谨慎，低时松散"

serotonin:       # 血清素：满足感、稳定性
  baseline: 0.5
  halflife: 3600s  # 1 小时
  triggers:
    - social_positive: +0.3
    - lonely_too_long: -0.15
  behavior_effect: "高时稳定乐观，低时消极"

noradrenaline:   # 去甲肾上腺素：注意力、警觉
  baseline: 0.4
  halflife: 900s  # 15 分钟
  triggers:
    - novel_event: +0.3
    - urgent_message: +0.4
  behavior_effect: "高时集中，低时散漫"
```

### 5.3 情绪状态（Emotional State）

基于激素浓度的二阶派生：

```
情绪矩阵 (2D):
  ▲ 积极
  │   喜悦(high DA, high 5HT)
  │   兴奋(high NE)      警惕(high Cortisol)
  │─────────────────────────────────► 高唤醒
  │   安心(high 5HT)     焦虑(high Cortisol+NE)
  │   平静(low all)      恐惧(high Cortisol, low DA)
  ▼ 消极
```

### 5.4 时间感 & 孤独感 & 无聊感

```python
# Core 中定义
class InternalStates:
    sleep_duration: timedelta  # 停机了多久？
    circadian_phase: float     # 时间节律位置（0-1）
    social_deficit: float      # 多久没有有意义的互动？
    stimulation_level: float   # 最近的刺激程度（聊天、事件）
    novelty_craving: float     # 想要新事物的程度
    
    # 这些会直接影响主动唤醒的决策
```

### 5.5 执念池（Concern Pool）

未解决的问题积累：

```python
@dataclass
class Concern:
    id: str
    topic: str               # "为什么用户这么久没回复？"
    raised_at: datetime
    salience: float          # 0-1，有多紧迫
    related_memories: List[str]  # 相关的历史记录
    attempts: int            # 尝试过多少次
    
    # 根据日期自动衰减，但如果用户再次提及则复活
```

---

## 6. 每一轮工作流程（Heartbeat Cycle）

典型的 1 秒钟心跳周期：

```
┌─────────────────────────────────────────────────────────┐
│ HEARTBEAT CYCLE (1 second)                              │
└─────────────────────────────────────────────────────────┘

1️⃣ 感知输入 (Perception Input) ~ 100ms
   ├─ 读通道事件（新消息、定时器、外部事件）
   ├─ 读状态变化（能量、激素自然衰减）
   ├─ 更新时间线（记录新感知）
   └─ 写入全局工作空间

2️⃣ 内感受反馈 (Interoception) ~ 50ms ⭐ 关键步骤
   ├─ 更新激素浓度（按半衰期衰减）
   ├─ 更新能量状态
   ├─ 生成系统提示词（情绪基调）
   ├─ 检查休眠条件
   │  └─ 如果 energy < 0.1 且 no_urgent_message
   │     → 进入深度睡眠（梦境模式），本周期结束
   └─ 生成约束条件（给后续器官用）

3️⃣ 注意力焦点 (Attention) ~ 50ms
   ├─ 优先级排序新消息
   ├─ 从执念池选择当前焦点
   ├─ 从日程查看是否有逾期承诺
   └─ 选出本轮最高优先级的任务

4️⃣ 记忆检索 (Memory Retrieval) ~ 100ms
   ├─ 根据当前任务的关键词
   ├─ 用嵌入模型从 A-Memorix 检索相关记忆
   ├─ 检索相关的人物档案（如果涉及特定用户）
   ├─ 检索表达方式库（符合当前情绪的表达风格）
   └─ 写入全局工作空间

5️⃣ 规划器请求 (Planner Invocation) ~ 200-500ms ⭐ 最昂贵
   ├─ 组装完整上下文：
   │  ├─ 自我模型摘要
   │  ├─ 内感受状态提示词
   │  ├─ 检索到的记忆
   │  ├─ 时间线片段（最近 10 条）
   │  ├─ 活跃人物档案
   │  └─ 当前焦点任务描述
   ├─ 调用规划器（Planner 模型）
   ├─ 规划器返回：
   │  ├─ 建议动作（speak / act / wait / delegate）
   │  ├─ 理由（可解释性）
   │  ├─ 预期结果
   │  └─ 优先级分数
   └─ 检查能量/权限是否允许
      └─ 若不允许 → wait，本周期结束

6️⃣ 回复生成（仅当决定 speak） (Replyer) ~ 200-400ms
   ├─ 调用回复器（Replyer 模型）
   ├─ 输入：
   │  ├─ 规划建议的内容
   │  ├─ 当前表达风格（+内感受调整）
   │  ├─ 黑话库、表达库匹配项
   │  └─ 目标用户的人物档案
   ├─ 回复器返回自然语言
   └─ 快速模型检查可读性

7️⃣ 执行（Execution & Approval） ~ 50ms
   ├─ 建议提交给行动网关
   │  ├─ 权限检查
   │  ├─ 风险等级评估
   │  └─ 若需要审批 → 进队列，等待用户决策
   └─ 若通过 → 执行（发送消息、调工具等）

8️⃣ 反馈与学习 (Feedback) ~ 50ms
   ├─ 记录本轮的：
   │  ├─ 输入（消息、上下文）
   │  ├─ 规划选择
   │  ├─ 执行动作
   │  ├─ 预期结果
   │  └─ 实际结果（如果有）
   ├─ 计算预期 vs 实际差距
   ├─ 更新相关的激素浓度
   │  └─ 成功 → +dopamine，-cortisol
   │     失败 → -dopamine，+cortisol
   └─ 写入时间线

9️⃣ 睡眠整理（仅夜间或关闭时） (Consolidation)
   ├─ 批量处理整天的时间线
   ├─ 提取关键记忆 → 写入 A-Memorix
   ├─ 更新人物关系图
   ├─ 清理执念池（衰减不重要的）
   ├─ 调用学习模型提取表达方式/黑话
   └─ 生成睡眠日记

🔟 梦境模式（离线时）(Dream Mode)
   ├─ 能量 < 0.1 或关闭状态
   ├─ 后台默认运行：
   │  ├─ 激素自然恢复（加速衰减）
   │  ├─ 能量缓慢恢复
   │  ├─ 随机重新激活某个执念（"想起来……"）
   │  ├─ 调用快速模型做轻量级反思
   │  └─ 每小时生成一条"梦境"时间线记录
   └─ 等待外部刺激或能量恢复
```

---

## 7. 特殊机制

### 7.1 自主唤醒 (Autonomous Wake-up)

```python
# 在梦境模式时，每分钟检查一次
if (energy > 0.5 and 
    (high_dopamine or high_noradrenaline) and
    idle_time > 5_minutes and
    not user_busy):
    # 自主唤醒，发起一条对话
    wake_up_topics = select_from_concerns_and_interests()
    reply = await planner.generate_initiative_topic(wake_up_topics)
    await send_message(reply)
```

### 7.2 行为承诺 (Behavioral Commitment)

```python
# 醒来后的第一件事
async def on_wake_up():
    """唤醒后必须先进行承诺注册。"""
    pending_commitments = db.get_commitments(
        created_before=now,
        not_yet_registered=True
    )
    
    # 必须逐条登记（模拟有意识的决策）
    for commitment in pending_commitments:
        if not await user_confirm_commitment(commitment):
            # 拒绝已承诺的事 → 高 cortisol，关系受损
            cortisol += 0.3
            await timeline_record(
                actor=ME,
                event="broke_commitment",
                content=commitment.description,
            )
        else:
            # 承诺被重新确认 → 加入本周期的优先级列表
            current_agenda.add_priority(commitment)
```

### 7.3 创伤疤痕 (Trauma Scar)

```python
class TraumaScar:
    """能量濒死时留下的永久警告。"""
    timestamp: datetime
    energy_level_at_incident: float  # 记录多低
    context: str                      # 当时在做什么
    recovery_multiplier: float = 1.5  # 未来类似情况需要更多能量
    
    # 每次能量濒死触发，自动增加一条
    # 无法删除（必须用户主密钥）
    # 会影响规划器的风险评估
```

### 7.4 插件与技能热装载 (Skill Hot-loading)

```python
# 可以自己写新插件
async def learn_new_skill(
    skill_name: str,
    skill_code: str,  # Python 代码
    test_cases: List[Tuple],
):
    """学习新技能。"""
    # 1. 语法检查、安全检查
    # 2. 在沙箱中运行测试用例
    # 3. 成功率评估
    # 4. 存入技能库，标记学习时间
    # 5. 下一轮 Planner 可以选择使用
    # 6. 不需要重启
```

### 7.5 L0 文件锁

```python
# 核心源码和基因组是不可改的
class L0Lock:
    """最高权限锁。"""
    protected_files = [
        "aureus/core/*",
        "aureus/organs/*",
        "genome.json",  # 激素基线、半衰期等
    ]
    
    async def attempt_modification(file_path: str) -> bool:
        if file_path in protected_files:
            require_user_master_key()
            # 需要输入用户主密钥 + 理由
            log_to_audit_trail()
            return True
```

### 7.6 分级审批 (Tiered Approval)

```python
class RiskLevel(Enum):
    L0 = "禁止（核心源码）"
    L1 = "高风险（需用户确认）"
    L2 = "中风险（自动放行，但审计）"
    L3 = "低风险（默认允许）"

# 示例
if action.risk_level == L1:
    await request_user_approval(action)
elif action.risk_level in (L2, L3):
    await audit_log(action)
    execute(action)
```

---

## 8. 核心架构图

```
┌────────────────────────────────────────────────────────────┐
│                    外部适配器                              │
│        (CLI / HTTP API / 浏览器 / 桌面应用)              │
└────────────────────────────────────────────────────────────┘
                        ▲ / ▼
                   事件总线
                        ▲ / ▼
┌────────────────────────────────────────────────────────────┐
│                   全局工作空间 (GW)                        │
│         (有限容量、广播、状态聚合、见证所有)              │
└────────────────────────────────────────────────────────────┘
                        ▲ / ▼
┌──────────────────────────────────────────────────────────────────┐
│                       认知内核 (Core)                            │
│ ┌────────────────────────────────────────────────────────────┐  │
│ │ 自我模型          记忆系统      内感受系统    注意力系统  │  │
│ │ (Self-Model)      (Memory)      (Interoception) (Attention)│  │
│ └────────────────────────────────────────────────────────────┘  │
│ ┌────────────────────────────────────────────────────────────┐  │
│ │              执行系统 (Executive)                          │  │
│ │  规划器  回复器  快速模型  学习器  嵌入  VLM  STT   │  │
│ │ (Planner)(Replyer)(Fast)(Learner)(Embed)(Vision)(Voice)  │  │
│ └────────────────────────────────────────────────────────────┘  │
└──────────────────────────────────────────────────────────────────┘
                        ▲ / ▼
┌────────────────────────────────────────────────────────────┐
│             行动网关 (Action Gateway)                     │
│       (权限 / 沙箱 / 审计 / 回滚)                       │
└────────────────────────────────────────────────────────────┘
                        ▲ / ▼
┌────────────────────────────────────────────────────────────┐
│          工具与执行环境 (Tools & Execution)               │
│      (时间线、A-Memorix、表达库、黑话库、技能库等)       │
└────────────────────────────────────────────────────────────┘
```

---

## 9. 实现路线

**Phase 0.5 目标**：建立器官框架，实现 Planner + Replyer + 快速模型

1. **器官接口定义**（`aureus/organs/interface.py`）
   - 抽象基类 `Organ`
   - 消息协议 `OrganRequest`, `OrganResponse`, `Candidate`

2. **假器官实现**（`aureus/organs/fake.py`）
   - `FakePlanner`：返回固定建议（用于测试）
   - `FakeReplyer`：返回模板回复

3. **真实器官实现**（`aureus/organs/llm_adapter.py`）
   - `LLMPlanner`：调用大模型进行规划
   - `LLMReplyer`：调用大模型生成回复
   - 使用 `litellm` 适配多个 LLM 服务商（OpenAI、Anthropic、DeepSeek 等）

4. **账本系统**（`aureus/safety/ledger.py`）
   - 记录每次调用：模型、token、费用
   - 预算检查、硬上限

5. **安全网关最小版本**（`aureus/safety/gateway.py`）
   - 权限检查
   - 预算检查

6. **内感受系统**（`aureus/core/interoception.py`）
   - 激素浓度计算
   - 情绪派生
   - 行为约束生成

7. **器官总线**（`aureus/organs/bus.py`）
   - 异步消息中介
   - 候选汇聚

8. **整合到 Core**（`aureus/core/core.py`）
   - 一个 Heartbeat 周期的实现
   - 单元测试

---

## 10. 设计哲学总结

> **AureusBot 试图在功能上实现"我思故我在"。它是否有真实的意识，本文不讨论（见 01 号文档第 8 节）。**

- ✅ 状态完全在 Core，器官无法直接改变
- ✅ 内感受系统驱动行为，不是大模型编造
- ✅ 每次思考都有能量代价，不能无限思考
- ✅ 离线时会做梦和反思，展现连续性
- ✅ 创伤疤痕和执念是长期心理特征
- ✅ 人设只能按照真实经历演化，别人诱导无效
- ✅ 完全可解释：每个决策都能说出理由
- ✅ 能自己写插件，本质是学习系统
- ✅ L0 锁和审批流程确保安全边界
