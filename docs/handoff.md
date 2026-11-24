# Aureus 交接 · 2026-11-24

## 下一步

3.15 仍在实施。以[路线图](08-roadmap.md)、[逐项台账](tasks/3.15.md)和各子任务的验收意见为准。先查 `git status -sb`、`git log --first-parent -8 --oneline` 与 `git worktree list`，不要从已合入的小步重新开工。

- [设置、切页和身份](tasks/3.15-management.md)：切页首帧 `85c92ce`、设置搜索与中文说明 `d7c9f19`、私聊实发分段/补读 `680e255` 及身份称谓 `145317e` 已合入。
- [增量总结](tasks/3.15-memory.md)：返工版 `ad2c612` 随 `f0dc456` 合入，QQ 和网页私聊分别建窗与提交。组合专项 24 项通过，未用真实模型。
- [长期记忆与学习](tasks/3.15-resource-console.md)：分区工作台与连续翻页修复 `aacba11` 已合入；按[质检](tasks/3.15-expression-quality.md)、[事实/画像生命周期](tasks/3.15-resource-lifecycle.md)和[包互通](tasks/3.15-bundles.md)续做。**只暂缓关系图的新 UI 设计**；`070cf17`、`e8c0641` 保留且默认无授权，关系数据管理和后端权限照常。
- [平台收件箱](tasks/3.15-platform.md)：HTTP 500 修复 `7c1ab29`、按批阅读 `131f668`、会话桌面 `c70b09f`、结构化组件 `c832de0` 已合入。另已合入[显式查已读历史](tasks/3.15-history-actions.md) `2e5fbbd`、文字/引用/@/戳 `01ed1ee`、附件 `51d410b`、头像 `4421157`。继续旧历史的受控清理，以及[普通图片描述与会话输入状态](tasks/3.15-live-conversation.md)。
- [判断与生化参数](tasks/3.15-feedback.md)：同轮状态输入 `365569b`、网页与 API 只读保护 `2407e0a` 已合入。[公开交付](tasks/3.15-release.md)的最终隔离全量和远程推送尚未完成。

## 正式实例与回退

最近有接管证据的是 `133d0c7`，不是当前主线。其后的本地合入和专项结果不代表已实机验收。第 17、18 项分别是两处提示独立显示五秒 `9b3e3e9`、仅观察页统一为「仅用户可见」标签 `9c29811`。按 D104，只提醒维护者可用根目录 `Aureus.exe` 重启；智能体不代操作，不另启实例，也不因等待反馈而停止开发。重启入口的[只读调查](tasks/3.15-parity.md)已完成。

正式数据在项目默认 `aureus_home/`，迁移时校验过 5752 个文件。停机源、切换前的原默认目录、旧指针和七份中断工作树均属恢复资料，不是临时文件。原检出可用 `git rev-parse --git-path info/aureus-recovery-locations.md` 定位不随仓库分发的本机索引；中断工作树也可用 `git worktree list` 定位。切换之后的新增经历只在当前数据根，回退请按[不丢数据的步骤](default-home-cutover.md#5-回退不丢数据)，不要直接覆盖或恢复旧指针。
