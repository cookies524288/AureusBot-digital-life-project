# 路线图 3.11 补充：表达抽取提示词原文

状态：完成。`d6f0a5c` 合并版模拟 HTTP 集成文件 7 项通过，已核对固定版原句并确认新内核接管。只评审表达抽取的这一处文案，不承诺模型总能产出无重复的内容。
设计：沿用[在线语言学习](language-extraction-stages.md)与[固定版提示词对照](maibot-prompt-parity.md)，不改 D100。

## 先读与范围

[开发守则](../11-dev-guide.md)、`aureus/common/language_prompts.py`、`tests/web/test_prompt_http_integration.py`。从 MaiBot 1.3.15 固定提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的公开 `prompts/zh-CN/learn_style.prompt` 原样取「输出要求」下的一行：除 JSON 外明确要求不输出重复内容。保留后续本地数组示例、证据编号和空结果规则。未采用上游群聊假设、固定示例或不同的编号说明；不改黑话抽取及释义、覆盖键、样本不可信指令防护、证据校验、存储和召回权限。

## 自检清单

- [x] `test_integrated_prompt_scenes_match_captured_http_and_template_edits` 用 `httpx.MockTransport` 捕获实际表达学习请求，预览与系统规则和末条消息一致。旧模板先因缺少固定版上游原句失败 1 项，移植后通过。
- [x] 联合 `tests/test_language_stages.py` 与 `tests/test_language_runtime.py` 共 13 项通过，覆盖样本编号、输出协议、伪造指令隔离及跨会话内部召回。最终调整断言排版后 HTTP 联测复跑通过。
- [x] 两份改动 Python 文件的 Ruff 规范、格式及 Pyright 通过，`git diff --check` 通过。未跑全量检查，未调用真实模型、接触正式实例或私人数据，未合并、重启或推送。

## 待设计问题

无。
