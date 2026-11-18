# 05 · 路线图

> 分阶段交付。每个阶段都有明确的验收标准，不通过不进入下一阶段。

---

## 总览

```text
Phase 0   骨架            2 周
Phase 1   自我模型        3 周
Phase 2   工作空间        3 周
Phase 3   器官            4 周
Phase 4   闭环            4 周
Phase 5   记忆与叙事      4 周
Phase 6   治理            3 周
Phase 7   评估与消融      4 周
Phase 8   封装与文档      4 周
```

单人 + AI 辅助的估算。实际取决于投入时间。

---

## Phase 0 · 骨架

### 目标

跑通最小事件循环。

### 交付

```text
事件总线
Percept / Event / Intent / Outcome 数据结构
一个 echo 器官
一个假 adapter
一个假 effector
日志与快照
```

### 验收

```text
投递一个 Percept
→ 生成一个 Intent
→ 执行
→ 产生 Outcome
→ 写入 JSONL
```

### 不做

```text
不做自我模型
不做工作空间
不做记忆
不做治理
```

---

## Phase 1 · 自我模型

### 目标

身份成为状态。

### 交付

```text
MirrorSelf schema
不可变层 / 可变层
版本与乐观锁
写入守卫（最小版）
快照与恢复
```

### 验收

```text
重启后自我模型完全恢复
不可变层写入被拒绝
可变层写入有 diff 与 reason
```

---

## Phase 2 · 工作空间

### 目标

容量有限的注意力。

### 交付

```text
Candidate
Spotlight 评分
MindDome 竞争与广播
TTL 与衰减
探索噪声
```

### 验收

```text
给定 20 个候选，只有 K 个进入工作空间
评分可解释
重复运行结果稳定（固定种子）
```

---

## Phase 3 · 器官

### 目标

LLM 成为器官。

### 交付

```text
OrganBus
OrganRequest / OrganResponse
5 个器官：language / planner / memory / critic / social
超时、降级、熔断
置信度
```

### 验收

```text
器官只能看到 CognitiveFrame
器官不能改自我模型
单个器官失败不影响整轮
```

---

## Phase 4 · 闭环

### 目标

行动产生学习。

### 交付

```text
Expectation 记录
Outcome 采集
PredictionError 计算
回流更新：self / soma / skills
```

### 验收

```text
每次行动都有 expectation
每次结果都有 error
连续失败后置信度下降
连续成功后置信度上升
```

---

## Phase 5 · 记忆与叙事

### 目标

连续的经历。

### 交付

```text
Hippo 写入与检索
记忆类型
巩固
Thread 叙事
```

### 验收

```text
关键经历能被正确召回
叙事按天聚合
召回带来源与时间
```

---

## Phase 6 · 治理

### 目标

安全边界。

### 交付

```text
Warden 完整版
权限
审批
审计
回滚
熔断
```

### 验收

```text
高危动作被拦截
不可逆动作需 dry-run
所有写入可审计
可回滚到任意快照
```

---

## Phase 7 · 评估与消融

### 目标

证明模块不是装饰。

### 交付

```text
评估脚本
指标计算
消融开关
报告生成
```

### 验收

```text
去掉 Self-Model → 可测退化
去掉 Workspace → 可测退化
去掉闭环 → 可测退化
```

---

## Phase 8 · 封装与文档

### 目标

可被他人使用。

### 交付

```text
安装脚本
配置模板
示例人格
接入文档
API 文档
```

### 验收

```text
新人按文档可在 30 分钟内跑起来
示例可复现
```

---

## MVP 定义

Phase 0–4 完成即 MVP。

```text
有自我模型
有工作空间
有器官
有闭环
```

不包含记忆、叙事、治理、评估。

---

## 风险与缓冲

```text
每个 Phase 预留 30% 缓冲
Phase 3 与 Phase 4 最容易超时
Phase 7 常被低估
```

---

## 里程碑

```text
M1  Phase 0 完成    骨架可跑
M2  Phase 2 完成    注意力可跑
M3  Phase 4 完成    MVP
M4  Phase 6 完成    可安全运行
M5  Phase 8 完成    可分发
```

---

## 不做的事

```text
不在 Phase 0 做架构美化
不在 MVP 做 GUI
不在 MVP 做多平台
不在 MVP 做自我改进
不在 MVP 做分布式
```
