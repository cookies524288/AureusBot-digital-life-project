# Phase 3 语言学习：会话分批与提示词证据

状态：完成

## 先读

`docs/tasks/maibot-learning-parity.md`、`docs/tasks/maibot-prompt-parity.md`、`docs/11-dev-guide.md`。对照 MaiBot 1.3.15 固定提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的公开表达与黑话学习器。只读源码，不访问私人配置或样本。

## 目标与子步骤

1. 新入站样本入队时附可信路由，以完整 ConversationKey 分批；网页和终端只在自身可信通道与用户内分批。不从正文、昵称或界面选项推导会话。缺可信路由的平台样本不能与其他人的样本一起发送。保留未选批次的顺序和学习机会，不改变回复优先级和原有去重规则。
2. 学习请求按 system 规则、逐条 user 样本、最终 user 输出要求组装。去重过滤后从 1 编号。样本正文仅作数据，预览复用实际构建器。保留既有输出协议、预算、器官绑定和覆盖键，但不要重复注入样本。只将器官实际看到且含该词的片段作为新提议证据；批内编号须为有效整数，映射回真实记录号后沿用原持久证据键。不要因截断而改变已知表达的普通观察行为。
3. 通过合成专项后同步两份对照表与交接。不把本步称作上游独立表达、黑话请求或多阶段释义已移植。后续分别对齐抽取和释义流程。

## 自检清单

- [x] 两群、普通私聊与临时私聊交错，裸目标或昵称相同也不混批；其余批次不丢失，缺路由不误并：`test_language_learning_batches_by_trusted_conversation` 通过。
- [x] 只接受实际展示的本轮编号和片段；无效编号、伪造编号、重复编号和截断之后的词不能制造证据；重复投递与持久记录号仍正确：`test_language_learning_maps_only_presented_evidence_ids`、`test_presented_evidence_must_be_a_prefix_of_the_original` 通过。后一反例在旧实现上失败、修复后通过。
- [x] 用 MockTransport 捕获实际 HTTP 的角色和顺序，测试恶意样本仍仅是单条 user 文本，预览和运行采用同一模板：`test_integrated_prompt_scenes_match_captured_http_and_template_edits` 通过。
- [x] 七文件语言学习、记忆、QQ 回放、HTTP 预览及适配器专项合计 105 项通过；未调用真实模型或 QQ。全检 `uv run --active --no-sync python scripts/check.py`：2649 通过、8 失败、4 跳过；Ruff 规范与架构检查通过，格式检查有 `aureus/attention/working_memory.py`、`tests/core/test_model_runtime.py` 两处既有差异；Pyright 有 `aureus/lab/charts.py`（matplotlib）与 `aureus/monitor/page.py`（pandas、streamlit）4 项缺失可选依赖。全检 8 项失败中，5 项旧基线失败，另有回复分段 2 项及记忆批量移除浏览器 1 项；这 3 项均在任务基线提交 `25ff850` 独立复现。

## 不做

不沿用混批作为失败回退，不将分批键授予旧资料召回权；不改正式人格或私人库，不推送。遇到无法证明可信路由的通道记录反例，不自行放开。

## 验收意见

Astra 复审通过 `78de236`；持久收件箱路由用于分批，批内整数编号仅映射到实际展示的原文前缀。独立重跑学习、存储、HTTP 请求与 QQ 通道回归共 36 项通过。`82a179e` 已合并到本地 main 并完成当次内核接管验证；不代表所有 MaiBot 学习任务已移植。

实现提交与专项、全检快照见 `docs/handoff.md`。全检中的三项额外失败已在 `25ff850` 基线工作树独立复现，不属于本任务引入；不修与本任务无关的浏览器、回复分段或基线失败。
