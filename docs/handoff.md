# Aureus 交接 · 2026-11-26

## 当前状态

D116–D134 与 3.16–3.53 的全部任务已完成、复审通过并合入本地 `main`，已普通推送 `origin/main`（最新 `86d4688`，本地与远程同步）。静态检查 ruff、lint-imports、pyright 全部通过。维护者实机验收尚未进行。

## 新会话从这里开始

1. **实机验收**：用根目录 `Aureus.exe` 重启，按下面「观察点」逐项看。
2. **可选后续**（按优先级，均未批准实施）：
   - [3.52](tasks/3.52-model-test-and-reply-latency.md) 调查里未做的三项：短回复跳过 LLM 分段并降低打字延迟、按会话并发或投递与下一轮规划并行、流式首包超时 + 阶段计时 + 上下文瘦身。
   - 学习工作台的实时刷新（3.43 遗留：工作台使用自己的 React Query 缓存，`resources` 事件目前只刷新长期记忆页）。
   - 预取开关做进设置页（3.27 遗留，目前只有 `localStorage` 与 `__aureusSetPagePrefetch`）。
   - 滞后页面脚本测试（维护者 2026-11-26 决定暂不更新）。
3. 推送已完成，无待推送。

## 本轮完成（分组，详情见各任务文档）

- **模型调用**：D132/[3.30](tasks/3.30-model-timeout-and-test-policy.md)/[3.31](tasks/3.31-model-calling-alignment.md)（90 秒超时、默认非流式、共享连接、瞬时失败重试一次、本地网关直连）、[3.53](tasks/3.53-model-test-align-mai.md)（测速按 MaiBot 移植提示词/工具/图片，独立许可 2 与限流 60）、[3.51](tasks/3.51-model-config-backend-fields.md)（启用/分时价格/思考/联网/额外参数）与 [3.36](tasks/3.36-model-page-redesign.md)（模型配置页按 MaiBot 布局）。
- **学习与记忆**：D122/[3.17](tasks/3.17-learning-inline-workbench.md)（学习页内联工作台）、[3.18](tasks/3.18-memory-page-redesign.md)（长期记忆页重排）、[3.37](tasks/3.37-memory-title-cleanup.md)、[3.38](tasks/3.38-learning-title-unify.md)、D130/[3.28](tasks/3.28-person-names-and-memory-prefix.md)（联系人命名与记忆前缀清理）。
- **平台会话**：D123/[3.19](tasks/3.19-inbox-activity-panel.md)（活动面板、去人工输入区、加宽）、[3.41](tasks/3.41-inbox-image-preview.md)（图片预览）、[3.42](tasks/3.42-pixel-back-icon.md)、[3.44](tasks/3.44-inbox-list-height.md)、[3.46](tasks/3.46-inbox-media-cache.md)、[3.47](tasks/3.47-typing-becomes-message.md)、[3.48](tasks/3.48-circular-avatars.md)、[3.43](tasks/3.43-live-updates-no-refresh.md)（WebSocket 实时同步、删除刷新图标）。
- **时间线**：D124/[3.20](tasks/3.20-timeline-detail-formatting.md)（详情中文化、筛选条移到底部）、[3.35](tasks/3.35-timeline-filter-scroll.md)（切换筛选不跳顶部）、[3.39](tasks/3.39-timeline-title-bottom.md)（标题吸顶）。
- **性能与动效**：D129/[3.27](tasks/3.27-web-performance.md)（二次进入 0–13ms、收件箱索引 2.7s→110ms）、D133/[3.33](tasks/3.33-page-transition.md)/[3.21](tasks/3.21-scrollbar-hover-expand.md)/[3.34](tasks/3.34-learning-fixed-gutter.md)。
- **其它**：D126/[3.24](tasks/3.24-ledger-lock-retry.md)（账本锁重试）、D127/[3.25](tasks/3.25-model-stream-fallback.md)、D128/[3.26](tasks/3.26-local-embedding-latency.md)、D125/[3.22](tasks/3.22-auto-index-building.md)、[3.49](tasks/3.49-restart-and-launcher-attach.md)/[3.50](tasks/3.50-web-restart-overlay.md)（重启与启动器附着）、[3.40](tasks/3.40-disable-password-autofill.md)、默认提示词开头改为三行数字生命声明。

## 测试基线

- 全量 pytest（2026-11-26，`E:/Temp/release-full.log`）：122 失败、3223 通过、2 跳过。其中 2 项表达式向量运行时用例已随测试替身修复；剩余 120 项为已知基线：109 项 `tests/web` 旧页面脚本测试（与新 DOM 不一致，维护者决定暂不更新）、11 项 QQ 通道/迁移既有基线。
- 静态检查：`ruff check .`、`lint-imports`、`pyright` 全部通过。
- 前端改动按维护者要求未跑单项 pytest，用隔离实例浏览器手测与截图（`E:/Temp/ui-shots/`）；真实 QQ 会话未验证。

## 环境与坑

- 项目 `.venv` 已重建，基础 Python 在 `E:\Tools\uv-python\cpython-3.13.18-windows-x86_64-none`。
- 正式实例当前由源码直接启动在 28976（无托盘）。要改用 `Aureus.exe`，先停掉它，否则单实例锁会拒绝；新启动器会附着已运行实例。
- 多个隔离测试实例可能遗留在临时数据根（`E:/Temp/aureus-ui-review*`，端口 1877x）；与正式实例无关，可随时停止。
- `.claude/launch.json` 有多个临时配置（未跟踪）。
- `E:\Temp\pytest-of-cooki\pytest-current` 是无权访问的联接，跑 pytest 用 `PYTEST_DEBUG_TEMPROOT` 指到独立目录。
- 推送 GitHub 需要本机代理：`git -c http.proxy=http://127.0.0.1:7897 -c https.proxy=http://127.0.0.1:7897 push origin main`。

## 维护者实机验收观察点

- **模型**：模型测速不再大面积失败；MAGPIE 本地网关测速不超时；模型配置页可按厂商筛选、启用、编辑分时价格/思考/联网/额外参数。
- **回复**：群里连发消息只回一次；回复先出；判断不阻塞。
- **平台会话**：图片显示缩略图并可点开大图；输入中显示右侧 Bot 气泡、完成后原地变文字；头像圆形；左右两栏等高；不再有刷新图标，新消息自动出现。
- **时间线**：标题吸顶；详情是中文可读信息；底部筛选切换不跳顶部。
- **学习/记忆**：学习页是内联工作台、只有一层标题；长期记忆页无小字说明、无「未命名人物」与内部序号。
- **其它**：切页有过渡动画且很快；设置页可打开配置文件夹；点重启有遮罩并自动恢复。

## 正式实例与回退

正式数据在项目默认 `aureus_home/`。最近一次实机接管证据是 2026-11-25 重建 `.venv` 后的启动；本轮改动尚未实机验收。按 D104，只提醒维护者自行重启，智能体不代操作、不另启正式实例。切换前的恢复资料与旧指针仍按[不丢数据的步骤](default-home-cutover.md#5-回退不丢数据)处理，不直接覆盖。
