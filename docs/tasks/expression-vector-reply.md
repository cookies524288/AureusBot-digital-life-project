# 路线图 3.4 / 3.11 后续：表达向量候选进入回复

状态：待评审（正常回复路径已实现；本轮按授权只做最小离线运行检查）
基线：`71ff690`；依照 D100、[表达聚类的逐项发送约束](expression-vector-clusters.md)及[学习功能对照](maibot-learning-parity.md)。

## 先读

- `aureus/heartbeat/conversation.py`、`aureus/memory/language.py`、`aureus/memory/expression_clusters.py`、`aureus/organs/llm_adapter.py`。
- MaiBot 1.3.15 公开提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的[回复选择器](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/chat/replyer/maisaka_expression_selector.py)、[向量索引](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/chat/replyer/expression_vector_index.py)、[配置](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/config/official_configs.py)。按 D101 移植并登记来源，不推断正式配置。

## 已核实

- 上游 `use_vector_expression` 默认关。开启且配置嵌入模型后，查询由表达意图与回复参考组成，没有回复参考才使用规划依据。先用当前嵌入空间和当前会话候选过滤持久索引，有效入索引候选少于 10 条则不进入查询向量阶段。向量路径按近簇、候选相似度及 MMR 多样性构建候选，随后可由另一次选择调用挑选；无结果或失败回退普通候选。候选向量材料是「情景＋风格」，不是例句或来信原文。
- 当前 Aureus 的 `ConversationFlow._reply` 将 `_expression_reference` 结果交给真实 `build_reply_request`；选择按话题、意图、说话者、避用和预算，不调用表达嵌入。`LanguageStore.expression_candidates` 合并在线和迁入表达，遵守语言包会话绑定、撤销及审核。D100 只改变内部召回，不给嵌入器传输授权。
- `build_expression_clusters` 的 `authorized_entry_keys` 由独立调用方逐次明确传入，且只针对传入的嵌入器；正式运行没有这个授权来源。聚类快照只存空间、成员键及指纹，不保存向量、授权或查询材料。网页图簇授权只属于展示。
- `text_embedding` 有多条可用模型路线，`EmbeddingOrgan` 可在失败后使用备用路线；响应的 `embedding_space` 要等路线处理成功才返回，不能把它当作发送前的供应商校验。现有网关额度和普通记忆重排也不是表达发送许可。

## 维护者决定（D101）

直接移植固定版的候选、查询构造、索引、选取和回退；按上游时机使用配置的本地文字嵌入模型。不另建逐条发送授权或单模型、每次建索引的费用上限，不把本地嵌入算成收费调用。仅保留已有收费模型总月额度。启用且符合原有审核等条件的候选可送本地模型；不向远程备用路线转发。D100 仍不按来源会话分库。上游可复用代码的文件和修改登记在 THIRD_PARTY；本地字段、预算和异步边界适配单列。不以每轮重新嵌入全库替代持久索引。

`[language].use_vector_expression` 默认关闭；开启时从 `[organs.text_embedding]` 选择第一个受支持、未配置价格的回环地址 OpenAI 兼容模型，并锁定实际端点和模型。没有该路线则继续普通回复。独立选择器可在 `[language].expression_selector_organ` 显式绑定，未绑定走上游的直接注入分支。本地器官仍经权限、急停、频率保护和记账，但免费向量不消耗月度付费额度。候选向量由正常回复触发的后台增量补建与 SQLite 派生索引提供；空索引或少于 10 项时不发查询，补建与查询独立。发布结果只接真实 Replyer，展示权限和快照不变。

## 验收提纲

- [x] 默认关；没有本地路线或同空间有效候选不足 10，回退普通候选、不发送查询。当前空间在发请求前锁定，补建为单独后台路径，不阻塞首次普通回复。
- [x] 本轮 `test_local_expression_vectors_reach_actual_reply` 通过合成 `httpx.MockTransport` 检查候选仅含情景/风格、回复参考优先于理由、禁止远程嵌入、重启复用、交换向量改变实际回复请求次序、移除后门槛回退、免费嵌入不占月额度以及同一请求保留防泄漏和避用。
- [ ] 未单独跑默认关闭、热更换路线、审核拒绝、停用、编辑、包撤销或选择器回归矩阵。有效候选入口复用现有审核、移除与包范围；读向量时比对原件稳定键和即时指纹，选取后再核实。依本轮最小验证要求未跑全量，不把未跑的矩阵算作验收。

## 剩余差异

- 上游有模型指纹探针、后台周期性历史补建、在线增量聚类和跨空间文件操作；本轮是正常回复之后每次处理一批缺项，SQLite 保留不同路由的空间且不自动迁移，不探测相同端点与模型名下不改变维度的模型替换。网页空间清理、重建进度、独立浏览授权均未改变。
- 可选独立选择器使用同一表达匹配依据与编号候选，不再另发原始对话；未配置则直接采用前五条并受本地参考字数限制。普通回退仍用 Aureus 的话题、意图、说话者与避用择取，而非上游的随机抽样。
