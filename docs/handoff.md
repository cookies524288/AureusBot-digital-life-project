# 会话交接文档

更新于 2026-11-22。当前根目录为 E:\WorkShopE\Projects\AureusBot-digital-life-project。

## 下一步

请 GPT 6.2 Astra 先评审，再结合 docs/08-roadmap.md 规划下一步。功能提交 a515ab6，整批评审范围可用 git log --oneline d8f80c4..a515ab6 查看。远端同步状态以 git fetch 和 git status -sb 为准。

1. 对照 [启动器](tasks/windows-launcher.md) 和 [模型目录与项目标识](tasks/provider-catalog-logo.md) 评审。重点核对重复启动、网页退出确认、CLI 和托盘重启、单实例锁释放与端口发布。
2. 核对模型诊断的逐项输入、回复、失败和预算记账。LLM 总测工具调用，多个模型可提交，实际调用在安全网关串行。供应商测速为无凭据 HTTP HEAD 往返。请用模拟供应商覆盖并发点击、局部失败及按钮状态恢复。
3. 检查网页更新期间的文字选区、子标签局部淡入、器官拓扑与 CORE、设置释义及两秒测速提示。登录页与启动器用平滑原版，标签页用带圆环图标，侧栏与托盘用像素版。
4. 将评审意见写进对应任务文档。2.12 和 2.12b 仍需正式评审，2.12c 已完成，2.13 尚待规划。

## 验证记录

当前项目根目录执行 uv run --no-sync python scripts/check.py，pytest 使用独立临时目录。首轮发现 tests/web/test_server.py 的导入排序和长行格式问题，修正后完整复跑通过。Ruff 格式与规范通过，Pyright 零错误，8 条架构规则全部通过，pytest 2330 通过、1 跳过，用时 347.22 秒。未真实调用模型，最终视觉体验与供应商兼容性待评审。

本次收尾未纳入用户原有的两份 README 修改与 .claude/ 本地工作树。