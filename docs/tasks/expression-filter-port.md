# Phase 3.11 · 在线表达学习写入前过滤

状态：实现中
设计：D89、D100；沿用[在线语言学习](language-extraction-stages.md)的证据和存储边界。

## 先读

[学习功能核对](maibot-learning-parity.md)、[D100](../10-decisions.md)、[开发守则](../11-dev-guide.md)、`aureus/heartbeat/language_runner.py`、`aureus/memory/language.py`。对照 MaiBot 1.3.15 固定公开提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211`，根许可证 GPL-3.0。只读公开源码，不读取本机 MaiBot 数据或配置。

## 行为和边界

- [上游调用](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/learners/expression_learner.py#L394)：`runtime` 发起学习，`_run_learning_batch` 解析后调用 `_filter_expressions`，再经写入钩子及可选 `_check_expression_before_upsert`，最后 `_upsert_expression_to_db`。本步移植前置确定性规则：去掉风格开头的「使用」（兼容 Aureus 原有的「使用：」），清洗后为空不写入；风格与当前真实机器人名字或去掉首尾空白的任一昵称做 `casefold` 后完全相等时不写入，不禁用包含名字的句式；编号指向的完整来源包含大写 `SELF` 时不写入，即使标记在展示上限之后。
- Aureus 从当前内存身份取名字/昵称，仅在本地 `learn_language → _extract → _parse_proposals → LanguageStore.observe_extraction` 筛表达，不增模型请求或提示词字段。原有严格编号映射、QQ 自发消息排除、空来源、图像/表情标记、风格和情景的 `SELF` 检查仍由现有路径执行。继续保存实际展示的来源前缀；一次可解析的抽取仍消费来源，黑话、原句观察、现有学习开关均不变。
- `_check_expression_before_upsert` 实际经 [check_expression_suitability](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/learners/expression_utils.py#L132) 向 `learner` 发 `expression.check` 请求，写通过、拒绝或错误日志；不等同于确定性过滤。若要增加该路径，需另定模型接收方、可发送的场景和风格、费用、触发开关及审查记录权限。现有人工审核不变。
- 名为 `_find_similar_expression` 的实现只在同会话内比对清洗后的完全相同风格和情景，命中后增加计数并更新审核标记，不做近义相似度。Aureus 沿用现有候选键及唯一来源计数，不重置审核、不删除或重扫旧原件、不改 D100 的跨会话召回和包授权。不移植插件、上游单批 20 条规则（保留本项目上限）、上游宽松解析或只在上游回复候选使用的提示示例屏蔽。

## 实现和验收

1. 在合成数据库中，从实际收件箱、心跳和假器官重现新过滤生效前的失败，然后接入写入路径。
2. 跑语言抽取、运行时、D100 及相邻存储专项，检查改动文件规范、格式、类型和架构；登记固定源码与差异。此分支不改交接、不合入、不推送、不重启正式实例。

## 自检清单

- [ ] `test_online_expression_filter_runs_before_write`：昵称精确过滤、使用前缀清洗、完整来源标记、同回复的有效提议、拒绝后来源仍存和重复投递无新请求。
- [ ] 既有线上学习、黑话、D100 实际 Planner/Replyer 请求、包授权和原件维护专项通过。
- [ ] 来源与许可已登记；文件检查及架构通过；未接入自评、相似合并或历史修改。

## 待设计问题

本步无。自动模型自评及日志如上，超出本步授权。

## 验收意见

待 Astra 复审。
