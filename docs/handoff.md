# Aureus 交接 · 2026-11-24

## 正式实例

正式实例最近确认的运行代码为工具协议 `66db7ec`（包含总览修复 `6c4d0b0`）。维护者点击重启后，项目根目录 `Aureus.exe` 的同一启动器 `24708` 和默认数据目录均未变，新内核 `22024` 接管网页 `28976`（首页 200），与 QQ 网关 `23104` 已建立连接，旧内核 `18040` 已退出。PID 只作当次记录，不据此结束进程。没有推送远程。

学习和长期记忆已有分页、缩略图、统一资料目录和可逆移除。实际回复已接入本地表达向量候选（按上游默认关闭）、画像别名及迁入图谱证据。长期记忆保留 JSONL，支持标准知识包导出、兼容 `.amembundle` 的预检、安装、原包下载及可逆卸载；事实、证据、外部引用、画像和别名可随兼容包恢复。**不等于完整移植或通过全量验收。**

<a id="下一步"></a>

## 新会话从哪开始

当前以 [3.15 台账](tasks/3.15.md)、[规划器任务](tasks/3.15a.md)和[固定版差异](tasks/3.15-parity.md)为准。`66db7ec` 的工具协议已换核，不要重复重启；它不等于 planner 已经能按需调用 replyer。`efbdc83` 的 utils 压缩和 `067d3ca` 的资源分页仍在独立分支；以台账的实际接管记录为准。以下是本轮开工时的历史起点。

1. 先看[补充任务汇总](tasks/supplemental.md)，尤其是[长期记忆差异](tasks/supplemental.md#maibot-memory-parity)、[学习差异](tasks/supplemental.md#maibot-learning-parity)和[提示词差异](tasks/supplemental.md#maibot-prompt-parity)。编号任务、[实施约定](tasks/README.md)及证据仍保留；旧专项原文在 `aab708a:docs/tasks/`。
2. 上次开发中断时，**从本地生成完整包、图片 v2 成员接入、在线表达的模型自评**尚未上线。另有向量包成员、关系回收站等差异。不要把已有的知识包导出当作完整包导出，也不要把写入前规则过滤当作模型自评。按维护者的新指示选下一步，不在交接时继续开发。
3. 沿用[项目决定](10-decisions.md) D99–D102：移除可逆；会话是来源标记，允许跨会话内部召回；表达嵌入走本地模型、不设单模型额度；每个可运行小步合入本地 `main`，由原启动器重启同一实例并确认恢复。维护者实机观察，不例行跑测试、不发测试群消息。纯文档提交不重启。

## 数据与回退

正式数据迁移已完成，停机复制时 5752 个文件逐一核对 SHA-256。回退基线位于 `E:/WorkShopE/Aureus-Phase3-XiaoDou/final-06788e4b869f483dae9c3dcbb025b1fb/`：`aureus_home/` 是停机时的源，`project-default-before-cutover-20261124/` 是原默认目录，`aureus-home-before-cutover-20261124.txt` 是旧指针。三者不是临时文件。之后的新记录只写入项目默认目录；如需回退，按[回退说明](default-home-cutover.md#5-回退不丢数据)，不要直接恢复指针或覆盖数据库。

临时开发分支已回收，本地只留 `main`、`backup/1.10-attempt-1` 和 `legacy-history`。中断的图片包工作树没有独有内容，已删除。七份含未提交修改、冲突或本机数据的旧现场已移至 `E:/WorkShopE/Aureus-Phase3-XiaoDou/handoff-recovery-20261124/`，可用 `git worktree list` 定位；不必合入或重放它们。

清理例外：根目录 `Tempaureus-full-e5c875a-20566b75650f47cbbc462d39e0762a3fpytest/` 和 `E:/Temp/` 内本轮四份合成测试目录（`aureus-review-`、`aureus-prompt-integration-`、`aureus-qq-integration-`、`aureus-style-integration-` 加随机标识）删除被执行环境拦截。它们不在 Git 中，不影响小豆运行。
