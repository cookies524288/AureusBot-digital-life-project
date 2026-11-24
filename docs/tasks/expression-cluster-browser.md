# 路线图 3.4 / 3.11 后续：学习页表达簇浏览

状态：待评审

## 先读

- [表达簇快照](expression-cluster-snapshots.md)、[关系图只读页](memory-graph-web.md)、[D100](../10-decisions.md)、[开发守则](../11-dev-guide.md)。
- MaiBot 1.3.15 公开提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的[表达 API](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/webui/routers/expression.py)：参考独立簇列表、按空间区分同号簇和单簇成员入口，不复制代码或沿用默认展示范围。

## 目标和边界

- 学习页有独立「表达簇」标签，能分页读当前可见的簇和逐簇成员；无快照或无可见成员时显示同一个空态。390 px 宽度下不横向溢出。
- 仅接收服务端提供的当前会话逐表达键网页授权及已打开的 `ExpressionClusterStore`。这两个依赖默认不提供；没有可信授权不能从网页登录、普通学习资料列表、会话来源、语言包召回范围、逐项发送许可或内部召回推导。任何客户端参数都不能授予可见性。
- 读快照后重新读取语言条目；只有获网页授权、存在、已启用、未移除、未拒绝、不待映射且内容指纹仍一致的表达可见。不展示例句、原件来源、证据、作者或向量；过滤后才汇总计数和分页，空簇不出现。
- `GET /api/resources/learning/clusters` 返回分页项、总数和下一偏移；`GET /api/resources/learning/clusters/detail` 用当前快照及簇的定位键读成员。定位键不是授权；未知、失效、隐藏详情统一 404。读请求须是已设置口令且会话有效，返回禁止缓存；异步读后再核查授权和登录，授权变化不得返回旧结果。
- 前端使用现有资源分页控件和详情对话框；翻页、刷新、切换或离开时清除旧可见资料并使晚到的读取结果失效。

## 子步骤和自检

1. 先提交任务书；增加记忆只读投影和两条网页读接口，合成快照覆盖权限、失效条目、分页与权限撤销。
2. 接学习页只读标签和详情，用 390 px 浏览器覆盖结果、总页数、空态及迟到列表和详情。
3. 跑新功能与相邻回归、静态检查，记录结果及夹具路径后提交待复审。

- [x] `tests/web/test_expression_cluster_api.py` 三项通过：未登录及未改初始口令被拒，缺授权时不读取快照；合成快照验证过滤后计数、跨空间隔离、快照替换及撤权；列表及详情都丢弃读取期间被撤权的结果。
- [x] `test_expression_clusters_paginate_and_discard_stale_results_at_390px` 通过：验收 390 px 窄屏、总页数、末页、长成员文本换行、只读请求和撤权空态，旧列表和详情均不得重现。
- [x] 联合专项 52 项通过（17.14 秒），包括新的 API、浏览器整文件、资源 API、图谱 API、快照存取和聚类。改动文件 Ruff 格式及规范、Pyright、8 条架构规则、JavaScript 语法和差异检查通过。未跑全量。

## 验证记录（2026-11-24）

- 基线 `efee879`，任务书 `a9f5e1f`，工作树 `E:/WorkShopE/Projects/Aureus-expression-cluster-browser-dev`（`codex/expression-cluster-browser`）。
- 主树虚拟环境运行 `pytest -q -p no:cacheprovider`，`PYTHONPATH` 指向开发树，`AUREUS_HOME` 指向隔离测试目录内的合成路径。首次专项三项接口通过；浏览器两次失败因测试在学习页容器内找全局对话框，改为查找全局弹窗后加入联合回归并通过。
- 联合夹具 `E:/Temp/aureus-expression-browser-regression-b72fb440f6c04c88b9a2c78a9da26877`；早期接口及浏览器夹具 `E:/Temp/aureus-expression-cluster-browser-3428985cb76942c5915f5c3e4840a43e` 保留待复审。未操作正式实例、数据、主线及远程。

## 不做

不新增正式授权来源、文件扫描、自动建簇、嵌入请求、写入接口、回复改动或快照空间维护。不操作正式数据、正式实例、主线或远程。

## 待设计问题

正式网页授权由谁授予及从哪里读取已保存快照不在本步；缺省为空，不将其描述为已有正式材料可见。
