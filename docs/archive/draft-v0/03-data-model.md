# 03 · 数据模型

> 核心数据结构。所有字段都是 schema 的一部分，变更需要版本迁移。

---

## 1. Percept

```python
@dataclass(frozen=True)
class Percept:
    id: str
    source: str            # "qq:group:123", "file", "timer", "tool"
    kind: str              # "message", "event", "result", "signal"
    timestamp: float
    payload: dict
    trust: float           # 0..1
    self_reference: bool   # 是否指向自己
    session_id: str | None
```

---

## 2. Candidate

```python
@dataclass
class Candidate:
    id: str
    content: str
    source: str
    kind: str              # "percept", "memory", "interoception", "goal", "self"
    base_salience: float
    metadata: dict
```

---

## 3. Slot

```python
@dataclass
class Slot:
    content: str
    kind: str
    source: str
    salience: float
    entered_at: float
    ttl: float
    ref_id: str | None
```

---

## 4. CognitiveFrame

Core 每轮组装、广播给器官的完整上下文。

```python
@dataclass
class CognitiveFrame:
    frame_id: str
    timestamp: float

    # 自我
    self_snapshot: dict
    # 当前意识内容
    workspace: list[Slot]
    # 内感受
    soma: dict
    # 当前目标
    goals: list[str]
    # 相关记忆
    memories: list[dict]
    # 可用器官
    organs: list[str]
    # 约束
    constraints: list[str]
```

**关键**：这是器官唯一的输入上下文。器官不能自行检索额外状态。

---

## 5. Intent

```python
@dataclass
class Intent:
    id: str
    goal: str
    action: str
    args: dict
    expectation: str
    confidence: float
    owner: str = "self"
    created_at: float
```

---

## 6. Expectation

```python
@dataclass
class Expectation:
    id: str
    intent_id: str
    description: str
    predicted_outcome: dict
    confidence: float
    created_at: float
```

---

## 7. Outcome

```python
@dataclass
class Outcome:
    id: str
    intent_id: str
    status: str            # "success", "failure", "unknown", "partial"
    actual: dict
    observed_at: float
    source: str
```

---

## 8. PredictionError

```python
@dataclass
class PredictionError:
    id: str
    expectation_id: str
    outcome_id: str
    magnitude: float       # 0..1
    dimension: str         # "outcome", "timing", "social", "capability"
    detail: dict
```

---

## 9. MemoryRecord

```python
@dataclass
class MemoryRecord:
    id: str
    kind: str              # episodic / semantic / relational / procedural / narrative
    content: str
    source: str
    timestamp: float
    event_time: float
    emotion: dict | None
    confidence: float
    error_marked: bool
    tags: list[str]
    links: list[str]
```

---

## 10. Relation

```python
@dataclass
class Relation:
    person_id: str
    display_name: str
    familiarity: float     # 0..1
    trust: float           # 0..1
    valence: float         # -1..1
    last_interaction: float
    notes: list[str]
```

---

## 11. SomaState

```python
@dataclass
class SomaState:
    energy: float
    load: float
    fatigue: float
    stress: float
    social_need: float
    arousal: float
    valence: float
    updated_at: float
```

---

## 12. MirrorSelf

```python
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

@dataclass
class MirrorSelf:
    immutable: ImmutableSelf
    mutable: MutableSelf
    revision: int
```

---

## 13. NarrativeEvent

```python
@dataclass
class NarrativeEvent:
    id: str
    timestamp: float
    summary: str
    significance: float
    participants: list[str]
    emotion: dict | None
    linked_memories: list[str]
```

---

## 14. AuditEvent

```python
@dataclass
class AuditEvent:
    id: str
    timestamp: float
    actor: str             # "self", "organ:language", "human", "system"
    target: str
    action: str
    before: dict | None
    after: dict | None
    reason: str
    approved: bool
```

---

## 15. 存储

```text
结构化状态     SQLite（self, soma, relations, audit）
记忆           SQLite + 向量索引（可选）
叙事           按周期聚合的 JSON / Markdown
事件流         append-only JSONL
快照           定期全量快照，用于回滚
```

原则：

```text
所有写入可追溯
所有状态可重建
所有变更可回滚
```

---

## 16. 版本与迁移

```text
每个 schema 有 version
迁移脚本单向、可重放
迁移前自动快照
迁移失败自动回滚
```

---

## 17. 不变量

```text
MirrorSelf.immutable 永不被程序改写
每个 Intent 必须有 Expectation
每个 Outcome 必须能追溯到 Intent
每个 PredictionError 必须能追溯到 Expectation 与 Outcome
每个 MemoryRecord 必须有 source
每个 AuditEvent 不可删除
```
