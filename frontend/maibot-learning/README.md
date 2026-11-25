# Aureus 学习工作台

固定源码为 MaiBot `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的 dashboard 1.9.9，许可证为 AGPL-3.0。`upstream-files.json` 逐文件记录来源和原始摘要。`upstream-package.json` 与 `upstream-package-lock.json` 保留完整上游依赖来源，实际构建使用当前目录的精简包清单及锁文件。

`public/fonts` 随产物分发固定版的 Ark Pixel、Departure Mono 和 Jersey 20 原字形及各自的 SIL OFL 1.1 文件。代码样式使用系统字体回退，不分发 JetBrains Mono 字体文件。

## 构建

在本目录执行 `npm ci --ignore-scripts`，再执行 `npm run build`。安装需要 npm 缓存或网络。TypeScript 和 Vite 均使用锁定版本，输出到 `aureus/web/static/learning-workbench`。成品通过 Aureus 的 `/learning-workbench/` 提供，同源加载脚本、字体和 API。

产品学习页面中的「打开完整学习工作台」进入此模块，当前 Aureus 登录同时用于静态入口和 API。保留原页面入口供组合验收期间使用。

## 适配记录

- `src/main.tsx`、`src/host.css` 和 Vite 配置为 Aureus 宿主，提供中文、浏览器历史、共享主题和独立样式。
- `src/lib/http/instances.ts` 只连接当前 Aureus，认证失效回到已有登录，移除外部统计实例。
- `src/components/ui/streamline-icon.tsx` 只使用本地登记图标，缺失图标不从外部服务加载。
- 原学习页、表达/黑话/表情业务组件及其 hooks 基于固定版。表达的簇、成员、向量空间和补建进度仍走 `/api/webui/expression`；簇成员使用服务端返回的原件编号。独立质检使用 `/api/resources/learning/quality`，以同一记录的预览版本确认挽回或撤回。
- 表达和黑话没有人工回复资格审批。表情卡右下审批保留，独立于登记；同源缩略图缓存维护、旧版数据库导入和其它尚缺接口仍待接入，不能视为已交付或有意移除。

安装包学习 chunk 的核对基线为 SHA-256 `6337b1ad95901d1c7f980c337e951d8315a21f61b87d7783ffce60aad836001f`。原发布包没有源映射，本模块以固定 TSX 重建，界面和业务适配后不使用其二进制摘要作为构建产物断言。
