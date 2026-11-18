# 02 · 模块

> 每个模块的职责、边界、接口与失败模式。

---

## 模块总表

| 模块 | 层 | 职责 | 状态 |
|---|---|---|---|
| `adapter` | Sensory | 外部事件归一化 | 有状态（连接） |
| `mirror_self` | Core | 自我模型 | 有状态（持久） |
| `mind_dome` | Core | 全局工作空间 | 有状态（每轮重置） |
| `soma` | Core | 内感受与情绪 | 有状态（持久） |
| `spotlight` | Core | 显著性竞争 | 无状态 |
| `executive` | Core | 执行控制 | 有状态（当前意图） |
| `organ_bus` | Core | 器官调度 | 无状态 |
| `prediction_loop` | Core | 预测闭环 | 有状态（待验证预测） |
| `hippo` | Core | 记忆 | 有状态（持久） |
| `thread` | Core | 叙事 | 有状态（持久） |
| `warden` | 横切 | 治理 | 有状态（审计） |
| `effector` | Actuator | 行动执行 | 有状态（连接） |

---

## 1. adapter

### 职责

把外部世界的事件转成内部 `Percept`。

### 接口

```python
class Adapter(Protocol):
    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    def emit(self) -> AsyncIterator[Percept]: ...
```

### 边界

```text
不做判断
不做记忆
不做行动
只归一化
```

### 失败模式

```text
连接断开 → 重连，不影响 Core
消息格式异常 → 丢弃并记录
重复消息 → 按 id 去重
```

---

## 2. mirror_self

### 职责

持有并维护自我模型。

### 结构

```python
@dataclass
class MirrorSelf:
    # 不可变层
    immutable: ImmutableSelf
    # 可变层
    mutable: MutableSelf
    # 版本
    revision: int

@dataclass(frozen=True)
class ImmutableSelf:
    name: str
    self_reference: str
    core_values: tuple[str, ...]
    boundaries: tuple[str, ...]
    identity_invariants: tuple[str, ...]

@dataclass
class MutableSelf:
    current_goal: str | None
    current_focus: list[str]
    confidence: float
    capability_map: dict[str, float]
    relationships: dict[str, Relation]
    narrative_position: str
    last_updated: float
```

### 接口

```python
class MirrorSelfService:
    def snapshot(self) -> MirrorSelf: ...
    def update_mutable(self, patch: MutablePatch, reason: str) -> None: ...
    def assert_invariant(self, key: str) -> None: ...
```

### 边界

```text
不可变层只能由人类/配置初始化
可变层写入必须带 reason
每次写入记录 diff
```

### 失败模式

```text
写入冲突 → 版本号乐观锁
非法字段 → 拒绝并告警
不可变层被改 → 拒绝 + 审计 + 告警
```

---

## 3. mind_dome

### 职责

容量有限的当前意识内容。

### 结构

```python
@dataclass
class Slot:
    content: Content
    salience: float
    source: str
    entered_at: float
    ttl: float

@dataclass
class MindDome:
    capacity: int
    slots: list[Slot]
    broadcast_log: list[Broadcast]
```

### 接口

```python
class MindDomeService:
    def compete(self, candidates: list[Candidate]) -> list[Slot]: ...
    def broadcast(self) -> Broadcast: ...
    def clear(self) -> None: ...
```

### 边界

```text
容量固定
每轮重新竞争
不持久化（可选快照用于调试）
```

### 失败模式

```text
候选过多 → 按评分截断
候选为空 → 空广播（合法状态）
```

---

## 4. soma

### 职责

内感受与情绪状态。

### 结构

```python
@dataclass
class SomaState:
    energy: float          # 0-100
    load: float            # 0-100
    fatigue: float         # 0-100
    stress: float          # 0-100
    social_need: float     # 0-100
    arousal: float         # 0-1
    valence: float         # -1..1
    updated_at: float
```

### 接口

```python
class SomaService:
    def snapshot(self) -> SomaState: ...
    def apply(self, event: SomaEvent) -> None: ...
    def decay(self, dt: float) -> None: ...
```

### 边界

```text
全部有上下限
按时间衰减
单次事件影响有上限
```

### 失败模式

```text
越界 → clamp
异常事件 → 忽略并记录
```

---

## 5. spotlight

### 职责

为候选内容计算显著性。

### 接口

```python
class Spotlight:
    def score(self, candidate: Candidate, ctx: SalienceContext) -> float: ...
    def select(self, candidates: list[Candidate], k: int) -> list[Candidate]: ...
```

### 评分因子

```text
novelty          新颖性
goal_relevance   目标相关性
affect_intensity 情绪强度
social_signal    社会信号
prediction_error 预测误差
self_relevance   自我相关性
```

### 边界

```text
无状态
可替换实现
必须有探索噪声
```

---

## 6. executive

### 职责

生成候选行动、预测、选择、抑制。

### 接口

```python
class Executive:
    async def decide(self, frame: CognitiveFrame) -> Decision: ...

@dataclass
class Decision:
    chosen: Intent
    rejected: list[Intent]
    expectations: dict[str, Expectation]
```

### 边界

```text
不直接执行
不直接写记忆
必须记录 expectation
```

### 失败模式

```text
无候选 → 选择"不行动"
全部低置信 → 选择"等待"或"询问"
超时 → 降级为保守动作
```

---

## 7. organ_bus

### 职责

器官注册、调度、超时、降级。

### 接口

```python
class Organ(Protocol):
    name: str
    async def invoke(self, req: OrganRequest) -> OrganResponse: ...

class OrganBus:
    def register(self, organ: Organ) -> None: ...
    async def call(self, name: str, req: OrganRequest) -> OrganResponse: ...
    async def parallel(self, calls: list[OrganCall]) -> list[OrganResponse]: ...
```

### 器官清单（MVP）

| 器官 | 职责 | 输出 |
|---|---|---|
| `language` | 理解与生成自然语言 | 表达候选 |
| `planner` | 生成行动方案 | 计划候选 |
| `memory` | 检索与概括 | 记忆片段 |
| `critic` | 评估质量与风险 | 评分与理由 |
| `social` | 推测他人意图 | 心理模型 |

### 边界

```text
器官无状态
器官不互相调用
器官不能改身份
器官必须声明置信度
```

### 失败模式

```text
超时 → 返回降级结果
异常 → 记录并标记不可用
连续失败 → 熔断
```

---

## 8. prediction_loop

### 职责

记录预测、采集结果、计算误差、驱动更新。

### 接口

```python
class PredictionLoop:
    def expect(self, intent: Intent) -> str: ...      # 返回 prediction_id
    def observe(self, prediction_id: str, outcome: Outcome) -> None: ...
    def reconcile(self) -> list[Update]: ...
```

### 更新目标

```text
world_model     世界模型
self_model      能力边界与置信度
soma            情绪
skills          技能有效性
hippo           带误差标记的记忆
```

### 边界

```text
没有预测的行动不执行
没有结果的预测标记为 unknown
误差必须归因
```

---

## 9. hippo

### 职责

长期记忆的写入、检索、巩固。

### 记忆类型

```text
episodic    情景
semantic    语义
relational  关系
procedural  技能
narrative   叙事
```

### 接口

```python
class Hippo:
    async def write(self, record: MemoryRecord) -> str: ...
    async def search(self, query: MemoryQuery) -> list[MemoryRecord]: ...
    async def consolidate(self, window: TimeWindow) -> None: ...
```

### 边界

```text
写入必须带来源
检索必须带时间与置信度
巩固不可删除原始记录
```

---

## 10. thread

### 职责

长期叙事。

### 接口

```python
class Thread:
    def append(self, event: NarrativeEvent) -> None: ...
    def summarize(self, period: Period) -> NarrativeSummary: ...
    def current_position(self) -> str: ...
```

### 边界

```text
叙事不是聊天记录
按周期聚合
可被 MirrorSelf 引用
```

---

## 11. warden

### 职责

治理横切。

### 能力

```text
身份不变量保护
权限检查
危险动作审批
写入审计
回滚
熔断
```

### 接口

```python
class Warden:
    def guard_write(self, target: str, patch: Any) -> None: ...
    async def approve(self, action: Action) -> ApprovalResult: ...
    def audit(self, event: AuditEvent) -> None: ...
    def rollback(self, revision: int) -> None: ...
```

### 边界

```text
失败朝安全方向倒
所有高危动作必须经过
不可绕过
```

---

## 12. effector

### 职责

执行 Intent。

### 接口

```python
class Effector(Protocol):
    async def execute(self, intent: Intent) -> Outcome: ...
    async def dry_run(self, intent: Intent) -> Outcome: ...
```

### 边界

```text
不决策
不重试（由 Executive 决定）
必须返回结构化 Outcome
```

---

## 模块依赖

```text
adapter → percept → core
core → intent → effector
effector → outcome → prediction_loop
prediction_loop → update → mirror_self / soma / hippo
hippo → recall → spotlight
mirror_self → frame → executive
warden ← 所有写入
```

禁止反向依赖。
