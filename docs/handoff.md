# 3.6a Prompt 集成交接

下一步由 Astra 复审 `codex/phase3-prompts-integration`。实现、自检、依赖提交及基础版浏览器证据已汇总在 [3.6a 任务](tasks/3.6a.md)。

## 3.6a 集成验收（2026-11-23）

状态为待评审。集成分支为 `codex/phase3-prompts-integration`。

- 基础实现 `e374dfc`，浏览器证据 `b187028`。集成起点 `9913337` 已含首批语言实现，随后纳入人物 `3feb949`、语言修订 `49c0594`、记忆 `91db5bd`、夜间整理 `dae9d33`（含 `698da3a`）以及记忆返工 `80e4789`（含 `4f2233e`）。
- `3b49e81` 将人物、两种召回标题及习得语言纳入管理和预览。`4d82c13` 接入夜间提取模板，固定 JSON 输出格式，用编号发言占位预览。`c9bc303` 合入私聊可见性修订，`4c65820` 验证正常心跳生成的规划和回复请求。
- `test_integrated_prompt_scenes_match_captured_http_and_template_edits` 用 MockTransport 核对四器官最终 HTTP 路径、模型、消息角色、顺序及动态替换。网页编辑、单项恢复和重新打开临时配置后继续对照。`learn_language` 对重复样本只发送一次；`_extract` 将两条发言编号为 1、2，保留发言中的字面量 `$messages`。
- 保留 3.7 `65a6580` 对应回归：自定义 `new_messages` 与启发式召回同次生效。派生偏好通过 `_retrieve` 命中原文，`format_recall` 与 `build_reply_prompt` 将它交给回复器。已验证用户私聊记录原文编号；该用户群聊与联系人私聊均为零命中。
- 最终召回、HTTP 与网页专项 37 项通过。`uv run python scripts/check.py` 全部通过，Ruff、Pyright、8 条架构规则通过，pytest 2413 项通过、1 项跳过，10 条第三方 Pydantic 警告。测试使用合成身份、临时存储及模拟模型。桌面和窄屏浏览器证据见 [基础版验收记录](evidence/3.6a/browser-result.json)。
