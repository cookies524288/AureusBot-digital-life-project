# 路线图 3.11 / 3.14：QQ 表情视觉选择

状态：待评审

设计：补 [学习核对](maibot-learning-parity.md#表情)和[提示词核对](maibot-prompt-parity.md)的视觉选择缺口；延用 [3.14](3.14.md) 的候选许可和成功发送统计。

## 先读

- [开发守则](../11-dev-guide.md)、[识别专项](emoji-recognition-labels.md)、`aureus/heartbeat/emoji_selection.py`、`aureus/memory/emoji.py`、`aureus/heartbeat/prompt_preview.py`。
- MaiBot 1.3.15 固定提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的 [emoji_selection.prompt](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN/emoji_selection.prompt)、[send_emoji.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/maisaka/builtin_tool/send_emoji.py)及[配置](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/config/official_configs.py)。根许可证 GPL-3.0，按项目许可规则登记移植的提示词。

## 目标与差异

上游视觉子代理抽样，用 256 像素方块、12 像素间隔及左上角角标排成接近方形的实图，按行从 1 编号。格子数不一定等于候选数；上游的附加用户提示固定写 5x5，这里按实图填写。无候选时上游不选择，但其结构化结果默认首项，解析失败或越界也回退首项。Aureus 不照搬回退：整数 0 表示不选，缺键、无效或越界编号均不发表情，也不改走文字选择；现有文本路径仍可独立使用。

## 接口、开关和边界

- `[heartbeat].qq_emoji_visual_enabled` 新增，默认关闭，先受现有 `qq_emoji_selection_enabled` 控制。选择沿用正常 QQ 回复的最终规划意图、授权会话窗口、可选模型预算及现有表情/文字前后顺序。开启后用 `vlm` 接收拼图，不改变旧部署模型路由；视觉不可用时只发原文字。
- 候选沿用表情库低使用次数优先的稳定顺序，不随机抽样。最多取前 25 个目录候选；发送前检查仍有效可读，跳过禁用、移除、未登记、坏图及超限文件；不为坏图占号。线程内缩放有效静态图或动画首帧；编号、候选摘要与拼图共用同一顺序。选择后和现有投递入口照常复核可发送，实际成功回执才记使用。
- 生产和 VLM 预览共用请求构造器，视觉指令在系统消息，最近对话、情绪和计数在用户文本，拼图紧随其后。预览只用模拟变量和具名图片占位。不读目录媒体、聊天记录，不调用模型。

## 子步骤与自检清单

1. 提交本任务书；在独立工作树实现并提交，运行覆盖视觉、文本选择、原 QQ 发送回执、预览和模型传输的合成专项及改动检查。

- [x] `test_visual_grid_filters_candidates_and_maps_labels_to_the_same_order` 核对 PNG 像素、行列、左上角角标及编号映射，坏图、未登记、停用和移除资源不入模型。
- [x] `test_visual_no_selection_and_late_moderation_do_not_try_text` 验证 0、负数、越界、缺键、布尔、字符串、浮点数和损坏 JSON 均不发表情；选择后停用不改发其他表情，无候选不调用模型。
- [x] `test_emoji_visual_preview_matches_actual_http_request_without_reading_media` 用 `MockTransport` 核对实际请求与预览的角色、正文、附件位置；实际请求含 PNG，预览只给具名占位且不读目录媒体。
- [x] `test_qq_emoji_reply_uses_final_plan_and_counts_only_platform_success` 覆盖文字与视觉两种选择、表情前后置和回执成败；原语义匹配与文字回退测试仍通过。

任务书 `2ae75d0`，实现 `b881bad`。选择、QQ 回环与 HTTP 预览专项 21 项，另有媒体、回放、表情库及提示词 API 回归 42 项；合计 63 项通过。改动 Python 文件 Ruff 规范与格式、Pyright（0 错误；显式使用主工作树既有解释器）、架构规则 8 条通过；未跑全量。合成测试根：`E:/Temp/aureus-emoji-visual-selection-0ec57339f9b44a888bc5fb3b51d87ac8`（`pytest`、`regression` 两组）。

## 不做

不改正式实例、数据或部署默认，不推送，不使用真实模型或发送 QQ；不加库维护策略、私有数据迁移或上游插件钩子。

## 待设计问题

无。若维护者考虑在实际部署中开启，需先核实已绑定支持视觉的 VLM、图片发送权限和预算，在此任务之外决定。
