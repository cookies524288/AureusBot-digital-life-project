# 路线图 3.4 / 3.11 后续：表达向量候选进入回复

状态：待实现（维护者已在 D101 明确材料、接收方与额度）
基线：`71ff690`；依照 D100、[表达聚类的逐项发送约束](expression-vector-clusters.md)及[学习功能对照](maibot-learning-parity.md)。

## 先读

- `aureus/heartbeat/conversation.py`、`aureus/memory/language.py`、`aureus/memory/expression_clusters.py`、`aureus/organs/llm_adapter.py`。
- MaiBot 1.3.15 公开提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的[回复选择器](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/chat/replyer/maisaka_expression_selector.py)、[向量索引](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/chat/replyer/expression_vector_index.py)、[配置](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/config/official_configs.py)。只参考行为，不复制代码，不推断正式配置。

## 已核实

- 上游 `use_vector_expression` 默认关。开启且配置嵌入模型后，查询由表达意图与回复参考组成，没有回复参考才使用规划依据。先用当前嵌入空间和当前会话候选过滤持久索引，有效入索引候选少于 10 条则不进入查询向量阶段。向量路径按近簇、候选相似度及 MMR 多样性构建候选，随后可由另一次选择调用挑选；无结果或失败回退普通候选。候选向量材料是「情景＋风格」，不是例句或来信原文。
- 当前 Aureus 的 `ConversationFlow._reply` 将 `_expression_reference` 结果交给真实 `build_reply_request`；选择按话题、意图、说话者、避用和预算，不调用表达嵌入。`LanguageStore.expression_candidates` 合并在线和迁入表达，遵守语言包会话绑定、撤销及审核。D100 只改变内部召回，不给嵌入器传输授权。
- `build_expression_clusters` 的 `authorized_entry_keys` 由独立调用方逐次明确传入，且只针对传入的嵌入器；正式运行没有这个授权来源。聚类快照只存空间、成员键及指纹，不保存向量、授权或查询材料。网页图簇授权只属于展示。
- `text_embedding` 有多条可用模型路线，`EmbeddingOrgan` 可在失败后使用备用路线；响应的 `embedding_space` 要等路线处理成功才返回，不能把它当作发送前的供应商校验。现有网关额度和普通记忆重排也不是表达发送许可。

## 维护者决定（D101）

直接移植固定版的候选、查询构造、索引、选取和回退；按上游时机使用配置的本地文字嵌入模型。不另建逐条发送授权或单模型、每次建索引的费用上限，不把本地嵌入算成收费调用。仅保留已有收费模型总月额度。启用且符合原有审核等条件的候选可送本地模型；不向远程备用路线转发。D100 仍不按来源会话分库。上游可复用代码的文件和修改登记在 THIRD_PARTY；本地字段、预算和异步边界适配单列。不以每轮重新嵌入全库替代持久索引。

## 验收提纲

- [ ] 默认关；开启但没有有效许可、许可目标路线不可用或没有有效索引时继续现有文字候选，不调用表达嵌入器。模型绑定变化不能沿用对原模型的许可。
- [ ] 合成候选证明查询与候选输入分别只含获准材料；有效向量同空间并以实时指纹关联，内容改变、审核拒绝、停用、移除和包解绑即时失效；重复情景/风格不占多个候选。非包资料仍可跨会话使用，目标不是来源筛选。
- [ ] 用假嵌入器和 `httpx.MockTransport` 捕获真实 Replyer HTTP 请求。固定文字输入，交换向量排序可以改变真正注入的表达；关闭、拒绝、撤销或过期时的实际请求也逐项断言。不得以预览、孤立 store 或只测试数值排序替代。
- [ ] 同次回复仍保留原防泄漏提示、避用和字数预算；新路径不自动修改原件或扩大网页可见性。若仅完成精排，不把上游独立选择调用、补建或空间管理视为验收。
