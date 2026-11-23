# 学习页：批量上传表情

状态：待评审

## 先读

`docs/tasks/maibot-learning-parity.md` 的表情章节、`docs/tasks/resource-page-ux.md`，以及现有单张上传的 API 和 Chrome 验收。对应 Phase 3.11 网页维护。

## 子步骤

1. 对照固定版本 MaiBot 公开的批量上传交互，在现有学习页允许一次选择多张图片，确认前列出所选文件数。取消不发上传请求。
2. 复用当前已认证、同源和逐文件校验的单张入口；批次进行中禁用重复提交。每个文件说明成功、重复或失败（以接口确实可知的结果为准），失败不抹去已成功的结果。完成后刷新列表和全库统计，保留筛选条件。
3. 合成文件覆盖有效图片、重复项与无效文件混合，验证原件、登记/禁用状态不变，分页、筛选和批次结果一致。补浏览器交互验证并更新学习对照表。

## 不做

不增加持久队列，不修改默认登记策略，不覆盖原件，不删除资源，不调用真实模型或操作正式实例。所有编辑和专项只在独立开发工作树进行。

## 自检清单

- [x] `test_emoji_batch_upload_cancel_mixed_results_and_filter_persistence`：取消零请求；执行时再次提交仍只发每文件一次；混合批次逐文件显示上传、重复和失败结果。
- [x] `test_emoji_sequential_upload_keeps_existing_metadata_after_bad_file`：真实 API 上重复上传不改原件、来源、计数及登记/禁用状态；坏文件返回 422，之后的有效文件照常入库。
- [x] 在独立开发树用原项目虚拟环境运行 `pytest -q tests/web/test_server.py tests/web/test_resource_browser.py -k emoji`：7 passed，42 deselected；运行前打印 `aureus.__file__` 确认为开发树。`ruff check`、`ruff format --check`（两份 Python 测试）、`node --check` 通过。对照表保留上游独立批量接口、直接登记上传与永久删除的差异；本步提交后由主会话复核，再决定合并和重启。
