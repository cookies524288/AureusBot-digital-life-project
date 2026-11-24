# 路线图 3.14 补充：上传表情时可选登记

状态：待评审
设计：延续 [3.14 表情库](3.14.md)及[学习功能对照](maibot-learning-parity.md)，保留原件和已有登记状态。

## 先读

- [资源管理](resource-management.md)、[开发守则](../11-dev-guide.md)。
- `aureus/memory/emoji.py` 的上传和登记校验、`aureus/web/resource_api.py` 的资源维护入口、`aureus/web/static/resource-management.js` 的表情上传表单。
- [MaiBot 1.3.15](https://github.com/Mai-with-u/MaiBot/tree/062d45cf8bd4272ac57d7ec03fcf405e5a458211) 的公开 GPL-3.0 源码，只参考操作，不复制源码。其 [`EmojiUploadDialog`](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/dashboard/src/routes/resource/emoji/EmojiDialogs.tsx) 选择一个或多个文件、逐项编辑标签，随后逐项通过 `/upload` 提交。[单项和批量上传路由](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/webui/routers/emoji/routes.py#L829) 都声明 `is_registered`，但该提交的两个路由实际均直接登记，也可能改写既有条目的描述和禁用状态。Aureus 保留显式选择和重复上传不改状态的边界，不宣称行为等同。

## 目标和验收

单张和批量上传均可勾选「上传后直接登记」；默认关闭。勾选后描述必填，批次内的新图使用同一人工描述。只登记通过现有图片校验的新条目，重传原图不改变旧条目状态。上传错误、重复和成功分别显示，批量完成后保留当前筛选并更新统计。

## 改动和边界

- 原始图片仍以 `application/octet-stream` 发送，复用同一登录、同源、开关和 5 MB 边界。可选 `X-Emoji-Registration-Description` 请求头承载 URL 编码的 UTF-8 描述，存在即请求登记；不把描述放入 URL，不改成功响应 `{digest, created}`。空白、超长或无法解码的描述拒绝，不写原件及库。
- `EmojiStore.upload` 复用图片验证，并在同一个 `INSERT … ON CONFLICT DO NOTHING` 中设置新行描述及登记态，不在上传后再修改旧行。保留同摘要的缺件补齐、计数、标签、原始来源、禁用及移除语义；不覆盖已有媒体。登记所需的描述去空白、限长及非空校验与目录登记一致。
- 单张复用现有弹窗；批量复用顺序上传，两种表单均按勾选显示描述，取消前不请求。途中不重复提交，逐图报告失败，未勾选时保持原先的上传行为。390 像素窄屏仍沿用统一控件。

## 子步骤

1. 添加合成接口和窄屏浏览器回归，旧版先失败。
2. 实现上传选项和原子登记，更新对照表，复跑专项和静态检查后提交复审。

## 自检清单

- [x] 只有新条目才写入人工描述及登记状态。`test_emoji_upload_registers_only_valid_new_assets` 验证正常、缺省、非法描述与图片、重复的未登记及禁用移除条目；`test_emoji_catalog_upload_and_maintenance_preserve_originals` 验证认证、同源、大小限制和原件不覆盖。
- [x] `test_emoji_single_upload_can_register_on_narrow_screen` 验证 390 像素下默认不登记、按需显示必填描述、登记后列表刷新；`test_emoji_batch_upload_cancel_mixed_results_and_filter_persistence` 验证窄屏取消不请求、上传时不能重复提交、成功/重复/失败分开显示，保留筛选并刷新全库统计。
- [x] 三项登记回归在旧代码因忽略登记描述或缺少上传选项而失败，修复后表情存储、接口和浏览器联合专项 19 项通过；导航整文件与接口确认 10 项通过，其中 1 项接口重复。改动 Python 文件的 Ruff 格式、规范及 Pyright 通过，8 条架构规则和 `git diff --check` 通过。仅改表情存储、网页接口及交互、合成测试、任务和交接文档。

本分支按逐步发布约定只运行改动专项，未重跑全量。基线 `e5c875a` 的冻结全检仍有 3 项已记录的 pytest 失败，Ruff 格式和 Pyright 也未全过，见[交接](../handoff.md)。未访问正式数据或 MaiBot 私有数据、调用真实模型、执行 QQ 收发或重启。

## 不做

不添加独立批量路由、逐图标签编辑、模型描述或复审、后台任务，不删除文件，不改生产数据、权限或配置，不合入 `main` 或重启。

## 待设计问题

无。

## 验收意见
