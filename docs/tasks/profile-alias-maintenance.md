# 长期记忆：人物画像人工别名维护

状态：待评审
范围：路线图 Phase 3 的长期记忆追加核对；沿用 D99 的可逆原则和 D100 的现有召回授权。

## 先读

- [长期记忆核对表](maibot-memory-parity.md)、[3.3 画像追加交付](3.3.md)、[D99/D100](../10-decisions.md)、[开发守则](../11-dev-guide.md)。
- MaiBot 1.3.15 固定提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 内置 A_Memorix 2.0.0：[管理服务](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/runtime/services/profile_admin_service.py)、[存储](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/storage/metadata_profile.py)、[画像服务](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/A_memorix/core/utils/person_profile_service.py)。

## 目标和边界

固定版 `get_aliases`、`set_aliases`、`delete_aliases` 维护独立集合；实际画像读取 `effective_aliases`。直接移植其别名校验和去重规则：去首尾空白、忽略空行、大小写折叠去重并保留首次写法，去重后 1–100 项，每项最多 128 字。沿用人工完整覆盖、撤销后回到既有值的语义。上游可自动排入画像刷新，并可用昵称或群名片解析首个匹配的人；这里不移植这两条路径。

- 人物画像页仅给已由实例内来源键和明确账号关联的画像原件提供入口。网页从原件编号定位到 `(instance, subject_key)`，不能凭显示名、昵称、人物编号或任意文本另建身份；同源不同版本共用这一份本地覆盖。待关联原件和只有人物档案、没有来源画像的人保持只读。
- 采用单独的本地维护表，不改迁入原件及证据，撤销只删除本地覆盖。无本地覆盖时仍是迁入人工集合优先（包括既有空集合），否则取最新且仍有效的自动画像别名。画像正文及有效期规则不变，不把撤销变成启用过期画像。
- 既有登录会话可查看；写入使用现有登录和同源校验。管理接口 `GET/PATCH /api/resources/profiles/{profile_id}/aliases` 返回原有别名、本地覆盖（无则 `null`）和当前别名；`PATCH` 的 `aliases: null` 表示撤销。没有可核验目标的读取和写入均拒绝。
- 仅现有账号命中后的人际资料检索读取本地维护层；不改变说话人账号解析、召回范围或规则、文字检索排名，继续使用现有人际资料内部参考和字符预算。`people.profile_alias_maintenance_in_reply_enabled` 可单独关闭本地覆盖在回复里的使用；人际资料总开关不变。网页仍可维护。

## 子步骤与自检

1. 先用合成数据运行包含网页接口和实际回复路径的回归，确认旧版预期失败。
2. 接入存储、网页和原有检索，按维护者最新范围只运行基本检查。不合并、不重启、不修改交接或正式数据。

- [x] 登录、同源、待关联与无效输入限制；不同版本共用覆盖，撤销保留原件和证据。`test_local_profile_aliases_reach_only_the_exact_accounts_reply` 通过。
- [x] 同一测试检查假器官的实际回复请求：同名账号不串用别名，数据库重新打开后仍使用本机值，撤销后真实请求恢复原有值，关闭本机覆盖的读取时仍返回原有值。
- [x] 该回归在旧版因别名读取端点返回 404 而失败，接线后 1 项通过；改动 Python 文件的 Ruff 格式和规则、`node --check aureus/web/static/resource-management.js`、`git diff --check` 通过。未运行浏览器或全量检查。

## 手动复核入口

仅使用独立测试实例及合成数据。在「长期记忆 → 人物画像」打开已有且关联明确账号的画像，从详情点「维护别名」。核对人物编号和画像来源，输入一行一个别名并保存；重新加载页面并打开同一画像，核对当前值仍在。点击「撤销本机别名」，应恢复原有值；另一同名账号的别名不变，未关联画像没有维护按钮。可在 390 px 的浏览器视口复核这一流程。本次不执行浏览器或全量检查。

## 不做

不补别名建议、按别名身份识别、画像刷新及模型调用、画像正文写入、证据纠错、导出、图查询或新授权。不操作正式实例。是否支持以上行为需另外确定身份和材料权限，不能从 D99/D100 推导。

## 待设计问题

本小步无；昵称解析与画像刷新明确不在范围内。

## 验收意见

待 Astra 复审。
