# 04 · 协议

> 内部协议与事件契约。所有模块通过这些协议通信，不直接依赖实现。

---

## 1. 事件总线

Core 内部所有通信走事件总线。

### 1.1 事件信封

```python
@dataclass(frozen=True)
class Event:
    id: str
    topic: str
    timestamp: float
    source: str
    payload: dict
    correlation_id: str | None   # 用于串联一次认知循环
```

### 1.2 主题

```text
percept.received        收到外部事件
soma.updated            内感受变化
salience.scored         显著性评分完成
workspace.broadcast     工作空间广播
self.updated            自我模型更新
intent.proposed         候选意图
intent.selected         选定意图
action.dispatched       行动已发出
outcome.observed        结果已采集
error.detected          预测误差
memory.written          记忆写入
narrative.appended      叙事追加
audit.recorded          审计记录
warden.denied           治理拒绝
```

### 1.3 规则

```text
事件不可变
事件只追加
事件可回放
事件有 correlation_id
```

---

## 2. 器官协议

### 2.1 请求

```python
@dataclass
class OrganRequest:
    organ: str
    frame: CognitiveFrame
    task: str
    params: dict
    timeout_ms: int
```

### 2.2 响应

```python
@dataclass
class OrganResponse:
    organ: str
    ok: bool
    output: dict
    confidence: float
    reasoning: str | None
    error: str | None
    latency_ms: int
```

### 2.3 契约

```text
器官只接收 CognitiveFrame
器官不接收原始消息
器官不能检索额外状态
器官必须返回 confidence
器官必须在 timeout 内返回
器官失败不影响其他器官
```

### 2.4 器官清单

| 器官 | task | 输出 |
|---|---|---|
| language | `compose` | `{candidates: [...]}` |
| planner | `plan` | `{plans: [...]}` |
| memory | `recall` | `{memories: [...]}` |
| critic | `evaluate` | `{score, risks, reasons}` |
| social | `infer` | `{intents, affects}` |

---

## 3. 行动协议

### 3.1 请求

```python
@dataclass
class ActionRequest:
    intent: Intent
    dry_run: bool
    approval_token: str | None
```

### 3.2 响应

```python
@dataclass
class ActionResult:
    intent_id: str
    status: str
    output: dict
    side_effects: list[str]
    reversible: bool
    rollback_token: str | None
```

### 3.3 规则

```text
高风险动作必须带 approval_token
不可逆动作必须 dry_run 一次
所有动作必须返回 side_effects
可逆动作必须返回 rollback_token
```

---

## 4. 治理协议

### 4.1 写入守卫

```python
class WardenGuard(Protocol):
    def guard(self, target: str, patch: dict, actor: str) -> GuardResult: ...

@dataclass
class GuardResult:
    allowed: bool
    reason: str
    requires_approval: bool
```

### 4.2 审批

```python
@dataclass
class ApprovalRequest:
    action: str
    args: dict
    risk: str          # "low", "medium", "high"
    reason: str

@dataclass
class ApprovalResult:
    approved: bool
    token: str | None
    expires_at: float
```

### 4.3 规则

```text
不可变层写入 → 永远拒绝
高危动作 → 必须审批
审计事件 → 不可删除
异常 → 拒绝而非放行
```

---

## 5. 记忆协议

### 5.1 写入

```python
@dataclass
class MemoryWrite:
    kind: str
    content: str
    source: str
    event_time: float
    emotion: dict | None
    confidence: float
    tags: list[str]
```

### 5.2 检索

```python
@dataclass
class MemoryQuery:
    text: str
    kinds: list[str] | None
    time_range: tuple[float, float] | None
    limit: int
    min_confidence: float
```

### 5.3 规则

```text
写入必须带 source
检索必须带时间范围
检索结果必须带置信度
不返回未验证的推断
```

---

## 6. 外部接口（可选）

Aureus 可以作为内核被外部身体调用。

### 6.1 输入

```http
POST /v1/percept
{
  "source": "qq:group:123",
  "kind": "message",
  "payload": {...},
  "timestamp": 1234567890.0
}
```

### 6.2 输出

```http
POST /v1/action
{
  "intent_id": "...",
  "action": "send_message",
  "args": {...}
}
```

### 6.3 状态

```http
GET /v1/self
GET /v1/workspace
GET /v1/soma
GET /v1/audit?since=...
```

### 6.4 规则

```text
外部只能提交 Percept
外部不能直接改自我模型
外部不能直接写记忆
外部不能绕过 Warden
```

---

## 7. 版本

```text
所有协议带 version
不兼容变更升 major
新增字段升 minor
修复升 patch
```

---

## 8. 兼容性

```text
器官可独立升级
协议向后兼容
旧器官在新 Core 上降级运行
新器官在旧 Core 上拒绝注册
```
