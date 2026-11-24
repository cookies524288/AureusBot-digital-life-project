# 3.4 / 3.11 后续：表达向量聚类计算

状态：待评审

设计：D40、D80、D100；[学习对照表](maibot-learning-parity.md)。

## 先读

- [开发守则](../11-dev-guide.md)、[学习对照表](maibot-learning-parity.md)、[工作记忆与资源管理](3.11.md)。
- MaiBot 1.3.15 公开提交 `062d45cf8bd4272ac57d7ec03fcf405e5a458211` 的 [表达向量索引](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/chat/replyer/expression_vector_index.py)、[表达选择](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/chat/replyer/maisaka_expression_selector.py)、[表达 API](https://github.com/Mai-with-u/MaiBot/blob/062d45cf8bd4272ac57d7ec03fcf405e5a458211/src/webui/routers/expression.py)。仅参考行为，不复制代码。
- `aureus/memory/language.py`、`aureus/memory/language_matching.py`、`aureus/heartbeat/conversation.py`、`aureus/organs/llm_adapter.py`、`aureus/web/resource_api.py`。

## 已有、未有和待决定

- Aureus 已有按话题、回复意图、说话者、避用词和预算的文字候选；现有 `text_embedding` 器官提供有序向量及 `embedding_space`，但表达选择不调用它。学习网页已有搜索、启用状态、审核、移除和会话过滤；没有表达簇、成员列表及向量空间端点。记忆和表情向量不是表达向量。
- 上游有独立的情景＋风格向量、内容指纹、按模型空间隔离的余弦 k-means、补建、最近簇候选、成员/中心相似度与多样性排序。公开默认关闭向量，索引中的向量和簇成员独立展示。实际部署启用状态不从源码推断。
- 本步只做可独立调用、可用合成表达验证的余弦聚类计算及候选投影。不读本机数据、不自动调用器官、不接网页或正常回复。将用户已学表达（含可能来自私聊的表达）送到真实模型以补建，以及启用生产向量召回与空间删除，须由维护者决定。聚类浏览不得据 D100 扩大网页授权。

## 目标和验收

- 使用 `LanguageEntry.detail`（情景）与 `LanguageEntry.text`（风格）形成真实向量输入，压缩空白；不把例句、来源消息、说话人、范围或其它资源当作嵌入材料。
- 用实际向量方向进行余弦 k-means，归一化样本和中心；相近向量可同簇，变更向量应改变成员。每次计算只接受一个器官批次声明的空间，不合并多个空间；标识为空、长度不一、全零或非有限向量不发布结果。
- 按表达稳定键关联向量。同名异情景不覆盖；成员投影使用实时表达内容并检查指纹。停用、移除或拒绝的表达不进入结果；重新启用和恢复遵从原状态。保留来源标识，展示筛选与召回授权是两套条件。
- D100：非包记录可跨会话召回，包记录只按现有明确绑定候选，待映射或撤销不召回。来源不作为回复候选过滤条件，包声明不视为授权。现有回复内容和防泄漏提示本步不改。

## 改动与接口

- `aureus/memory/expression_clusters.py`：表达输入、暂存快照、余弦聚类与有效成员投影。调用者显式提供异步批量嵌入函数，返回同一空间及有序向量；聚类在工作线程执行。不新增依赖、配置、写库、后台任务或运行时接线。K 由调用者显式提供，测试验证真实簇，不把上游自动选 K、增量重聚类或候选精排记作已完成。
- `aureus/memory/language.py`：为表达文字召回和聚类成员投影提供同一个当前可用候选入口；不改变文字召回行为。
- 每次使用固定随机种子和稳定键排序初始化。参考上游余弦距离加权选初始中心、按中心余弦相似度分配，再归一化更新；不用同一随机数实现或 NumPy，不要求簇编号与上游相同。快照只带空间、成员键及指纹，不保存原文或向量，不是可供回复精排的向量索引。实际精排还须先核对当轮可用候选和查询向量空间，不能让无权条目通过聚类绕过授权。
- 构建调用默认无嵌入传输授权；调用方必须显式传入对该嵌入器获准发送的表达键。调用前再与目标会话下当前可用的表达取交集；目标会话不指定时，不发送包记录。D100 和明确会话绑定都不等于传输授权。已发出的请求不能靠事后撤销收回；当前快照投影仍独立复核召回权限。
- 有效反向向量同簇相消为零中心时，明确报错并不产出快照，不随机选方向或偷偷增加簇数；MaiBot 1.3.15 的 `_build_cluster_centers_from_labels` 同样拒绝零中心。
- `tests/memory/test_expression_clusters.py`：确定性假器官、非文本分组、向量合法性、原件状态和会话边界。

## 子步骤

1. 建立表达向量聚类及实时有效成员投影并运行本步专项，再更新对照表和交接。完整检查留给主会话冻结后统一做，不合并、不重启。

## 自检清单

- [x] `test_expression_clusters_follow_vector_direction_and_keep_homonyms` 验证假器官仅收到标准化情景与风格、交换向量改变聚类；`test_expression_clusters_reject_invalid_embeddings` 覆盖空空间、数量与维度、零向量和非有限值。
- [x] 同名异情景保留不同键；`test_expression_cluster_recall_rechecks_state_and_content` 验证停用、移除、拒绝、恢复与编辑的即时效果。
- [x] `test_expression_cluster_recall_keeps_source_separate_from_package_scope` 验证跨会话在线候选、原始来源浏览、语言包待映射/授权/撤销；现有 `tests/test_language_runtime.py` 检查正常回复请求边界。
- [x] 聚类、语言库、语言运行共 27 项通过；返工文件的 Ruff、Pyright、8 条架构规则及差异检查通过。红测夹具为 `E:/Temp/aureus-expression-red-62af062-d3a6b80c`，绿测夹具为 `E:/Temp/aureus-expression-green-62af062-9e2b521a`，防误用的数据目录均未创建。未跑全量。

## 坑与不做

只用隔离工作树和合成夹具。不上真实模型、QQ 或正式数据；不将此计算步骤称为已接成用户功能。不处理审核自评、上游模型探针、多空间持久化/维护、全库回填、簇网页、自动选 K、回复候选精排或上游空结果回退。

## 待设计问题

正式发送既有表达给哪个模型、预算及触发条件，和是否启用正式回复切换或删除派生空间，留待维护者决定。本步不依赖这些决定。

## 验收意见

### 第 1 轮修改说明（GPT，2026-11-24）

- `test_expression_clusters_do_not_embed_without_explicit_permission` 在 `62af062` 上先失败：假器官收到本地可召回表达及仅绑定会话的语言包。修复后未显式传入发送许可时不调用嵌入器；`test_expression_cluster_embedding_inputs_require_live_scope_and_permission` 捕获实际入参，确认待映射、未指定会话、错会话、已撤销、停用、移除、拒绝及未列入许可的材料均未发送。
- `test_expression_clusters_reject_zero_center_from_valid_opposite_vectors` 验证同一对有效向量分到两簇时可用、放入同一簇时重复构建均明确报零中心错误，不产出快照。上游也拒绝零中心；未增加兜底簇或真实模型默认。
