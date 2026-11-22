# 第三方代码清单

移植（复制过来再改）的代码都登记在这里；只依赖的库见 `pyproject.toml` 和 `uv.lock`。规则见 docs/11-dev-guide.md 第 1.3 节：移植的文件开头也要注明来源、原许可证和改动。

| 本项目的文件 | 来源 | 原许可证 | 我们改了什么 |
|---|---|---|---|
| `aureus/heartbeat/speech.py` | MaiBot `src/chat/utils/utils.py` 里的 `_split_into_sentence_segments`、`_merge_processed_segments_to_max_count`、`protect_kaomoji`、`recover_kaomoji`，https://github.com/Mai-with-u/MaiBot/blob/main/src/chat/utils/utils.py | GPL-3.0 | 去掉按概率合并的随机步骤，改成确定性的；只在句号、感叹号、问号、分号和换行处拆；条数超限时均匀合并而不是返回默认回复；去掉对配置、平台、日志和 LLM 断句的依赖，改成纯函数 |
| `aureus/heartbeat/recall.py` 的 `format_recall` | MaiBot `src/maisaka/builtin_tool/query_memory.py` 里的 `_build_replyer_memory_reference`，https://github.com/Mai-with-u/MaiBot/blob/edf42446f123149c57fc79765dd5a8b004b8524c/src/maisaka/builtin_tool/query_memory.py | GPL-3.0 | 保留查询、命中数、编号行和空白归一；改用本项目的 MemoryHit、当地时间和人物称呼；补无命中说明和 900 字整条裁剪；不接入它的工具框架和记忆后端 |
| `aureus/memory/history.py` 的 `search_history` | 安装版 A_Memorix 2.0.0 的 `core/storage/metadata_fact.py::list_current_person_fact_claims` | AGPL-3.0 | 复用有效状态与左闭右开的时段条件，读取 Aureus 时钟，为记忆原件增加内容与人物过滤 |
| `aureus/memory/sparse.py` 的 `ranked_matches` | 安装版 A_Memorix 2.0.0 的 `core/storage/metadata_fts.py::fts_search_bm25` | AGPL-3.0 | 移植 SQLite FTS5 MATCH 和 BM25 的稀疏排位，接到 Aureus 原件及时间线索引。使用标准库 SQLite，脱离 MaiBot 运行 |
| `aureus/tools/migrate/maibot_memory.py` 的 `_message_person` | 安装版 MaiBot 1.3.15 的 `src/person_info/person_info.py::get_person_id` | GPL-3.0 | 保持平台字段规范化和哈希规则，附逻辑实例名，匹配同源 A_Memorix 人物键 |
| `aureus/web/static/ark-pixel.woff2` | TakWolf/ark-pixel-font，2026-11-10 版，12 像素比例简体中文 WOFF2，https://github.com/TakWolf/ark-pixel-font | SIL OFL 1.1 | 原样随网页分发，许可证见同目录 `ark-pixel-OFL.txt` |

## 许可证核对记录

- MaiBot：2026-11-18 读了仓库根目录的 `LICENSE`，是 GNU GPL 第 3 版。GPL-3.0 第 13 条允许把它覆盖的作品和 AGPL-3.0 的作品合并成一个整体，本项目整体采用 AGPL-3.0（D12），所以可以移植。
- 该仓库根目录还有 `EULA.md` 和 `PRIVACY.md`，没有读过，没有核对它们对源码有没有额外限制。
- A_Memorix 2.0.0：安装源码随附的 `LICENSE` 为 AGPL-3.0。`LICENSE-MAIBOT-GPL.md` 对 MaiBot 单独授予 GPL-3.0 权利，本项目沿用默认 AGPL-3.0。
