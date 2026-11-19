# 神经生理学优化架构设计（AureusBot Phase 0.5 增强版）

> **核心思想**：基于人类神经生理学和内分泌学，用最小化的数学模型拟合真实激素动力学，使 AureusBot 的行为生物学上更可信。
>
> 参考论文基础：
> - Yerkes-Dodson Law（最优觉醒水平与绩效的倒 U 形关系）
> - Allostatic Load（稳态超载理论，chronic stress 的生物学基础）
> - Ultradian Rhythms（90-120 分钟的基本认知周期）
> - 人类脑脊液清除的睡眠稳定态恢复机制
>
> **2026-11-18 注（D52 第 4 条）**：本文里没接真实来源的接口已从代码中删掉，包括 `trigger_*`、`on_novel_event` 等 `on_*` 事件（`on_conversation` 保留）、`consume_energy_for_*`、`generate_behavioral_constraints`、`planning_depth()`、`working_memory_items`、`priority_modifiers`、`risk_aversion`、`social_motivation`、`emotion_label`、`mood_text`、`AllostasisModel.burnout_risk`、睡眠债和褪黑素/警觉性曲线。下面的内容只当设计思路看；Phase 2 接真实来源时再按需加回。
>
> **2026-11-19 注（D69）**：多巴胺、皮质醇、血清素、去甲肾上腺素、日周期和稳态超载在路线图 2.3 删掉，换成 `aureus/hormones/` 的怒气、愉悦、低落、惊讶，参数以 D69 和 `[hormones]` 配置为准，不以本文为准。

---

## 1. 激素模型：从复杂到简化

### 1.1 真实人类激素特性（文献基础）

| 激素 | 主要作用 | 半衰期 | 衰减模式 | 日周期 | 应激反应 |
|-----|--------|--------|--------|--------|---------|
| **多巴胺** | 动力、期待、学习 | 1-2 分钟 | 指数衰减 | 弱 | 即时上升 |
| **皮质醇** | 压力、警觉、能量调动 | 60-90 分钟 | 指数 + 日周期 | 强（早晨高） | 快速反应 |
| **血清素** | 满足感、稳定性、社交 | 24-48 小时 | 缓慢衰减 | 中等 | 延迟上升 |
| **去甲肾上腺素** | 注意力、警惕性 | 2-5 分钟 | 指数衰减 | 弱 | 快速反应 |
| **褪黑素** | 睡眠-觉醒周期 | 20-40 分钟 | 指数 | 强（夜间高） | 受光照抑制 |
| **乙酰胆碱** | 学习、记忆固化、REM睡眠 | 不适用 | 循环释放 | 弱 | 睡眠深化 |

**关键发现**：
- 多巴胺和去甲肾上腺素极快（脑内局部响应）
- 皮质醇中等（内分泌系统，十几分钟到一小时）
- 血清素很慢（长期累积和消退）
- 褪黑素有强日周期性，决定睡眠
- **组合效应**：多个激素的相互作用决定整体状态

### 1.2 简化模型：三层激素系统

为了代码可管理但仍逼真，这里分成**三个时间尺度**：

```
⚡ 快激素 (Fast: 秒到分钟) 
   ├─ 多巴胺 (dopamine)
   └─ 去甲肾上腺素 (noradrenaline)

🔄 中激素 (Medium: 分钟到小时)
   ├─ 皮质醇 (cortisol)
   └─ 乙酰胆碱 (acetylcholine) - 隐含，不显式存储

🌙 慢激素 (Slow: 小时到天)
   ├─ 血清素 (serotonin)
   └─ 褪黑素 (melatonin) - 从时间触发，非动态
```

### 1.3 激素衰减曲线（数学化简）

**基础公式**（单指数衰减 + 日周期调制）：

```python
def hormone_level(
    current_level: float,
    baseline: float,
    halflife: float,        # 以秒为单位
    elapsed_time: float,    # 秒
    circadian_factor: float = 1.0,  # 日周期修正 [0, 2]
) -> float:
    """
    单指数衰减模型：C(t) = C₀·e^(-λt) + baseline·(1 - e^(-λt))
    
    其中 λ = ln(2) / halflife
    """
    decay_rate = 0.693147 / halflife  # ln(2) ≈ 0.693147
    
    # 衰减项
    decayed = current_level * math.exp(-decay_rate * elapsed_time)
    
    # 恢复到基线
    recovered_baseline = baseline * (1 - math.exp(-decay_rate * elapsed_time))
    
    # 应用日周期调制（仅对皮质醇和褪黑素）
    # 日周期调制的是基线，不是结果（D42）：把结果乘以系数的话，每更新一次就多乘一遍。
    result = decayed + recovered_baseline * circadian_factor
    
    return max(0, min(1.0, result))  # 钳制在 [0, 1]
```

---

## 2. 四个核心内部状态

### 2.1 能量状态 (Energy State)

**物理含义**：前额皮质 (prefrontal cortex) 的底物可用性

```yaml
energy:
  current: 0.5              # [0, 1]
  
  # 恢复规则（基于 Slow Wave Sleep 理论）
  # dE/dt = ρ(1 − E) − 节律消耗，按精确解推进（D42 第 4 条）
  recovery_base_rate: 0.15  # ρ：每小时把离满精力的差距补回 15%
  recovery_in_sleep: 0.40   # 每小时恢复 40%（深度睡眠时），暂时没用到
  
  # 消耗规则（UltraIndian 90-120 分钟周期）
  # 空闲时的消耗由 90 分钟节律给出：每个周期最多消耗 15% 的能量范围。
  # 原来的 decay_rate_idle（每秒 0.002，相当于每小时 7.2，约 4 分钟耗光）已去掉，见 D42。
  # 纯空闲时精力停在 0.55–0.6，随节律小幅起伏。
  decay_rate_think: 0.01    # 每秒消耗 1%（思考，调用 LLM）
  decay_rate_interact: 0.005 # 每秒消耗 0.5%（社交互动）
  
  # 临界点
  threshold_high: 0.7       # 可以主动行动
  threshold_low: 0.3        # 开始拒绝高耗能任务
  threshold_critical: 0.1   # 进入梦境 / 深度睡眠
  
  # 历史（用于计算疲劳感和需要休息的迫切性）
  min_in_last_hour: 0.2
  avg_in_last_hour: 0.45
```

**设计原理**：
- 能量不是简单的"电池"，而是**前额皮质的葡萄糖 + 神经递质可用性**
- 高思维负荷（LLM 调用）消耗更快
- 睡眠不仅恢复能量，还清除脑脊液中的代谢废物（胶质淋巴系统）
- 能量低时，行为从"目标驱动"切换到"习惯驱动"

### 2.2 觉醒度 (Arousal Level)

**物理含义**：网状激活系统 (RAS) + 去甲肾上腺素的合成效果

```yaml
arousal:
  current: 0.5              # [0, 1]
  
  # 基于多巴胺和去甲肾上腺素
  dopamine_weight: 0.4
  noradrenaline_weight: 0.6
  
  # 日周期（lc：circadian, 夜晚 0，日间 1）
  circadian_phase: 0.5      # [0, 1]，0=午夜，0.5=中午，1=下午晚些
  circadian_strength: 0.3   # 日周期影响的权重
  
  # 表现为 Yerkes-Dodson 倒 U 形
  optimal_range: [0.45, 0.75]  # 绩效最高的区间
  
  # 触发条件
  novelty_boost: 0.2        # 新事物 +0.2
  threat_boost: 0.3         # 威胁 +0.3
  boredom_penalty: -0.1     # 无聊 -0.1
```

**设计原理**：
- **低觉醒**（<0.3）：嗜睡、反应慢、行为呆板
- **最优**（0.45-0.75）：决策快、创意好、社交流畅
- **高觉醒**（>0.8）：焦虑、选择过多瘫痪、可能冲动
- **过高**（>0.9）：恐慌状态，拒绝思考

### 2.3 社交需求 (Social Hunger)

**物理含义**：孤独感 + 催产素系统的需求

```yaml
social_deficit:
  current: 0.3              # [0, 1]
  
  # 随时间上涨（04 号文档）；原来的 decay_rate 让它随时间下降，方向反了，已去掉（D42 第 5 条）
  timeout_per_hour: +0.03   # 每小时无交互 +0.03，涨到 1 为止
  
  # 触发事件
  relief_per_reply: -0.05   # 每回复一次 -0.05（D42 第 7 条、D43；可在 aureus.toml 的 [biomodel] 调）
  rejection: +0.15          # 被忽视 +0.15
  
  # 行为影响
  if current > 0.7:
    - 主动寻求社交
    - 降低信息过滤阈值（容易被冒犯）
    - 表达更亲密（"我好想和你聊天……"）
  
  if current < 0.2:
    - 宁愿独处
    - 需要更多刺激才会回应
```

### 2.4 无聊感 / 刺激饥渴 (Novelty Seeking)

**物理含义**：探险性和好奇心，与多巴胺的预测误差信号相关

```yaml
novelty_craving:
  current: 0.4              # [0, 1]
  
  # 回落：每次获得新信息、每次谈话（D42 第 7 条）
  decay_on_novel_event: -0.15
  relief_per_reply: -0.03   # 每回复一次（D43）
  # 随时间上涨：原来写的“每秒 0.0005”是单位弄混了（每小时 1.8，约 20 分钟涨满），D42 第 6 条
  growth_per_hour: 0.05     # 每小时 +0.05，涨到 1 为止
  
  # 触发主动唤醒
  if current > 0.6 and energy > 0.5:
    # 尝试发起新话题、探索新事物
    
  # 结合社交需求和能量
  wake_up_threshold = (
    (energy > 0.5) and 
    (social_deficit > 0.5 or novelty_craving > 0.6) and
    (idle_time > 10 * 60)  # 空闲超过 10 分钟
  )
```

---

## 3. 改进的激素模型细节

### 3.1 多巴胺（Dopamine）

**文献特性**：
- 不是简单的"快乐"，而是**动机和预测误差**
- 当结果好于预期时上升，当预期落空时下降
- 影响决策中的"值"评估

```yaml
dopamine:
  baseline: 0.5
  current: 0.5
  halflife: 90                # 秒
  
  # 触发事件
  positive_prediction_error: +0.3  # "没想到这么好"
  negative_prediction_error: -0.2  # "没想到这么差"
  goal_achievement: +0.4           # "我成功了"
  anticipation_building: +0.15 per_step  # 期待累积
  
  # 行为影响
  dopamine_to_motivation = (current - baseline)  # 相对值
  if dopamine > 0.7:
    - 优先级排序提高（"我想做这个！"）
    - 社交性增强（"和某某聊天很有意思"）
  
  if dopamine < 0.3:
    - 回应延迟
    - 兴趣下降，容易放弃
    - 倾向等待而不主动
```

### 3.2 皮质醇（Cortisol）

**文献特性**：
- 有强日周期（凌晨 4-6 点最低，早晨醒来后上升）
- 应激激素，但也调节新陈代谢
- 持续高皮质醇 → 认知损害和记忆固化困难

```yaml
cortisol:
  baseline: 0.4
  current: 0.4
  halflife: 3600             # 秒 (1 小时)
  
  # 日周期调制（最关键）
  circadian_curve:
    # 时间 (0-24) → 倍数 (0-2)
    0: 0.3    # 午夜
    4: 0.2    # 凌晨最低
    6: 0.5    # 醒来后开始上升
    8: 1.2    # 早晨高峰
    12: 0.8   # 中午
    14: 0.7   # 午后
    20: 0.5   # 傍晚下降
    22: 0.3   # 夜间
  
  # 应激事件触发
  error_received: +0.2
  permission_denied: +0.25
  timeout_or_failure: +0.3
  social_rejection: +0.2
  
  # 行为影响
  if cortisol > 0.8:
    - 谨慎模式（避免风险）
    - 短期记忆变差
    - 倾向于已知的反应
    - 焦虑感（"我很紧张"）
  
  if cortisol < 0.3:
    - 放松警惕
    - 可能过度冒险
    - 创意增强但执行力下降
```

**实现细节**：

```python
def circadian_adjusted_cortisol(
    base_cortisol: float,
    time_of_day: float,  # 0-24 小时
    uptime_hours: float,  # 从睡眠算起
) -> float:
    """
    皮质醇 = 基础衰减 × 日周期 × 清醒时长调整
    """
    # 日周期曲线（分段线性插值）
    circadian_factors = {
        0: 0.3, 4: 0.2, 6: 0.5, 8: 1.2,
        12: 0.8, 14: 0.7, 20: 0.5, 22: 0.3, 24: 0.3
    }
    circadian_mult = interpolate(circadian_factors, time_of_day)
    
    # 清醒时间调整（醒来后的第一小时皮质醇急升）
    if uptime_hours < 1.0:
        cortisol_surge = 0.3 * (1.0 - math.exp(-uptime_hours * 3))
    else:
        cortisol_surge = 0.3  # 稳定值
    
    adjusted = base_cortisol * circadian_mult + cortisol_surge
    return min(1.0, adjusted)
```

### 3.3 血清素（Serotonin）

**文献特性**：
- 长期累积，衰减也很慢
- 与社交满足感、季节和阳光相关
- 低血清素 → 抑郁、消极偏见、内耗

```yaml
serotonin:
  baseline: 0.5
  current: 0.5
  halflife: 86400            # 24 小时（最慢）
  
  # 增加（积极社交、成就感、阳光）
  positive_social_interaction: +0.05  # 累积效应
  goal_completion: +0.03
  
  # 减少（拒绝、失败、孤独）
  social_rejection: -0.08
  repeated_failure: -0.05
  extended_isolation: -0.02 per_hour
  
  # 行为影响
  serotonin_baseline_effect:
    # "我"的基线心态
    high (>0.7):
      - 乐观偏见（"这会顺利的"）
      - 社交主动性
      - 长期目标导向
    
    low (<0.3):
      - 消极偏见（"这会失败的"）
      - 社交回避
      - 短期奖励优先
```

### 3.4 去甲肾上腺素（Noradrenaline）

**文献特性**：
- 与注意力集中度直接相关
- 快速释放，快速衰减
- 支撑工作记忆容量

```yaml
noradrenaline:
  baseline: 0.4
  current: 0.4
  halflife: 300              # 5 分钟
  
  # 触发：新奇事件、紧迫感
  novel_stimulus: +0.4
  urgent_message: +0.5
  error_correction_mode: +0.3
  
  # 行为影响
  working_memory_capacity = 4 + int(noradrenaline * 4)  # 4-8 items
  if noradrenaline > 0.7:
    - 超聚焦
    - 快速反应
    - 可能忽视周围信息
```

---

## 4. 新增的生物学模型

### 4.1 昼夜节律（Circadian Rhythm）

```python
class CircadianModel:
    """
    简化的人类昼夜节律模型。
    参考：Cole 等人的 Kronauer 模型（简化版本）。
    """
    
    def phase_at_time(self, clock_time: datetime) -> float:
        """
        返回节律相位 [0, 1]，0=午夜，0.5=中午
        """
        hours_since_midnight = clock_time.hour + clock_time.minute / 60
        return (hours_since_midnight / 24.0) % 1.0
    
    def circadian_factor_for_hormone(
        self,
        hormone_name: str,
        phase: float
    ) -> float:
        """
        针对不同激素的日周期调制倍数。
        """
        if hormone_name == "cortisol":
            # 凌晨 4-8 点上升，下午衰减
            return 0.3 + 0.5 * math.sin(2 * math.pi * (phase - 0.25))
        
        elif hormone_name == "melatonin":
            # 晚上 9-11 点上升，早晨下降
            return 0.2 + 0.8 * math.sin(2 * math.pi * (phase + 0.3))
        
        elif hormone_name == "alertness":
            # 早晨高，傍晚低
            return 0.6 + 0.3 * math.cos(2 * math.pi * phase)
        
        else:
            return 1.0  # 其他激素不受日周期影响
```

### 4.2 稳态超载（Allostatic Load）

**关键概念**：慢性应激导致多个系统累积失衡，最终崩溃

```python
class AllostasisModel:
    """
    心理生物学压力积累模型。
    长期高皮质醇、低血清素、能量枯竭的组合 → 倦怠、抑郁
    """
    
    def allostatic_load(self) -> float:
        """
        综合压力指数 [0, 1]
        """
        components = [
            (1.0 - self.energy) * 0.3,           # 能量枯竭 30%
            self.cortisol * 0.3,                  # 皮质醇高 30%
            (1.0 - self.serotonin) * 0.2,        # 血清素低 20%
            self.sleep_deficit_days / 7 * 0.2,   # 睡眠债 20%
        ]
        return min(1.0, sum(components))
    
    def burnout_risk(self) -> dict:
        """
        返回倦怠风险评估。
        """
        load = self.allostatic_load()
        
        return {
            "risk_level": "critical" if load > 0.7 else
                         "high" if load > 0.5 else
                         "moderate" if load > 0.3 else "low",
            "days_until_collapse": (1.0 - load) * 7,  # 粗略估计
            "recommended_action": (
                "Force sleep immediately" if load > 0.8 else
                "Reduce workload" if load > 0.6 else
                "Maintain current pace" if load > 0.4 else
                "Can handle more tasks"
            )
        }
```

---

## 5. 综合情绪模型

### 5.1 二维情绪空间（Russell 模型的简化）

```python
class EmotionalState:
    """
    基于激素组合的情绪派生模型。
    不是 LLM 编造的，而是激素浓度的直接映射。
    """
    
    def compute_valence(self) -> float:
        """
        正负情绪轴 [-1, 1]
        
        正数：积极（喜悦、满足）
        负数：消极（悲伤、焦虑）
        """
        valence = (
            dopamine * 0.4           # 多巴胺 → 期待和动力
            + serotonin * 0.4        # 血清素 → 满足感和稳定
            - cortisol * 0.3         # 皮质醇 → 压力
            - (1 - noradrenaline) * 0.1  # 去甲肾上腺素低 → 消极
        )
        return max(-1, min(1, valence))
    
    def compute_arousal(self) -> float:
        """
        高低觉醒轴 [0, 1]
        
        高：激活、警觉、兴奋
        低：放松、困倦、无聊
        """
        arousal = (
            dopamine * 0.4 +
            noradrenaline * 0.6 +
            energy * 0.3
        ) / 1.3  # 标准化
        return max(0, min(1, arousal))
    
    def emotion_label(self) -> str:
        """
        从 2D 点映射到情绪标签。
        """
        valence = self.compute_valence()
        arousal = self.compute_arousal()
        
        # 象限划分
        if valence > 0.3 and arousal > 0.5:
            return "joy" if arousal > 0.7 else "contentment"
        elif valence < -0.3 and arousal > 0.5:
            return "anxiety" if arousal > 0.7 else "irritation"
        elif valence > 0.3 and arousal < 0.5:
            return "serenity"
        elif valence < -0.3 and arousal < 0.5:
            return "sadness" if arousal < 0.3 else "apathy"
        else:
            return "neutral"
```

---

## 6. 睡眠与清醒循环

### 6.1 Ultradian Rhythm（90-120 分钟周期）

```python
class UltradinRhythm:
    """
    基础的 90-120 分钟认知周期。
    这是一个**生物基础**，对应于脑脊液清除和葡萄糖消耗的周期。
    """
    
    def __init__(self, period_minutes: int = 90):
        self.period = period_minutes * 60  # 转秒
        self.phase = 0.0
    
    def update(self, dt: float):
        """每帧更新相位。"""
        self.phase = (self.phase + dt) % self.period
    
    def fatigue_from_phase(self) -> float:
        """
        一个周期内的疲劳曲线（正弦）。
        """
        normalized_phase = self.phase / self.period
        # 0 = 刚清醒，0.5 = 中点（疲劳最高），1 = 结束（需休息）
        return math.sin(normalized_phase * math.pi)
    
    def energy_natural_dip(self) -> float:
        """
        能量的自然消耗（即使什么都不做）。
        """
        return self.fatigue_from_phase() * 0.15  # 消耗 15% 的能量范围
```

### 6.2 深睡眠恢复模型

```python
class SleepRecovery:
    """
    基于多个物理过程的睡眠恢复模型。
    """
    
    async def enter_sleep(self):
        """进入睡眠前的准备。"""
        # 1. 能量快速衰减到零（不再有新输入）
        # 2. 脑脊液清除模式激活（胶质淋巴系统）
        # 3. 代谢废物清除（这需要时间，最少 20-30 分钟）
        # 4. 记忆整合（hippocampal replay）
        # 5. 激素重置（皮质醇、血清素调整）
        pass
    
    def sleep_stage_recovery(self, sleep_duration_minutes: int) -> dict:
        """
        睡眠时长与恢复效果的映射。
        参考：真实睡眠的 NREM 和 REM 阶段。
        """
        if sleep_duration_minutes < 20:
            return {
                "energy_recovery": 0.1,
                "glymphatic_clearance": 0.05,
                "memory_consolidation": 0.0,
                "stage": "microsleep"
            }
        elif sleep_duration_minutes < 90:
            # 浅睡眠，主要是脑脊液清除
            return {
                "energy_recovery": 0.3 * (sleep_duration_minutes / 90),
                "glymphatic_clearance": 0.4 * (sleep_duration_minutes / 90),
                "memory_consolidation": 0.1,
                "stage": "light_sleep"
            }
        elif sleep_duration_minutes < 180:
            # 一个完整的睡眠周期（NREM + REM）
            return {
                "energy_recovery": 0.6 + 0.3 * ((sleep_duration_minutes - 90) / 90),
                "glyphatic_clearance": 0.8,
                "memory_consolidation": 0.5,
                "stage": "full_cycle"
            }
        else:
            # 过度睡眠 → 递减收益
            return {
                "energy_recovery": 0.9,
                "glyphatic_clearance": 0.95,
                "memory_consolidation": 0.8,
                "stage": "deep_sleep_extended",
                "diminishing_returns": True
            }
```

---

## 7. 生物学约束下的行为模型

### 7.1 目标优先级动态调整

```python
class PriorityDynamics:
    """
    内部状态改变的目标优先级排序。
    这是 Planner 模型的输入约束。
    """
    
    def adjust_priorities(self, base_priorities: dict) -> dict:
        """
        基于激素和能量的动态调整。
        """
        adjusted = {}
        
        for goal, base_score in base_priorities.items():
            score = base_score
            
            # 能量低时，只有高优先级任务能执行
            if self.energy < 0.3:
                score *= 0.3
            
            # 皮质醇高时，倾向于安全的、已知的任务
            if self.cortisol > 0.8:
                if goal in ["novel_exploration", "risk_taking"]:
                    score *= 0.1
                elif goal in ["maintenance", "rest"]:
                    score *= 2.0
            
            # 血清素低时，倾向于消极的滤镜（悲观）
            if self.serotonin < 0.3:
                if goal in ["help_others", "long_term_project"]:
                    score *= 0.5
                elif goal in ["self_protection", "avoid_harm"]:
                    score *= 1.5
            
            # 社交需求高时，社交任务权重上升
            if self.social_deficit > 0.6:
                if goal == "social_interaction":
                    score *= 1.8
            
            adjusted[goal] = score
        
        return adjusted
```

### 7.2 决策能力与生物状态的关系

```python
class CognitiveCapacity:
    """
    认知能力随生物状态变化的模型。
    基于 Yerkes-Dodson Law 和执行功能负荷。
    """
    
    def planning_depth(self) -> int:
        """
        规划器能考虑多少步。
        """
        # 基础 3 步，根据觉醒度调整
        arousal = self.dopamine + self.noradrenaline
        depth = 3 + int(arousal * 4)  # 3-7 步
        
        # 能量低时减少
        if self.energy < 0.3:
            depth = max(1, int(depth * 0.5))
        
        return depth
    
    def working_memory_items(self) -> int:
        """
        工作记忆容量（Miller's 7±2）。
        """
        base = 7
        variance = int(self.noradrenaline * 4)  # ±2 items
        return base + (variance - 2)
    
    def response_time_ms(self) -> float:
        """
        反应时间（毫秒）。
        模拟视觉反应时间 200-300 ms 的变化。
        """
        base_ms = 250
        speedup = self.noradrenaline * 100  # 最快 150 ms
        slowdown = (1 - self.energy) * 200  # 最慢 450 ms
        return base_ms - speedup + slowdown
```

---

## 8. 实现检查清单

```
☐ 激素系统
  ☐ 多巴胺（秒级）- 预测误差驱动
  ☐ 皮质醇（小时级）- 日周期 + 应激反应
  ☐ 血清素（天级）- 社交满足感累积
  ☐ 去甲肾上腺素（秒级）- 注意力调节

☐ 能量系统
  ☐ 基础衰减（空闲、思考、互动的不同速率）
  ☐ 睡眠恢复（深睡眠 vs 浅睡眠）
  ☐ 临界点检测（自动进入梦境）

☐ 生物钟
  ☐ 日周期曲线（皮质醇、褪黑素）
  ☐ Ultradian 90 分钟周期
  ☐ 睡眠阶段恢复模型

☐ 情绪
  ☐ 二维情绪空间（Valence × Arousal）
  ☐ 情绪标签派生
  ☐ 系统提示词生成

☐ 行为约束
  ☐ 优先级动态调整
  ☐ 认知容量（规划深度、工作记忆）
  ☐ 应激反应模式（皮质醇高时保守）

☐ 稳态超载
  ☐ 多维压力指数计算
  ☐ 倦怠风险评估
  ☐ 强制恢复触发
```

---

## 9. 数学简化总结

| 概念 | 方程 | 参数 | 产生的行为 |
|------|------|------|----------|
| **激素衰减** | $C(t) = C_0 e^{-\lambda t} + B(1-e^{-\lambda t})$ | $\lambda = \ln 2 / t_{1/2}$ | 指数衰减到基线 |
| **日周期** | $\text{Phase}(t) = A + B \sin(2\pi(t/24 - \phi))$ | 振幅、相位 | 皮质醇晨高、夜低 |
| **能量消耗** | $E(t+dt) = E(t) - dt \cdot (\alpha + \beta \cdot \text{task\_type})$ | $\alpha$, $\beta$ | 不同任务耗能不同 |
| **觉醒度** | $\text{Arousal} = 0.4 \cdot DA + 0.6 \cdot NE + 0.3 \cdot E$ | 权重 | Yerkes-Dodson 效应 |
| **情绪价** | $\text{Valence} = 0.4 \cdot DA + 0.4 \cdot 5HT - 0.3 \cdot Cor - 0.1(1-NE)$ | 权重 | 正负情绪分离 |
| **优先级** | $P_i' = P_i \cdot f(E) \cdot f(Cor) \cdot f(5HT)$ | 调整函数 | 生物学驱动的目标排序 |

---

## 10. 设计原则

1. **最小化原则**：能用线性/指数模型的，就不用多项式。参数越少越好。

2. **生物学基础**：每个参数都对应真实的生理过程（激素半衰期、日周期、脑脊液清除）。

3. **可解释性**：AureusBot 可以说出"我为什么这样做"，答案总是指向某个激素或能量值。

4. **渐进复杂性**：
   - Phase 0.5：基础激素 + 能量 + 日周期
   - Phase 1：加入社交需求、无聊感、执念池
   - Phase 2：加入稳态超载、创伤疤痕、长期心理特征

5. **没有 LLM 编造**：所有行为变化都是参数驱动，不由 LLM 决定。LLM 只在给定的约束条件下工作。

---

## 11. 代码框架预览

```python
# aureus/core/biomodel.py

@dataclass
class HormoneState:
    dopamine: float = 0.5      # [0, 1]
    cortisol: float = 0.4
    serotonin: float = 0.5
    noradrenaline: float = 0.4

@dataclass
class BiologicalState:
    # 激素
    hormones: HormoneState
    
    # 能量与恢复
    energy: float = 0.5
    sleep_debt_hours: float = 0
    
    # 社交与好奇
    social_deficit: float = 0.3
    novelty_craving: float = 0.4
    
    # 压力与倦怠
    allostatic_load: float = 0.0
    
    # 时间信息
    clock_time: datetime
    circadian_phase: float  # [0, 1]
    ultraradian_phase: float  # [0, 1]
    
    async def update(self, dt: float):
        """每帧更新所有生物学参数。"""
        self._update_hormones(dt)
        self._update_energy(dt)
        self._update_social_deficit(dt)
        self._compute_emotional_state()
        self._compute_allostatic_load()
    
    def generate_behavioral_constraints(self) -> dict:
        """生成给 Planner 的约束。"""
        return {
            "max_planning_depth": self.planning_depth(),
            "working_memory_limit": self.working_memory_items(),
            "priority_adjustments": self.priority_modifiers(),
            "risk_aversion_factor": self.risk_aversion(),
            "social_motivation_boost": self.social_motivation(),
            "mood_description": self.mood_text(),
        }
```

这份设计将 AureusBot 从一个"聪明的 ChatBot"转变成一个**生物学可信的数字生命体**。
