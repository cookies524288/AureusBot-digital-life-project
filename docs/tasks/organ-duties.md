# 十项器官职责与接线

状态：待评审

## 职责对照

| 职责 | 配置和候选 | 运行入口 |
| --- | --- | --- |
| `replyer` | `organs.replyer`，对话模型；默认由 `[heartbeat] reply_organ` 指向 | `ConversationFlow._reply` 生成对话回复，主动回复沿用相同绑定。 |
| `planner` | `organs.planner`，对话模型；默认由 `[heartbeat] plan_organ` 指向 | `ConversationFlow._plan` 按档位规划，主动发言也可调用。 |
| `memory` | `organs.memory`，对话模型；清空时沿用旧 `summarizer` | `MemoryConsolidator.run` 在提取开关启用、有材料且符合夜间整理条件时，从原文提取事实和偏好，保存前校验证据。 |
| `utils` | `organs.utils`，对话模型；既有任务绑定优先 | 小探索摘要、日记、人设成长和语言学习仍按各自守则启动。当前任务的专职器官未配置对话模型时，心跳把实际调用交给 `utils`。 |
| `fast_model` | `organs.fast_model`，对话模型；`[judge] organ` 有对话模型时优先 | `ConversationFlow._judge_emotion` 满足判断配置和触发条件时调用，专职判断器官空置时使用此模型顺序。 |
| `vlm` | `organs.vlm`，开启 `vision` 的对话模型 | `InboxMedia` 在心跳处理 QQ 来信时读取已缓存图片，生成给本轮对话的描述。 |
| `voice` | `organs.voice`，OpenAI 兼容服务商的 `transcription` 模型 | `InboxMedia` 在心跳处理 QQ 来信时获取转码音频，生成给本轮对话的转写。 |
| `embedding` | `organs.text_embedding`，`kind = "embedding"` | `ConversationFlow._rerank_memory` 对检索词和已召回候选做向量排序。停用或失败时沿用关键词召回顺序。 |
| `image_embedding` | `organs.image_embedding`，`kind = "image_embedding"`，Jina v4 | `inbox_images` 给入站图片建立向量，资源管理页使用已保存向量查找相似图。 |
| `web_search` | `[exploration] search_provider = "searxng"` 和 `search_endpoint`，默认关闭 | 小探索通过 `SearchProvider` 接口搜索；搜索服务配置跟随运行设置重启生效。 |

## 核对

- `test_text_roles_use_configured_routes_across_reload` 用假器官验证记忆绑定、专职优先、通用备用和清空恢复。路由发生在安全网关检查与记账之前。
- 器官关系与提示词目录根据保存后的模型路线显示任务分配。任务启用条件仍由各模块执行。
- 图片、语音、嵌入和搜索使用各自模块的模拟传输或合成数据验收。媒体接线见 `4e9928f`，嵌入见 `16f1d86`，相似图片见 `95f2746`，搜索见 `6246fe3`。
