# 第三方代码清单

移植（复制过来再改）的代码都登记在这里；只依赖的库见 `pyproject.toml` 和 `uv.lock`。规则见 docs/11-dev-guide.md 第 1.3 节：移植的文件开头也要注明来源、原许可证和改动。

| 本项目的文件 | 来源 | 原许可证 | 我们改了什么 |
|---|---|---|---|
| `aureus/heartbeat/speech.py` | MaiBot `src/chat/utils/utils.py` 里的 `_split_into_sentence_segments`、`_merge_processed_segments_to_max_count`、`protect_kaomoji`、`recover_kaomoji`，https://github.com/Mai-with-u/MaiBot/blob/main/src/chat/utils/utils.py | GPL-3.0 | 去掉按概率合并的随机步骤，改成确定性的；只在句号、感叹号、问号、分号和换行处拆；条数超限时均匀合并而不是返回默认回复；去掉对配置、平台、日志和 LLM 断句的依赖，改成纯函数 |

## 许可证核对记录

- MaiBot：2026-11-18 读了仓库根目录的 `LICENSE`，是 GNU GPL 第 3 版。GPL-3.0 第 13 条允许把它覆盖的作品和 AGPL-3.0 的作品合并成一个整体，本项目整体采用 AGPL-3.0（D12），所以可以移植。
- 该仓库根目录还有 `EULA.md` 和 `PRIVACY.md`，没有读过，没有核对它们对源码有没有额外限制。
