# 核心启停测试与工作记忆检查点

状态：待评审
设计：[D41](../10-decisions.md)、[3.11 工作记忆](3.11.md)

## 先读

[D41 启停契约](../10-decisions.md)、[3.11 工作记忆检查点](3.11.md)、`tests/core/test_runtime.py`。

## 目标和验收

- 首次唤醒只写一条 `woke_up`，并单独验证首次初始化工作记忆写入的检查点；不忽略其他事件。
- 正常停机后能释放数据库文件；重新打开同一数据目录时，启停记录严格按 `woke_up`、`shut_down`、`woke_up` 排列，既有工作记忆检查点不会重复写入。
- 用假时钟验证 `shutdown_recorded` 为真，停机时长正好一小时。

## 子步骤与自检

- [x] 在 `17aa242` 上复现 `test_assembles_with_fake_organs_and_closes_cleanly` 和 `test_restart_reports_downtime` 的断言失败：初次唤醒比预期多一个检查点，重启后整表序列也多这个检查点。
- [x] 两个目标用例严格核对事件顺序和次数；首次停机后删除夹具数据库文件以验证释放，重启后验证 `shutdown_recorded` 和一小时的精确时长。`tests/core/test_runtime.py` 共 14 项通过。
- [x] 改动文件 Ruff 规范、格式检查与 `git diff --check` 通过。提交只包含本任务书和 `tests/core/test_runtime.py`。

## 不做

不改生产代码、本地 `main` 或远程，不操作正式实例或数据，不调用真实模型。本次按交接要求仅跑专项，不重跑全量检查。

## 待设计问题

无。
