# 路线图 3.11：QQ 表情识别与标签

状态：待评审

设计：沿用 [3.11 资源管理](resource-management.md) 与 [3.14 表情库](3.14.md) 的媒体授权、登记和维护边界。只补 [学习核对](maibot-learning-parity.md#表情) 与 [提示词核对](maibot-prompt-parity.md) 中的识别差距。

## 先读

- [开发守则](../11-dev-guide.md)、[普通图片提示词预览专项](vlm-image-prompt-preview.md)、`aureus/heartbeat/inbox_media.py`、`aureus/memory/emoji.py`、`aureus/heartbeat/prompt_preview.py`、`tests/channels/test_qq_media.py`。
- 上游 MaiBot 1.3.15 公开提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的 [emoji_manager.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/emoji_system/emoji_manager.py)、[utils_image.py](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/common/utils/utils_image.py) 和 [emoji_content_analysis.prompt](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/prompts/zh-CN/emoji_content_analysis.prompt)。此提交的根 LICENSE 为 GPL-3.0，涉及路径没有另列许可证；依 D12 和 GPL-3.0 第 13 条并入 AGPL-3.0，注明来源并登记 `THIRD_PARTY.md`。

## 目标和验收

仅 OneBot 标记为表情的 QQ 图片使用公开表情识别指令。GIF 水平拼接差异帧；静态图保持单张；把真实请求返回的最多五个标签保存到现存目录字段，可按标签查询。普通图片与表情分别预览，不取实际图片。

## 改动及接口

- 心跳：`emoji_recognition` 准备有界单图/拼图、组装端点请求、规范标签；`InboxMedia` 在现有授权读取和可选 VLM 调用之间接入，不改语音和图片索引原件。
- 记忆：沿用按 SHA-256 收集、同一原件只保存一份；识别成功后通过表情库单独登记标签和简短显示文字，不再累计一次收集。登记时在同一数据库更新里再次核查未登记、未禁用、未移除且没人编辑描述或标签。
- 预览：保留普通图片场景，新增一张静态表情、GIF 拼图两个无字节场景，和生产请求共用构造器。不添加配置、依赖、新表或覆盖键。

## 行为细则和边界情况

1. 延用 QQ 原有读取、大小和安全网关；未标记的 GIF 仍是普通图片。未接入 VLM 时可以收集但不登记；关闭收集不写表情库，依旧可在本轮识别。登记过、禁用、已移除或有维护文字的条目不重复送模型，禁用和移除不改变。未得到可用结果保留原件和未登记状态。
2. GIF 按上游首帧、与上一选中帧的 RGB 均方差大于 1000、最多 15 帧、等比例缩放至高度 200、从左到右 JPEG（质量 85）。计算差值使用无溢出的 Pillow 差值统计，不复制上游无符号溢出；不增加 NumPy 依赖。限定输入 5 MiB、每帧 400 万像素、全部 6400 万像素、最多 200 帧；拒绝越出画布的帧和缺失结束符，拼图限制 400 万像素，在开辟目标画布前检查；继续验证剩余帧而不是在选够以后忽略坏尾帧。普通图片不进入此解码路径；非 GIF 表情仅支持单帧 PNG、JPEG、WebP。失败不降级为把原始动画送模型，原件照常保留，不登记。
3. 解析纯文本列表，支持逗号及常见同类分隔符、按原顺序去掉空项和不区分大小写的重复项；仅收有效短标签，最多五个、每项不超过现有的 40 字限制，不接受 JSON、代码块或带解释的格式。以逗号连接标签作为人可读的现有描述，真实分项另存 `tags`，不假造标签。标签不能触发行动或自行发消息。

## 子步骤与自检清单

1. 先提交任务书。合成用例先复现旧识别行为，后实现请求、登记和预览，并跑覆盖改动的专项后提交；独立开发工作树，不运行正式实例。

- [x] `test_image_prompt_preview_matches_inbox_vision_http_request` 从 OneBot 入站解码和持久化开始，`MockTransport` 核对四种实际请求、GIF 像素及原件，真实安全网关急停时没有视觉 HTTP；在旧代码先因表情仍请求普通描述而失败。
- [x] 同一联测在目录 API 查回标签、验证上限和原件计数；`test_qq_collects_marked_emoji_and_preserves_moderation` 验证合并计数且禁用不再请求；`test_automatic_emoji_registration_preserves_intervening_maintenance` 验证推理中的禁用、移除和人工编辑不被覆盖。
- [x] `test_gif_limits_reject_before_sending` 覆盖帧数、总像素、越界、拼图分配和截断；`test_invalid_emoji_recognition_keeps_original_unregistered` 验证异常 GIF 和无效模型输出保留原件及未登记状态。仅合成数据，不连真实 QQ 或模型，不发送群消息。
- [x] HTTP 联测对照三个预览的文字、角色及图片占位；`test_image_prompt_preview_displays_text_then_attachment` 用 Chrome 在普通图和表情拼图间切换，页面不加载真实图片。
- [x] QQ 媒体和表情回复回放、抽帧、存储、表情选择、资源 API、提示接口、HTTP 和 Chrome 专项合计 45 项通过；改动 Python 文件 Ruff、格式和 Pyright 检查通过，依赖检查 8 条通过。未跑全量。

## 已知边界

- 不补录已有描述但无标签的历史条目；它们仍可按原描述浏览，不会因重复收到而覆盖人工或迁入内容。支持标准画布内、不超过限额的 GIF；超限保留原件，不降级发送原 GIF 给识别器。不接入上游视觉选择和周期维护。

## 不做

历史重识别、独立内容复审、视觉表情选择器、批量维护、周期替换/清扫、任何真实调用、正式实例或远端变更。本任务不等于 MaiBot 学习全套移植，也不改变路线图 3.11 原完成状态。

## 待设计问题

无。

## 验收意见

待 Astra 复审。
