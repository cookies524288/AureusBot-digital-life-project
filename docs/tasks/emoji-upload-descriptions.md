# 路线图 3.14 补充：批量上传逐图描述

状态：完成（主审独立复测 4 项通过，已合入本地 main 并验证正式实例换核）

设计：在[上传表情时可选登记](emoji-register-on-upload.md)上增补学习页操作，不改变表情库写入规则。

## 先读

- [学习功能对照](maibot-learning-parity.md)、[开发守则](../11-dev-guide.md)，以及网页的 `emojiBatchForm` 和单张上传、批量上传测试。
- MaiBot 1.3.15 固定版本 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的公开 `dashboard/src/routes/resource/emoji/EmojiDialogs.tsx` 先选择文件，再为各图编辑标签并逐项上传；`src/webui/routers/emoji/routes.py` 的上传路由实际直接登记并可改写既有记录。只参考编辑流程，不复制代码，不改变 Aureus 对旧条目与登记授权的保护。

## 行为与边界

- 批量选图后，每页最多 3 张，显示文件名和当前页 / 总页数；按文件选择顺序定位，重名不会串描述。切页保留输入；重新选图清空逐图输入与旧结果。390 像素窄屏不横向溢出。
- 默认不登记；勾选后可填写逐图描述或共享描述。逐图非空描述优先，空白则用共享描述；两者都为空时，确认后定位到第一张缺少描述的图，提示并阻止整个批次。取消勾选即不传登记请求头。确认前关闭不上传。
- 复用已有单图上传接口、校验、原子登记和同摘要保留语义；按顺序发送。上传时禁止重复确认，每张的成功、重复、失败原因在同一分页列表中展示，汇总全批结果，完成后刷新表情库而不重置外层筛选。

## 子步骤

1. 为窄屏逐图描述、翻页和请求验证补浏览器回归，在旧代码先观察失败。
2. 实现分页编辑与结果，复跑专项，更新对照表与交接。

## 自检清单

- [x] `test_emoji_batch_upload_paginates_per_image_descriptions` 验证 390 像素分页、重名草稿、共享缺省、跨页缺项拦截和逐图请求；`test_emoji_batch_upload_cancel_mixed_results_and_filter_persistence` 验证取消零上传、混合结果、外层筛选和统计。
- [x] 新用例用合成图检查结果分页、成功与失败汇总、失败不阻断后续文件；`test_emoji_upload_registers_only_valid_new_assets` 验证重复项不覆盖描述、登记、禁用或移除状态；`test_emoji_single_upload_can_register_on_narrow_screen` 验证单张上传不变。
- [x] 新用例在旧版因缺少分页失败；实现后网页浏览器和接口上传专项共 8 项通过。JavaScript 语法检查、改动测试文件的 Ruff lint／格式与 `git diff --check` 通过。按逐步发布约定未跑全量检查。

## 不做

不改接口、持久数据结构、标签编辑或单张上传，不引入依赖；不访问正式数据、真实模型或用户的 MaiBot 数据，不合入 `main`、重启或推送。

## 待设计问题

无。

## 验收意见
