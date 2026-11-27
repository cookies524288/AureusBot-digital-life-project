# Aureus 学习工作台

固定源码为 MaiBot `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的 dashboard 1.9.9，许可证为 AGPL-3.0。`upstream-files.json` 逐文件记录来源和原始摘要。`upstream-package.json` 与 `upstream-package-lock.json` 保留完整上游依赖来源，实际构建使用当前目录的精简包清单及锁文件。

`public/fonts` 随产物分发固定版的 Ark Pixel、Departure Mono 和 Jersey 20 原字形及各自的 SIL OFL 1.1 文件。代码样式使用系统字体回退，不分发 JetBrains Mono 字体文件。

## 构建

在本目录执行 `npm ci --ignore-scripts`，再执行 `npm run build`。安装需要 npm 缓存或网络。TypeScript 和 Vite 均使用锁定版本，输出到 `aureus/web/static/learning-workbench`。成品通过 Aureus 的 `/learning-workbench/` 提供，同源加载脚本、字体和 API。

Aureus 学习页本身直接显示此工作台：主页面创建 Shadow DOM 宿主，注入 `workbench.css` 并按需加载 `workbench.js`，不再使用 iframe。当前 Aureus 登录同时用于静态资源和 API。

## 适配记录

- `src/main.tsx`、`src/host.css` 和 Vite 配置为 Aureus 宿主，提供中文、内存路由、Shadow DOM 挂载、共享主题和独立样式。
- `src/lib/http/instances.ts` 只连接当前 Aureus，认证失效回到已有登录，移除外部统计实例。
- `src/lib/jargon-api.ts` 的批量定性请求将条目编号 `ids` 和布尔判定 `is_jargon` 放入 JSON 请求体，不放入 URL。
- `src/components/ui/streamline-icon.tsx` 只使用本地登记图标，缺失图标不从外部服务加载。
- 原学习页、表达/黑话/表情业务组件及其 hooks 基于固定版。表达的簇、成员、向量空间和补建进度仍走 `/api/webui/expression`；簇成员使用服务端返回的原件编号。独立质检使用 `/api/resources/learning/quality`，以同一记录的预览版本确认挽回或撤回。
- 列表搜索等停止输入 250 毫秒再发送，保留已选筛选；翻页等待时保留上一页显示。表达库在 SQLite 按当前授权、会话、搜索和移除状态计数、排序并只返回当前页。表情与打开的向量库每 30 秒更新，也可以手动刷新。
- 表达和黑话没有人工回复资格审批。表情卡右下审批保留，独立于登记；同源缩略图缓存维护、旧版数据库导入和其它尚缺接口仍待接入，不能视为已交付或有意移除。

安装包学习 chunk 的核对基线为 SHA-256 `6337b1ad95901d1c7f980c337e951d8315a21f61b87d7783ffce60aad836001f`。原发布包没有源映射，本模块以固定 TSX 重建，界面和业务适配后不使用其二进制摘要作为构建产物断言。
