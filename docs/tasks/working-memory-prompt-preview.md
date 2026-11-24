# Phase 3.11：工作记忆摘要提示词预览

状态：待实现

## 先读

`docs/tasks/maibot-prompt-parity.md` 的工作记忆条目、`docs/08-roadmap.md` 的 3.6a 与 3.11、`docs/11-dev-guide.md`。实际路径见 `aureus/attention/working_memory.py` 和 `aureus/heartbeat/main_loop.py`，预览入口见 `aureus/heartbeat/prompt_preview.py` 和 `aureus/web/prompt_api.py`。

## 目标和验收

补齐器官页 `working_memory_summary` 场景。生产端与预览共用同一个纯请求构造器：`system` 保留原指令，`user` 保留「旧摘要」「经历」JSON。预览传入固定模拟样本，不查时间线，不发送请求；按已有任务路由显示，不混同长期记忆整理。

## 子步骤

1. 在 `tests/web/test_prompt_http_integration.py` 加独立用例 `test_working_memory_preview_matches_partitioned_summary_http_request`。以合成时间线触发 `WorkingMemory.prepare` → `summarize`，经真实适配器及 `httpx.MockTransport` 捕获最终请求。检查原指令、消息角色、JSON、分区隔离和预览不发送请求；用例先在旧代码上因缺少场景失败。
2. 在 `aureus/attention/working_memory.py` 提取纯构造器，经该子系统公开接口导出；让 `prepare` 与网页场景共用。保持请求字段、输出限制、分区、触发、路由和状态写入不变。
3. 复跑网页提示词联测、提示词 API 和工作记忆专项，记录结果与交接。按本轮独立分支任务范围只跑专项，不合入主线或重启。

## 自检清单

- [ ] 联测先失败于缺少场景，实施后通过；模拟 HTTP 捕获请求逐条与预览比较，验证 `system`、`user` 及固定摘要协议。
- [ ] 两份合成摘要请求各只含对应分区材料；预览不发请求、不读真实经历。
- [ ] 工作记忆和提示词相邻专项通过；无 `main_loop.py`、路由、配置或摘要契约改动。

## 不做

不移植 MaiBot 五字段压缩协议，不新增模板编辑项，不访问私有数据、不调用真实模型，不操作正式实例、不合入 `main`、不推送。

## 待设计问题

无。
