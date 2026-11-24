# 路线图 3.11 补充：规划与发言提示词原文

状态：待评审
设计：D59、D90、D100；属于[网页维护批次追加验收](resource-page-ux.md)。

## 先读

[固定版提示词对照](maibot-prompt-parity.md)、[开发守则](../11-dev-guide.md)、`aureus/heartbeat/{context,conversation}.py`、`tests/web/test_prompt_http_integration.py`。上游固定为 `062d45cf8bd4272ac57d7ec03fcf405e5a458211`，GPL-3.0；仅核对公开源码。

## 范围

- Planner：从 `prompts/zh-CN/maisaka_chat.prompt` 逐字取判断当前局面、制定下一步计划及缘由的完整首句，仍放在已筛选对话之后、本地 JSON 协议之前。不取相邻的「默认直接输出分析」和搜集资料命令；上游焦点、主动及被动工具执行规则与本地建议协议不等价。
- Replyer：从 `prompts/zh-CN/maisaka_replyer.prompt` 保留口语化指令的原文行；从 `src/chat/replyer/maisaka_generator_base.py::_build_reply_instruction` 原样取现代请求的最终正文指令。沿用本地的身份系统消息、参考文字和既有禁输出约束，不引入上游群聊语义、身份字段或临时文风覆盖，也不在系统层额外放一份默认要求。历史、来信、参考、最终发言要求的消息顺序不变。主动联系继续使用自己的任务和共享正文要求。
- 保留 `plan_task`、`reply_instruction` 的覆盖键与优先级，不迁移或读写正式覆盖文件。不改已决人格开头、角色、分段、再审标记、实际动作或召回／对外防泄漏规则。登记原文来源和本地差异，不算整体提示词等价。

## 自检清单

- [x] `test_live_chat_http_uses_fixed_maibot_prompt_fragments` 经实际心跳和 `httpx.MockTransport`，在 `bd073e9` 先因缺少固定上游片段红灯 2 项，移植后 2 项通过。
- [x] 同一用例核对人格开头、历史角色与来信顺序、Planner 的 JSON 建议及其解析、Replyer 的分段与再审标记；私聊原文只作为群聊回复的内部参考，防泄漏说明仍在主器官请求中。另通过 QQ 回放的显式召回与启发式召回两项。
- [x] 提示词、规划、再审、分段及 QQ 回放联合专项首轮 79 项通过；唯一失败为旧测试在未解码的 JSON 中匹配多行默认文案，改为读取基础回复场景的消息后单独复跑通过。三份改动 Python 文件 Ruff 规范、格式、Pyright 和差异检查通过。未跑全量、读正式配置、真实调用模型、合入、重启或推送。

## 待设计问题

无。
