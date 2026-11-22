# 第三方代码清单

移植（复制过来再改）的代码都登记在这里；只依赖的库见 `pyproject.toml` 和 `uv.lock`。规则见 docs/11-dev-guide.md 第 1.3 节：移植的文件开头也要注明来源、原许可证和改动。

| 本项目的文件 | 来源 | 原许可证 | 我们改了什么 |
|---|---|---|---|
| `aureus/heartbeat/speech.py` | MaiBot `src/chat/utils/utils.py` 里的 `_split_into_sentence_segments`、`_merge_processed_segments_to_max_count`、`protect_kaomoji`、`recover_kaomoji`，https://github.com/Mai-with-u/MaiBot/blob/main/src/chat/utils/utils.py | GPL-3.0 | 去掉按概率合并的随机步骤，改成确定性的；只在句号、感叹号、问号、分号和换行处拆；条数超限时均匀合并而不是返回默认回复；去掉对配置、平台、日志和 LLM 断句的依赖，改成纯函数 |
| `aureus/heartbeat/recall.py` 的 `format_recall` | MaiBot `src/maisaka/builtin_tool/query_memory.py` 里的 `_build_replyer_memory_reference`，https://github.com/Mai-with-u/MaiBot/blob/edf42446f123149c57fc79765dd5a8b004b8524c/src/maisaka/builtin_tool/query_memory.py | GPL-3.0 | 保留查询、命中数、编号行和空白归一；改用本项目的 MemoryHit、当地时间和人物称呼；补无命中说明和 900 字整条裁剪；不接入它的工具框架和记忆后端 |
| `aureus/web/static/ark-pixel.woff2` | TakWolf/ark-pixel-font，2026-11-10 版，12 像素比例简体中文 WOFF2，https://github.com/TakWolf/ark-pixel-font | SIL OFL 1.1 | 原样随网页分发，许可证见同目录 `ark-pixel-OFL.txt` |

## 许可证核对记录

- MaiBot：2026-11-18 读了仓库根目录的 `LICENSE`，是 GNU GPL 第 3 版。GPL-3.0 第 13 条允许把它覆盖的作品和 AGPL-3.0 的作品合并成一个整体，本项目整体采用 AGPL-3.0（D12），所以可以移植。
- 该仓库根目录还有 `EULA.md` 和 `PRIVACY.md`，没有读过，没有核对它们对源码有没有额外限制。

## 协议参考

SnowLuma 2.1.1 对接以本机安装的 snowluma-adapter 1.1.8 为基线，`_manifest.json` 标记 GPL-v3.0-or-later。Aureus 按下列接口行为独立编写通道，不随项目复制或打包适配器代码。

| Aureus 文件 | snowluma-adapter 1.1.8 对照路径与函数 |
|---|---|
| `aureus/channels/onebot_transport.py` | `transport.py` 的 `QQTransportClient._build_connect_url`、`_build_headers`、`call_action`、`_receive_loop`，以及 `runtime/router.py` 的 `bootstrap_adapter_runtime_state`、`_extract_self_id_from_login_response` |
| `aureus/channels/onebot.py` | `runtime/router.py` 的 `handle_transport_payload`、`handle_inbound_message`，`codecs/inbound/message_codec.py` 的 `build_message_dict`、`_require_message_segments`、`convert_segments_with_metadata` |
| `aureus/channels/onebot.py` | `profile.py` 的 `SNOWLUMA_PROFILE`，`codecs/outbound/message_codec.py` 的 `_build_send_action`，以及 `codecs/outbound/segment_encoder.py` 的 `_build_at_segments`、`_build_reply_segments` |
