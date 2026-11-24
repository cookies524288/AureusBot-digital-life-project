# Phase 3.11：普通图片提示词预览与实际请求对照

状态：待评审

## 先读

`docs/tasks/maibot-prompt-parity.md`、`docs/11-dev-guide.md`、`THIRD_PARTY.md`。MaiBot 1.3.15 的 `062d45cf8bd4272ac57d7ec03fcf405e5a458211:prompts/zh-CN/image_description.prompt` 是已核对的普通图片描述基线。不要访问个人媒体或私有配置。

## 目标与边界

普通图片默认指令已逐字采用上游文本；网页的 VLM 预览却回退为通用场景。应显示真实的系统消息、用户侧完整图片描述指令以及同一条用户消息中排在文字后的图片占位。区分“真实请求包含图片”和“网页没有读取或发送任何图片”。不要把占位符作为额外的模型指令。不要为预览生成可发送的图片数据。现有网页文字预览仍须可读，不能将数组渲染成对象字符串或直接显示数据结构。

## 子步骤

1. 登记普通图片识别的专用场景，复用实际请求的提示词构造与渲染路径。保留固定版本原文、现有模型绑定、可选调用、媒体 MIME 和传输约束。若登记模板，遵守项目可编辑与固定协议的区分；不覆盖既有设置。
2. 在网页现有提示词预览中呈现该图文顺序，附上具名图片占位，不接触任何实际附件。保持普通图片、表情、语音三条路径独立，不假称后两者等价于普通图片。
3. 用合成图片与 `httpx.MockTransport` 从常规识别入口捕获最终请求；以固定上游原文独立断言完整文字、消息角色、最后一条内容顺序、MIME 和合成字节；与实际网页预览逐项对照。覆盖文字器官预览未回退及表情/语音分支未误用图片模板。更新许可路径及对照表。

## 自检清单

- [x] `test_image_prompt_preview_matches_inbox_vision_http_request` 在任务书提交 `d08fd93` 上先因场景为 `custom` 而不是 `image_description` 失败；实现后完整通过。预览的请求次数和附件读取次数均不增加，也不返回图片 URL 或 Base64。
- [x] 同一联测从 `InboxMedia.enrich` 经真实 `LLMOrgan` 和 `httpx.MockTransport` 捕获最终 HTTP 请求，对照固定版本原文、系统角色和内容、用户角色、文字和图片的顺序、PNG MIME 及合成 PNG 的完整字节。表情请求仍使用原有不同指令。
- [x] `test_image_prompt_preview_displays_text_then_attachment` 在 390 px Chrome 对话框验证系统消息、用户多行文字、同一条用户消息内随后出现的图片占位，以及没有图片节点或对象字符串。文本器官和语音预览仍保持字符串消息；现有 QQ 媒体专项通过。
- [x] 任务书 `d08fd93`、实现 `87c1662`；专项 75 项通过，Ruff 规范及格式检查通过，Pyright 0 错误，`git diff --check` 通过。对照表与许可登记已同步。不操作正式实例、机密、用户身份或媒体数据，不合并、不推送。

## 核验命令

从开发工作树运行；`PYTHONPATH` 为该工作树，`python` 为主工作树既有 `.venv/Scripts/python.exe`。设置 `PYTHONUTF8=1`、`LITELLM_LOCAL_MODEL_COST_MAP=True`，`--basetemp` 指向本次专用临时目录，避开 Windows 共享 pytest 临时目录的访问错误。

```text
python -m pytest -q --basetemp <临时目录> --tb=short tests/web/test_prompt_http_integration.py::test_image_prompt_preview_matches_inbox_vision_http_request
python -m pytest -q --basetemp <临时目录> --tb=short tests/web/test_prompt_http_integration.py tests/web/test_prompt_api.py tests/channels/test_qq_media.py tests/organs/test_llm_adapter.py tests/web/test_resource_browser.py::test_image_prompt_preview_displays_text_then_attachment
```

第一条为旧版回归证据；第二条在实现后 75 项通过。只跑直接专项，未跑全量。普通图片保留固定文案，未新增编辑项；表情标签、表情拼图和语音转写未移植或改接。不代表所有视觉服务商或器官提示词已对齐。

## 待设计问题

无。
