# Phase 3.11 维护：学习页分别按来源和授权会话浏览

状态：完成（代码复审通过；正式发布以交接记录为准）
设计：D100；保留[按明确绑定浏览](learning-session-filter.md)的既有行为。

## 先读

[D100](../10-decisions.md#d100--会话标记来源召回不按会话分库2026-11-24)、[会话分批与提示词证据](language-session-evidence.md)、[学习对照](maibot-learning-parity.md)、[开发守则](../11-dev-guide.md)。

MaiBot 1.3.15 固定提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的[模型](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/common/database/database_model.py)、[表达接口](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/webui/routers/expression.py)、[黑话接口](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/webui/routers/jargon.py)。表达 `session_id` 和黑话 `session_id_dict` 用于上游作用域，网页维护接口可改；即使原字段存在，也不是与当前 Aureus 会话等价的原始学习证据。原始黑话证据引用是平台与消息号；当前迁入投影没有从这些引用到可信完整路由的映射。语言包的来源名称和 `targets` 也不是可信路由。

## 接口与行为

- 黑话与表达默认浏览全部会话；「来自会话」只以在线学习保存的完整可信路由键匹配；「授权会话」只以语言包明确授权匹配。两项都选时取交集；筛选不改变召回或授权。来源未确认的迁入、手填和语言包记录仍在不按来源筛选的目录中按原状态浏览；未映射的包仍用「显示停用和待映射」查看。详情将来源证据、已确认来自会话、包内所载名称和包授权分开呈现。
- `GET /api/resources/learning` 增加 `origin_conversation_key`、`authorized_conversation_key`，与文字、状态、审核和移除筛选组合后再计数分页。已有 `conversation_key` 的在线来源或包授权并集不变；未识别的键沿用空结果规则。新增来源会话列表包含可信收件箱会话（含合法临时私聊），与仅列已知群聊和普通私聊的包授权列表分开。现有 `/api/resources/learning/scope` 的响应和写入约定不变。
- 确认选项前不使用旧会话；会话选项读取失败不影响按全部会话浏览。切换任意筛选回第一页，过时响应不能覆盖新选择。

## 子步骤

1. 用合成在线、迁入和语言包原件覆盖黑话与表达的两个筛选、旧 API、组合计数分页和实际召回不变；先看旧版失败。
2. 实现两个筛选和详情文案；在 390 像素 Chrome 验证会话重载、分页、空态和筛选独立。

## 自检清单

- [x] `test_learning_origin_and_authorization_are_independent` 覆盖黑话与表达的来源、授权、交集、旧接口、同名群及临时私聊。旧版两项均误计 6 条，预期 2 条；修复后通过。保留迁入证据，浏览前后资料与召回一致。
- [x] `test_learning_conversation_filter_replaces_pages_and_ignores_stale_responses` 和 `test_learning_scope_reload_disables_unconfirmed_choices` 在 390 像素 Chrome 验证两项标签、总页数、清空一项保留另一项、空态、重载失败与迟到响应。
- [x] 学习、资源、浏览器、语言包、存储、运行时与迁移联合专项共 59 项通过；改动文件 Ruff 格式和规范、Pyright、Node 语法检查及 8 条架构规则通过。没有改动迁入库、召回、导出和原件。

## 完整检查

在独立合成数据根运行 `python scripts/check.py`：pytest 2779 通过、3 失败、4 跳过。失败命中先前记录的 QQ CLI 进程作用域、冷处理次数和 Vault 供应商更名用例。全库 Ruff 规范和架构规则通过；仍有两处未改文件格式差异。脚本的 Pyright 受独立工作树空虚拟环境影响；指定已有开发环境 `--venvpath` 复检后只剩既有的 matplotlib、pandas、streamlit 共 4 项诊断。未修改这些问题，不能称全量通过。日志：`E:/Temp/aureus-learning-origin-ee75d42decc64ef68081bb179dc4d476/check.log`。

## 不做


不猜测、不建立历史会话映射，不回填正式库。不改变包授权、召回、学习、导出、模式和权限；不操作主线、正式实例、数据或推送。

## 待设计问题

本轮没有既有迁入行能与当前 Aureus 可信会话核对的来源契约；以后若有可验明路由的来源映射，另行审定和验收，不将当前线上来源筛选误写成已覆盖该历史映射。
