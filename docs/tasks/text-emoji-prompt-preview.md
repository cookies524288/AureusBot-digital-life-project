# 路线图 3.14：默认文字选表情的提示词预览

状态：待评审
设计：沿用 D90 的实际请求预览与 [3.14](3.14.md) 的表情选择约定。

## 先读

- [提示词对照](maibot-prompt-parity.md)、[视觉选择](emoji-visual-selection.md)、[开发守则](../11-dev-guide.md)。
- `aureus/heartbeat/emoji_selection.py`、`aureus/heartbeat/prompt_preview.py`、`aureus/organs/topology.py`、`tests/web/test_prompt_http_integration.py`。

## 目标与验收

在器官页可选「QQ 表情文字选择」，用合成近期对话、情绪意图和编号描述预览现有请求。实际生产入口到模拟 HTTP 的消息角色、文本和顺序与预览一致。预览不读聊天、表情或媒体，不调用模型。

## 接口与行为

- `emoji_selection.py` 抽出现有纯请求构造函数，接收原始器官名、已排版的对话、情绪、按顺序的候选描述及偏好。保留 `emoji` 任务名、可选调用、系统文案、描述空白折叠、最多 50 项及 `selected` 编号协议；候选筛选及响应解释仍由选择器负责。
- `organ_tasks` 在总开关开且视觉关闭时登记 `emoji`，按原路由映射到专职 `emoji` 或配置的回复器官。视觉开启时仍只登记已有视觉场景。启用语义匹配时保留文字回退，不新增配置或模板覆盖键。
- 选择 0、无效编号或无候选仍不发表情，不改模型、权限、发送或回执。

## 源码对照

MaiBot 1.3.15 固定提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的公开 [`send_emoji.py`](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/maisaka/builtin_tool/send_emoji.py) 调用视觉拼图选择，抽样并通过带一基 `emoji_index` 的结构解释结果；[`emoji_selection.prompt`](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN/emoji_selection.prompt) 明确要求从拼图选择并返回理由。Aureus 默认发送纯文字对话、情绪和编号描述，只解释 `selected`，0 退出；不移植视觉模板或第三方 Text Selector 插件，不推断私有部署配置。

## 子步骤与自检清单

1. 先提交任务书；新增合成 HTTP 对照及场景检查，验证旧版缺场景后再实现；通过专项与改动检查后提交。

- [x] `test_emoji_text_preview_matches_actual_http_request` 在旧版因缺少场景失败；修复后从实际 `EmojiSelector.select` 捕获 HTTP 消息，逐条核对角色、正文、编号与顺序。首项选择成功；0、字符串编号、越界编号均不选择。预览未访问表情库或调用模型。
- [x] 网页 API 返回可选的 `emoji` 场景；`test_emoji_preview_routes_follow_active_selection_mode` 验证专职优先、回复器官回退，以及视觉或总开关分支。现有网页从 API 结果逐项生成场景下拉选项。
- [x] 表情选择及提示词联合专项 22 项，新增路由专项 1 项，均通过。改动文件 Ruff 规范和格式、Pyright（0 错误）及 8 条架构规则通过；仅运行合成专项，未跑全量。

## 不做

不接正式数据、真实模型或 QQ，不重启、合并或推送，不引进上游插件或改动运行配置。

## 待设计问题

无。
