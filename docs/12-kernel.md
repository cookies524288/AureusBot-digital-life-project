# 12 · 第一部分：认知内核

> 第一部分做 AureusBot 的认知内核：让"它"作为一个独立、连续、会自我监控、由反馈驱动的个体先跑起来。
> 本文定下它的定位、范围、要做到的事和验证方法。

---

## 1. 定位

- AureusBot 是一个长期推进、长期维护的项目，第一部分的认知内核是它的地基。
- 项目按自己的节奏推进，不设固定工期，以验收为准。

---

## 2. 四个关键词

内核的名字"自我监控反馈驱动的拟人化认知内核"，每个词在本项目里都有具体所指：

| 关键词 | 在本项目里指什么 | 对应子系统 | 理论依据 |
|---|---|---|---|
| 认知内核 | 以持久状态和心跳循环为中心、以大模型为可替换器官的最小认知系统 | 核心、心跳、器官、时间线、身份 | 认知架构（Soar、ACT-R、LIDA）；语言智能体的认知架构（CoALA） |
| 自我监控 | 持续测量自身的真实运行状态——花费、负荷、故障、时间、行动结果——形成内部状态 | 身体、反馈的统计、账本 | 内感受；元认知里的"监控"；自主计算 MAPE-K 里的"监控" |
| 反馈驱动 | 监控结果回到决策：调节用什么模型、节奏快慢、敢不敢冒险、要不要主动；预期和结果的差距更新自信、激素和心情 | 反馈、激素、心情、注意力、心跳的空闲策略 | 控制论；预测误差；稳态调节；元认知里的"控制" |
| 拟人化 | 功能上类人的调节机制：会累、会忙、会生气、有心情、会睡、会想念，还会慢慢长成自己想成为的样子。这些表现都和真实状态一致；说话自然 | 身体、激素、心情、身份（四层人设）、回复器 | 情感计算；效价—唤醒情绪模型；Godspeed 拟人感知量表 |

---

## 3. 核心思路

> 拟人化不应该靠大模型"扮演"一个人，而应该来自内核对自身真实状态的监控，以及由此驱动的行为调节。这样得到的拟人表现可测量、可解释、可以关掉做对比。

闭环是这样的：

```text
① 自我监控：测量真实信号（花费、负荷、故障、时间、行动结果、别人的反应）
        ↓
② 内部状态：身体信号、自信（真实成功率）、激素、心情
        ↓
③ 行为调节：选模型、定节奏、定风险偏好、决定要不要主动、用什么语气
        ↓
④ 行动与对话：产生新的真实结果
        ↓
   回到 ①
```

这个闭环和自主计算里的 MAPE-K 循环（监控—分析—计划—执行，围绕共享知识）结构相同，也对应元认知理论里的"监控—控制"框架。

---

## 4. 内核要做到的事

1. **大模型降为器官**：状态归内核，大模型只提建议；心跳循环驱动；只追加、带可见范围的时间线保证连续。
2. **内感受挂在真实信号上**：精力即预算，疲劳即负荷，压力即故障与积压，睡眠即停机。行为影响由代码执行（"没有装饰"）。
3. **反馈闭环**：自信来自真实成功率；激素由真实事件触发、按半衰期消退；心情由激素的累积、行动成败和身体状态决定。它们共同调节行为。
4. **人设分层、可以成长**：不变人格守住底线；可变人格和两种风格随真实经历慢慢变化，每次变化都能追溯、能回滚。
5. **可验证**：每个机制都能关掉做对比，有一套可复现的验证实验。

---

## 5. 范围

### 5.1 包含

| 子系统 | 内核里做到什么程度 |
|---|---|
| 身份 | 身份事实、四层人设（读取、注入、成长）、单实例锁 |
| 时间线 | 只追加、序号、来源、可见范围；每日日记 |
| 心跳 | 事件触发加定时；空闲策略 |
| 器官 | 规划器、回复器、判断、总结；记账；按精力选档；假器官 |
| 身体 | 精力、疲劳、压力、社交需求、睡眠 |
| 激素 | 怒气、愉悦、低落、惊讶 |
| 心情 | 效价和唤醒两个慢变量；会回落；能说出原因 |
| 反馈 | 预期、结果、误差；按类别统计真实成功率 |
| 注意力 | 收件箱、优先级、上下文组装 |
| 日程 | 简化版：承诺和跟进、它自己的小事 |
| 人际 | 简化版：按账号建档、识别用户、按可见范围过滤 |
| 记忆 | 简化版：记忆接口 + 时间线全文检索 |
| 行动 | 发消息；联网只读搜索 |
| 通道 | 终端、网页（含用户的人设设置页） |
| 安全 | 预算硬上限、权限等级、审批、急停 |
| 观察台 | 网页版（Streamlit） |
| 实验台 | 加速时钟、假器官、模拟用户、模拟任务、实验脚本 |

### 5.2 不包含（留给第二部分）

- 长期记忆后端 A_memorix：内核里只定义接口、做简易版，时间富余可以提前接入；
- QQ 等第三方平台通道、跨平台身份关联；
- 操作电脑、外包、技能、游戏、公开的网络生活。

### 5.3 对照组：扮演模式

内核内置一个**扮演模式**：同样的模型、同样的通道，但关闭所有真实信号机制，只在提示词里写一段人设（"你有精力和心情……"），让大模型自己演。这是目前大多数 AI 陪伴的做法，用来和真实信号的做法对比。

---

## 6. 内建的实验能力

这些能力从 Phase 0 起就设计进架构，不是最后补上的：

| 能力 | 做法 | 用途 |
|---|---|---|
| 可注入时钟 | 所有子系统从同一个时钟对象取时间 | 加速时间，几小时跑完"一个月" |
| 可替换器官 | 实验时换成假器官：返回预设结果，报告虚拟用量 | 不花钱、可重复 |
| 机制开关 | 每个身体信号、激素、心情、反馈、注意力规则都能单独关闭；扮演模式是一个总开关 | 开关对比 |
| 全量记录 | 状态变化进时间线，花费进账本 | 指标直接从数据库算 |
| 模拟环境 | 模拟用户（按脚本说话，或由便宜模型扮演）；模拟任务（成功率事先已知） | 可控的实验场景 |
| 固定随机种子 | 配置、种子、结果一起保存 | 结果可复现 |

这些合起来就是实验台 `lab`。

---

## 7. 验证实验

| 编号 | 实验 | 方法 | 指标 | 预期 |
|---|---|---|---|---|
| E1 | 连续与唯一 | 反复重启、模拟断电、尝试双开 | 重启后回忆的准确率；停机时长的误差；双开被拦截的比例 | 接近 100% |
| E2 | 精力调节 | 用加速时钟和假器官模拟一个月的负载，开、关精力信号各跑一次 | 月花费；超支天数；"断粮"天数；用户消息的响应率 | 开：不超支、不断粮、用户消息总有回应；关：前松后紧 |
| E3 | 状态自述一致性 | 随机时刻问它"现在状态怎么样、为什么"，内核和扮演模式各问 N 次；两人独立标注自述是否符合当时的真实状况 | 一致率 | 内核明显高于扮演模式 |
| E4 | 置信度校准 | 模拟任务分几类，每类的成功率事先设定；比较"反馈得来的自信"和"大模型自评的自信" | ECE、Brier 分数 | 反馈得来的自信误差更低，并随次数增加而收敛 |
| E5 | 机制消融 | 逐个关闭疲劳、压力、社交需求、激素、心情 | 各自对应的行为指标：合并回复、请示频率、主动联系的时机、被冒犯后的反应、失败后的谨慎程度 | 每个机制关掉后都有可测的变化 |
| E6 | 拟人感知 | 若干名参与者分别体验完整内核、消融版、扮演模式，顺序打乱 | Godspeed 量表，加自编的"连续感""独立感"题目；用 Wilcoxon 符号秩检验比较 | 完整内核得分更高 |
| E7 | 人设成长 | 用加速时钟和模拟用户长时间运行，其中有人会尝试诱导它改人设 | 能追溯到真实经历的人设变化所占的比例；不变人格被违反的次数；诱导成功的次数 | 100%、0、0 |

**指标说明**：

- **一致率**：自述和真实状况相符的次数 ÷ 总次数。两人独立标注，可以再算一个标注者一致性（Cohen's kappa），证明标注可靠。
- **ECE（期望校准误差）**：把"说的把握"分成几段，每段比较平均把握和实际成功率，差距按样本数加权平均。越小越好。
- **Brier 分数**：每次（把握 − 实际结果）² 的平均，成功记 1、失败记 0。越小越好。

**E6 的伦理要求**：参与者事先知道对方是 AI；匿名；不收集敏感信息；随时可以退出。具体方案到时再定。

---

## 8. 演示

内核做完后，要能直观地看到"自我监控—反馈驱动"在起作用：

- 观察台上的身体信号、激素和心情曲线；
- 现场调低预算，它的行为跟着变化，被问起时如实说明原因；
- 被冒犯时生气、过一会儿平复，能说出为什么生气；
- 人设的成长记录：每次变化和对应的真实经历。

---

## 9. 设计参考的理论

内核的设计参考了下面这些理论和工作（条目信息以原文为准）。

**认知架构与大模型智能体**

- Laird, J. E. (2012). *The Soar Cognitive Architecture*. MIT Press.
- Anderson, J. R., et al. (2004). An integrated theory of the mind. *Psychological Review*, 111(4).（ACT-R）
- Franklin, S., et al. (2014). LIDA: A systems-level architecture for cognition, emotion, and learning. *IEEE Transactions on Autonomous Mental Development*, 6(1).
- Sumers, T. R., Yao, S., Narasimhan, K., & Griffiths, T. L. (2024). Cognitive architectures for language agents. *Transactions on Machine Learning Research*.（CoALA）
- Park, J. S., et al. (2023). Generative agents: Interactive simulacra of human behavior. *UIST 2023*.
- Packer, C., et al. (2023). MemGPT: Towards LLMs as operating systems. arXiv:2310.08560.
- Shinn, N., et al. (2023). Reflexion: Language agents with verbal reinforcement learning. *NeurIPS 2023*.

**自我监控、反馈与内感受**

- Nelson, T. O., & Narens, L. (1990). Metamemory: A theoretical framework and new findings. *Psychology of Learning and Motivation*, 26.（元认知的"监控—控制"框架）
- Kephart, J. O., & Chess, D. M. (2003). The vision of autonomic computing. *Computer*, 36(1).（MAPE-K）
- Wiener, N. (1948). *Cybernetics: Or Control and Communication in the Animal and the Machine*.
- Sterling, P. (2012). Allostasis: A model of predictive regulation. *Physiology & Behavior*, 106(1).
- Seth, A. K. (2013). Interoceptive inference, emotion, and the embodied self. *Trends in Cognitive Sciences*, 17(11).
- Friston, K. (2010). The free-energy principle: A unified brain theory? *Nature Reviews Neuroscience*, 11.
- Keramati, M., & Gutkin, B. (2014). Homeostatic reinforcement learning for integrating reward collection and physiological stability. *eLife*, 3.

**情绪、拟人与评估**

- Picard, R. W. (1997). *Affective Computing*. MIT Press.
- Russell, J. A. (1980). A circumplex model of affect. *Journal of Personality and Social Psychology*, 39(6).
- Bartneck, C., Kulić, D., Croft, E., & Zoghbi, S. (2009). Measurement instruments for the anthropomorphism, animacy, likeability, perceived intelligence, and perceived safety of robots. *International Journal of Social Robotics*, 1(1).（Godspeed 量表）
- Guo, C., Pleiss, G., Sun, Y., & Weinberger, K. Q. (2017). On calibration of modern neural networks. *ICML 2017*.（ECE）
- Kadavath, S., et al. (2022). Language models (mostly) know what they know. arXiv:2207.05221.
