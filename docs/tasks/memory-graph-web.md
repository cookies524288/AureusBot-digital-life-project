# 路线图 3.11 后续：长期记忆关系图只读页

状态：待评审
设计：D99、D100；沿用[关系图只读查询](memory-graph-query.md)的独立授权边界。

## 先读

- [D99/D100](../10-decisions.md)、[开发守则](../11-dev-guide.md)、[关系图只读查询](memory-graph-query.md)。
- `aureus/web/server.py`、`aureus/web/memory_inspection_api.py`、`aureus/web/static/resource-management.js`、`tests/web/test_resource_browser.py`。

## 目标和验收

在长期记忆页添加独立「关系图」分页面板：浏览和搜索实体名与关系谓词，查看只读详情及实体的有向一跳。仅展示当前独立获准的结果；正式设置没有可信授权来源，默认显示「当前没有获准的图谱资料」。

## 接口和边界

- `GET /api/resources/memory/graph` 接收搜索词、可选锚点、偏移和页大小；`GET /api/resources/memory/graph/detail` 按来源键查询详情。两者沿用 `require_ready`，使用 `Cache-Control: no-store`，定位参数不是授权，未知、未授权、失效及非法详情键统一为相同 404。
- 网页仅接受服务端注入的可信授权提供者；生产不接入提供者，始终传空集合。不从客户端、登录状态、库、实例、来源前缀、召回或数据扫描推导授权。只调用 `MemoryIndex.query_graph`、`graph_detail`；当前范围在请求期间变动则丢弃结果，提示刷新。
- 列表沿用已有分页控件并显示当前页、总页数、可见总数。无覆盖与已授权但零命中用不同文字提示。换页、搜索、刷新、离开图谱或切换锚点时使旧列表与详情响应失效；不缓存图谱内容。详情和一跳不展开整图；窄屏仍用单页列表和现有对话框。
- 不新增配置、授权授予或撤销功能、写入、正式数据扫描、导出或召回逻辑。不操作正式实例和 `main`，不推送。

## 子步骤与自检

1. 接入只读路由，验证失效登录、默认无授权、注入合成授权、撤权、隐藏精确匹配及统一详情空态。
2. 接入长期记忆分页面板，窄屏验证可见浏览、总页数、两种空态、切换和撤权后的迟到响应。
3. 跑图谱、网页及 D100 专项，记录检查和独立夹具路径，提交并交 Astra 复审。

- [x] 后端权限边界、定位、分页及撤权：`tests/web/test_memory_graph_api.py` 三项通过。
- [x] 窄屏分页、详情、单跳、空态及旧响应失效：`test_memory_graph_pages_and_discards_stale_results_at_390px` 通过；资源页浏览器整文件 21 项通过。
- [x] D99 移除与恢复及 D100 实际群聊回复请求通过；改动 Python 文件的 Ruff、Pyright、8 条架构规则及 JavaScript 语法检查通过。未操作正式数据、实例、`main` 或远程。

## 实现与验证（2026-11-24）

- 设计 `1542284`，实现 `0d41176`。联合专项运行图查询、图来源、网页服务端、网页检修、操作记录、资源页浏览器及 D99/D100 回归：82 项通过、1 项失败。失败为翻页时计数栏清空改变列表高度，可能触发页容量重新计算。固定计数栏最小高度后，只复跑受影响的 `tests/web/test_resource_browser.py`，21 项全部通过；按范围未重跑全量。
- 使用项目主树现有 Python 虚拟环境，开发树为 `PYTHONPATH`，设置 `PYTHONUTF8=1`，执行 `python -m pytest -q --basetemp <独立目录>`、`ruff check`、`ruff format --check`、`python -m pyright --venvpath <主树>`、`import-linter`、`node --check` 及 `git diff --check`。
- 联合夹具：`E:/Temp/aureus-graph-web-regression-b5dc3bce98e7485f80b33e996f71fd14`；浏览器回归夹具：`E:/Temp/aureus-graph-web-browser-929ba91c1aed4ac0b31ff6c8c6726063`。

## 待设计问题

本步无阻塞。正式图谱授权仍待单独设计和确认。

## 验收意见

待 Astra 复审。
