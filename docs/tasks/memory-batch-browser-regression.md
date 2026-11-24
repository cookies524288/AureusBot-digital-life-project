# Phase 3.11：长期记忆批量移除与恢复的浏览器回归

状态：待评审

## 先读

`docs/tasks/resource-page-ux.md`、`docs/tasks/maibot-memory-parity.md`、`docs/11-dev-guide.md`、`tests/web/test_resource_browser.py::test_memory_batch_remove_and_restore`。遵守 D99：只移除明确选中的项，恢复保留原有状态。

## 实施与验收

用合成资料和临时目录在实际 Chrome 中定位冻结全检已在基线重现的失败。区分操作被遮挡、过期列表、按钮状态错误和测试夹具问题。若单跑通过，检查整文件与基线复现记录，保留可复现的最小条件。修复真正原因，不屏蔽断言、不加固定睡眠、不放宽跨页或权限约束。

### 触发条件与实际异常

旧用例在 `Browser.open()` 发出导航后立即执行 `_run()`；脚本先缓存 `document.querySelector('#editor-dialog')`，再等待页面初始列表。此时新页面的 DOM 尚未完成加载，查询结果为 `null`，第一次确认弹窗断言因此报 `TypeError: Cannot read properties of null (reading 'open')`。旧实现的页面批量操作本身没有被该异常执行到。

旧实现红灯证据：

- `tests/web/test_resource_browser.py::test_memory_batch_remove_and_restore`：失败，异常为上述 `TypeError`；
- `tests/web/test_resource_browser.py` 整文件：`17 passed, 1 failed`，同一异常；
- 初次单独运行目标用例曾通过，说明失败受导航时序影响而非稳定必现；失败发生在初始 `const dialog = ...` 早于首个 `waitFor` 时，相邻通过用例均在页面动作或加载完成后查询弹窗。

修复只调整测试夹具时序：把 `#editor-dialog` 和按钮查询移到首次 `waitFor(() => text('合成记忆000'), ...)` 之后。取消不写、跨页清选、末页选中范围、页数收缩、恢复页数及原停用状态等原有业务断言全部保留；未修改资源页生产代码或批量接口。

### 验证记录

- `tests/web/test_resource_browser.py::test_memory_batch_remove_and_restore`：修复后 `1 passed`；
- `tests/web/test_resource_browser.py`：修复后 `18 passed`；覆盖实际 Chrome、窄屏尺寸和合成 API；
- `tests/web/test_memory_transfer_api.py tests/web/test_learning_package.py::test_language_batch_restores_prior_recall_state`：`5 passed`；
- `git diff --check`：通过；未运行全仓检查，符合本任务只跑定向回归的范围。

旧实现阶段首次单跑目标用例通过，随后整文件运行复现间歇失败；因此将其判定为由导航时序触发的测试夹具竞态，而不是稳定页面行为故障。修复后目标用例、整文件和相关接口回归均通过。

修改文件：`tests/web/test_resource_browser.py`。提交：`3be293f`（修正测试时序）；任务书与交接记录另行提交。

### 自检清单

- [x] 写明触发条件与实际异常；旧实现已用合成资料和实际 Chrome 复现 `TypeError`，修复保留全部批量行为断言。
- [x] 运行覆盖修改的定向用例与直接相关回归：目标用例 `1 passed`，资源浏览器整文件 `18 passed`，记忆数据回归 `5 passed`。
- [x] 提交实现与清单，交由 Astra 复核；未启动正式实例、发送 QQ 消息、调用真实模型或访问用户数据，也未修改无关业务。

### 待设计问题

无。

### 验收意见

待 Astra 复核。
