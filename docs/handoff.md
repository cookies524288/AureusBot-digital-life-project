# Aureus 交接 · 2026-11-24

## 正式实例

正式实例最近确认的功能提交为 `133d0c7`（planner 显式调用 replyer）。同一根目录启动器 `24708` 拉起新内核 `11064`，旧内核 `5952` 已退出；网页 `28976` 首页返回 200，与 QQ 网关 `23104` 已建立连接。未发测试消息，不据此认定真实模型行为已验收。没有推送远程。

学习和长期记忆已有分页、缩略图、统一资料目录和可逆移除。实际回复已接入本地表达向量候选（按上游默认关闭）、画像别名及迁入图谱证据。长期记忆保留 JSONL，支持标准知识包导出、兼容 `.amembundle` 的预检、安装、原包下载及可逆卸载；事实、证据、外部引用、画像和别名可随兼容包恢复。**不等于完整移植或通过全量验收。**

<a id="下一步"></a>

## 新会话从哪开始

当前以 [3.15 台账](tasks/3.15.md)、[规划器任务](tasks/3.15a.md)和[固定版差异与重启入口调查](tasks/3.15-parity.md)为准。**按 D104 只提醒维护者可以重启验收，智能体不代为重启，不等待反馈才继续开发和合入。** 以下是本轮开工时的历史起点。

`main` 至 `fdb1ae9` 已合入工具菜单、资源翻页、表情审批、平台新旧历史、滚动条、设置总重置和临时共情。此后 `50d87c3` 合入显式引用，`7c1ab29` 修复收件箱打开即 HTTP 500（合成空库先复现；主线收件箱接口专项 7 项通过）。只提醒维护者重启验收，不以合入或合成回归代替实机反馈。

后续：分轮时间线 `f99eb29` 与长期记忆原文隔离 `fad6a13` 已有独立提交，尚待交叉复审合入。尤其不能使新的 `reply_delivered` 事件绕开消息原文隔离、从记忆或导出包返回。关系图和单会话阅读按各自任务书继续实施。

1. 先看[补充任务汇总](tasks/supplemental.md)，尤其是[长期记忆差异](tasks/supplemental.md#maibot-memory-parity)、[学习差异](tasks/supplemental.md#maibot-learning-parity)和[提示词差异](tasks/supplemental.md#maibot-prompt-parity)。编号任务、[实施约定](tasks/README.md)及证据仍保留；旧专项原文在 `aab708a:docs/tasks/`。
2. 上次开发中断时，**从本地生成完整包、图片 v2 成员接入、在线表达的模型自评**尚未上线。另有向量包成员、关系回收站等差异。不要把已有的知识包导出当作完整包导出，也不要把写入前规则过滤当作模型自评。按维护者的新指示选下一步，不在交接时继续开发。
3. 沿用[项目决定](10-decisions.md) D99–D104：移除可逆；会话是来源标记，允许跨会话内部召回；表达嵌入走本地模型、不设单模型额度；可运行小步串行合入，维护者自主选择重启时机。纯文档不需要重启。

## 数据与回退

正式数据迁移已完成，停机复制时 5752 个文件逐一核对 SHA-256。回退基线位于 `E:/WorkShopE/Aureus-Phase3-XiaoDou/final-06788e4b869f483dae9c3dcbb025b1fb/`：`aureus_home/` 是停机时的源，`project-default-before-cutover-20261124/` 是原默认目录，`aureus-home-before-cutover-20261124.txt` 是旧指针。三者不是临时文件。之后的新记录只写入项目默认目录；如需回退，按[回退说明](default-home-cutover.md#5-回退不丢数据)，不要直接恢复指针或覆盖数据库。

临时开发分支已回收，本地只留 `main`、`backup/1.10-attempt-1` 和 `legacy-history`。中断的图片包工作树没有独有内容，已删除。七份含未提交修改、冲突或本机数据的旧现场已移至 `E:/WorkShopE/Aureus-Phase3-XiaoDou/handoff-recovery-20261124/`，可用 `git worktree list` 定位；不必合入或重放它们。

清理例外：根目录 `Tempaureus-full-e5c875a-20566b75650f47cbbc462d39e0762a3fpytest/` 和 `E:/Temp/` 内本轮四份合成测试目录（`aureus-review-`、`aureus-prompt-integration-`、`aureus-qq-integration-`、`aureus-style-integration-` 加随机标识）删除被执行环境拦截。它们不在 Git 中，不影响小豆运行。
