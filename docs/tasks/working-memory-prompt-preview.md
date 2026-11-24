# Phase 3.11：工作记忆摘要提示词预览

状态：待评审

## 先读

`docs/tasks/maibot-prompt-parity.md` 的工作记忆条目、`docs/08-roadmap.md` 的 3.6a 与 3.11、`docs/11-dev-guide.md`。实际路径见 `aureus/attention/working_memory.py` 和 `aureus/heartbeat/main_loop.py`，预览入口见 `aureus/heartbeat/prompt_preview.py` 和 `aureus/web/prompt_api.py`。

## 目标和验收

补齐器官页 `working_memory_summary` 场景。生产端与预览共用同一个纯请求构造器：`system` 保留原指令，`user` 保留「旧摘要」「经历」JSON。预览传入固定模拟样本，不查时间线，不发送请求；按已有任务路由显示，不混同长期记忆整理。

## 子步骤

1. 在 `tests/web/test_prompt_http_integration.py` 加独立用例 `test_working_memory_preview_matches_partitioned_summary_http_request`。以合成时间线触发 `WorkingMemory.prepare` → `summarize`，经真实适配器及 `httpx.MockTransport` 捕获最终请求。检查原指令、消息角色、JSON、分区隔离和预览不发送请求；用例先在旧代码上因缺少场景失败。
2. 在 `aureus/attention/working_memory.py` 提取纯构造器，经该子系统公开接口导出；让 `prepare` 与网页场景共用。保持请求字段、输出限制、分区、触发、路由和状态写入不变。
3. 复跑网页提示词联测、提示词 API 和工作记忆专项，记录结果与交接。按本轮独立分支任务范围只跑专项，不合入主线或重启。

## 自检清单

- [x] `test_working_memory_preview_matches_partitioned_summary_http_request` 在任务书提交 `6f9c0b5` 上因缺少 `working_memory_summary` 场景而失败；实现后通过。模拟 HTTP 捕获请求逐条与预览比较，独立断言 `system`、`user` 及 1200 字正文输出契约。
- [x] 同一联测使用用户私聊与公开经历的两份旧摘要和新发言，逐一核对各分区最终请求；预览期间禁止时间线查询，模拟 HTTP 请求数保持零。
- [x] `tests/web/test_prompt_http_integration.py`、`tests/web/test_prompt_api.py`、`tests/heartbeat/test_working_memory.py` 共 13 项通过。改动文件 Ruff 规范、Pyright 和 8 条架构规则通过，`git diff --check` 通过。无 `main_loop.py`、路由、配置或摘要契约改动。

## 实现与验证（2026-11-24）

任务书 `6f9c0b5`，实现 `9ce6df9`。`build_working_memory_summary_request` 经 attention 公开接口导出；生产端仍先按原来源键分组，再调用构造器。预览使用一条固定模拟经历，不查询真实时间线，不调用器官，也不增加编辑项。

从本工作树使用主工作树既有 `.venv/Scripts/python.exe`，设置 `PYTHONPATH` 为本工作树、`PYTHONDONTWRITEBYTECODE=1`、`PYTHONUTF8=1`、`LITELLM_LOCAL_MODEL_COST_MAP=True`，`AUREUS_HOME` 指向本轮合成根 `E:/Temp/aureus-working-memory-preview-4326ac96a8494439957f7720eb02fb67/aureus_home`。

```text
python -m pytest -q -p no:cacheprovider --tb=short --basetemp <合成根>/baseline tests/web/test_prompt_http_integration.py::test_working_memory_preview_matches_partitioned_summary_http_request
python -m pytest -q -p no:cacheprovider --tb=short --basetemp <合成根>/specialized tests/web/test_prompt_http_integration.py tests/web/test_prompt_api.py tests/heartbeat/test_working_memory.py
```

第一条为旧版失败证据；第二条 13 项通过、3 条依赖警告。只跑直接专项，未跑全量。Ruff 格式检查三个改动文件通过；`working_memory.py` 的未改动 `dialogue_focus` 列表推导格式不通过，任务书提交的旧文件同样不通过，保留基线原状，不作全绿声明。Pyright 使用 `--pythonpath` 指向既有解释器，结果 0 错误（工具另提示本工作树没有本地 `.venv`）；8 条架构约束全过。

## 不做

不移植 MaiBot 五字段压缩协议，不新增模板编辑项，不访问私有数据、不调用真实模型，不操作正式实例、不合入 `main`、不推送。

## 待设计问题

无。
