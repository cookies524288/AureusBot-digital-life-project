# 路线图 3.14 补充：表情完整性预览

状态：待实现
设计：延续 [3.14 表情库](3.14.md)和[学习功能对照](maibot-learning-parity.md)的原件保留和现有网页授权；不改变上线策略。

## 先读

- [3.14](3.14.md)、[资源管理](resource-management.md)和[开发守则](../11-dev-guide.md)。
- `aureus/memory/emoji.py` 的表情目录与 `read_media`；`aureus/web/resource_api.py` 的认证和同源检查；`aureus/web/static/resource-management.js` 的表情页。
- 上游 [MaiBot 1.3.15](https://github.com/Mai-with-u/MaiBot/tree/062d45cf8bd4272ac57d7ec03fcf405e5a458211) 的公开源码（根许可证 GPL-3.0）。只借鉴检查目的，不复制代码，不推断安装实例配置。

## 源码依据

| 路径 | 上游行为 |
| --- | --- |
| [注册维护](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/emoji_system/emoji_manager.py#L1152) | 启动后等待配置的分钟数，热重载唤醒重置等待。只有打开偷取且未满额或允许满额替换时才扫描 `data/emoji`；跳过已有登记、禁用或已做 VLM 处理的已知路径和过大文件，一轮首次成功即停。登记可能做 VLM 描述及可选内容审查；满额可调用 `emoji`（未配置则 `planner`）决定先取消旧登记再登记新条目，保留旧图片和识别结果，新登记失败也不保证恢复旧登记。公开默认偷取、替换打开，最多登记 64 张，每 10 分钟，5 MB，内容审查关闭（`src/config/official_configs.py:4281` 起）。 |
| [完整性](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/emoji_system/emoji_manager.py#L1106) | 扫描门槛未满足仍尝试检查。遍历数据库表情记录，删除标记无文件、找不到文件的已登记记录及加载异常的可发送记录，重建内存可发送池；保留无文件未登记记录和图片原件。Aureus 只预览当前活跃登记记录与可验证原件之间的差异，不照搬删除。 |
| [原件缓存回收](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/emoji_system/emoji_cache_cleanup.py#L285) | 两套循环均由 [`src/main.py`](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/main.py#L247) 注册。这一循环启用时先清理再等待，公开默认启用、每 6 小时，两项保留期均 30 天。以系统时间计算截止，记录按最近使用、登记、记录时间依次回退，孤儿文件按修改时间。保护已登记记录及其引用路径；可删过期未登记原件、孤儿图片及无文件未登记记录，也会校正无文件标记。Aureus 没有等价生命周期，不能拿收集时间直接补齐。 |
| [网页缩略图](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/webui/routers/emoji/routes.py#L1055) | 单独统计、清理、预热和清空派生 WebP，不等于原件回收；Aureus 没有这个独立缓存。 |

## 目标和验收

学习页表情工具栏只在主动点击时执行整库范围内的活跃登记检查，不受搜索、分页或格式筛选影响。显示检查总数、原图不可用数和最多 30 条问题描述，明确预览不修改数据、没有后台调度或下次运行时间。

## 改动与接口

- `EmojiStore.preview_integrity(limit)`：查询登记、未禁用且未移除的目录记录，按摘要及来源稳定排列；没有摘要（保留的迁入记录）、文件缺失或现有 `read_media` 的内容地址和格式检查不通过，均归为原图不可用。不解码、不估计缓存到期、不遍历无登记原件；预览与发送均不因本接口放宽已有权限。
- `POST /api/resources/emoji/integrity-preview`：复用完成登录和同源的表情入口，库关闭仍返回 409；在线程内检查，返回 `checked`、`unavailable`、`examples`（仅描述字符串），设置 `Cache-Control: no-store`。用 POST 仅为复用同源边界，不写文件、数据库、缓存或运行状态。
- 学习页复用现有弹窗显示结果及错误，禁用重复点击，注明示例上限；无条目或无问题时照常显示 0。无新配置或持久状态。

## 测试与子步骤

1. 用假时钟和合成文件写接口与窄屏浏览器回归，先在旧代码上确认失败。
2. 实现检查和按钮，更新学习对照，复跑改动专项与静态检查；在独立分支交给 Astra 复审。

## 自检清单

- [ ] 认证、首次改口令、同源和关闭状态仍受原边界保护；响应不缓存。
- [ ] 正常、缺文件、字节不符、迁入无摘要以及禁用、移除、未登记、空库范围正确；检查前后表情表及附件字节一致；示例截断不改变计数。
- [ ] 窄屏只在点击后请求一次，可读结果、无变更提示和无下次运行说明，关闭不再请求。
- [ ] 专项、静态检查和差异检查已记录；准确披露全量检查状态。

## 不做

不做周期调度、自动登记、额度或替换策略、模型请求、合规审查、缓存预热/清理、删记录或删文件。需要维护者另行决定运行频率、真实模型预算、资料发送边界和清理授权；不操作本地 `main` 或正式实例。

## 待设计问题

无阻塞本次只读范围的问题。

## 验收意见
