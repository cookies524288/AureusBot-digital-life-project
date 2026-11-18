"""说话方式：把一段回复拆成几条短消息（路线图 1.7，D60）。

移植自 MaiBot 的 src/chat/utils/utils.py（_split_into_sentence_segments、
_merge_processed_segments_to_max_count、protect_kaomoji、recover_kaomoji），
来源：https://github.com/Mai-with-u/MaiBot/blob/main/src/chat/utils/utils.py
原许可证 GPL-3.0，和本项目的 AGPL-3.0 兼容，登记在 THIRD_PARTY.md。

我们改了什么：
- 去掉按概率合并相邻句子的随机步骤，改成确定性的：同样的回复永远拆成同样的几条，
  实验才能复现（11 号文档 4.5）；
- 只在句号、感叹号、问号、分号和换行处拆，逗号不拆；太短的句子并进前一句；
- 条数超过上限时均匀合并，不丢内容，也不返回默认回复；
- 去掉对配置、平台、日志和 LLM 断句的依赖，变成只做字符串处理的纯函数。
"""

import re

# 拆出来的一段是 (文字, 后面跟着的分隔符)；分隔符只在把几段并回一条时用来恢复停顿。
_Piece = tuple[str, str]

_NEWLINE = "\n"
# 句末的感叹号和问号留在这一句里；句号和分号拆开后丢掉，聊天里不写句号。
_KEPT_ENDINGS = "！？!?"
_DROPPED_BREAKS = "。；;\n"
# 并回一条时用逗号接上；每条消息结尾多余的逗号、句号、空格去掉。
_JOINER = "，"
_TRAILING = "，,。；; "
# 引号里面不拆：从开引号到对应的闭引号。
_QUOTE_PAIRS = {"“": "”", "‘": "’", "「": "」", "『": "』", '"': '"'}

# 颜文字（含括号和不含括号的）拆开就坏了，先换成占位符，拆完再换回来。
_KAOMOJI = re.compile(
    r"([(\[（【][^()\[\]（）【】]*?[^一-龥a-zA-Z0-9\s][^()\[\]（）【】]*?[)\]）】])"
    r"|([▼▽・ᴥω･﹏^><≧≦￣｀´∀ヮДд︿﹀へ｡ﾟ╥╯╰︶︹•⁄]{2,15})"
)
_PLACEHOLDER_OPEN = "\ue000"
_PLACEHOLDER_CLOSE = "\ue001"


def split_reply(text: str, *, max_messages: int, min_chars: int) -> list[str]:
    """把一段回复拆成几条短消息。

    Args:
        text: 回复器写出来的整段话。
        max_messages: 最多拆成几条，至少 1；1 表示不拆。
        min_chars: 一条至少多少个字；更短的句子并进前一句。

    Returns:
        从早到晚的几条消息，内容不丢。空文字返回空列表。

    Raises:
        ValueError: max_messages 小于 1。
    """
    if max_messages < 1:
        raise ValueError("max_messages 至少是 1")
    cleaned = text.strip()
    if not cleaned:
        return []
    if max_messages == 1:
        return [cleaned]
    protected, mapping = _hide_kaomoji(cleaned)
    pieces = _cap(_merge_short(_scan(protected), min_chars), max_messages)
    messages = [_restore_kaomoji(piece, mapping).rstrip(_TRAILING) for piece, _sep in pieces]
    return [message for message in messages if message.strip()] or [cleaned]


def _hide_kaomoji(text: str) -> tuple[str, dict[str, str]]:
    mapping: dict[str, str] = {}

    def hide(match: re.Match[str]) -> str:
        key = f"{_PLACEHOLDER_OPEN}{len(mapping)}{_PLACEHOLDER_CLOSE}"
        mapping[key] = match.group(0)
        return key

    return _KAOMOJI.sub(hide, text), mapping


def _restore_kaomoji(text: str, mapping: dict[str, str]) -> str:
    for key, kaomoji in mapping.items():
        text = text.replace(key, kaomoji)
    return text


def _scan(text: str) -> list[_Piece]:
    """在句末标点和换行处拆开；引号里面不拆。"""
    pieces: list[_Piece] = []
    current: list[str] = []
    closing = ""  # 在引号里面时，等着的那个闭引号
    index = 0
    while index < len(text):
        char = text[index]
        if closing:
            current.append(char)
            if char == closing:
                closing = ""
        elif char in _QUOTE_PAIRS:
            closing = _QUOTE_PAIRS[char]
            current.append(char)
        elif char in _KEPT_ENDINGS:
            while index < len(text) and text[index] in _KEPT_ENDINGS:  # 「？！」连着的一起留下
                current.append(text[index])
                index += 1
            _cut(pieces, current, "")
            continue
        elif char in _DROPPED_BREAKS:
            _cut(pieces, current, char)
        else:
            current.append(char)
        index += 1
    _cut(pieces, current, "")
    return pieces


def _cut(pieces: list[_Piece], current: list[str], separator: str) -> None:
    piece = "".join(current).strip()
    current.clear()
    if piece:
        pieces.append((piece, separator))
    elif separator == _NEWLINE and pieces:
        pieces[-1] = (pieces[-1][0], _NEWLINE)  # 空行：前一句后面是硬换行


def _join(first: str, separator: str, second: str) -> str:
    return first + (_JOINER if separator and separator in _DROPPED_BREAKS else "") + second


def _merge_short(pieces: list[_Piece], min_chars: int) -> list[_Piece]:
    """太短的句子并进前一句或后一句。换行是硬边界，不跨过去并。"""
    merged: list[_Piece] = []
    for text, separator in pieces:
        if merged and len(merged[-1][0]) < min_chars and merged[-1][1] != _NEWLINE:
            previous, previous_separator = merged[-1]
            merged[-1] = (_join(previous, previous_separator, text), separator)
        else:
            merged.append((text, separator))
    if len(merged) >= 2 and len(merged[-1][0]) < min_chars and merged[-2][1] != _NEWLINE:
        last, last_separator = merged.pop()
        previous, previous_separator = merged.pop()
        merged.append((_join(previous, previous_separator, last), last_separator))
    return merged


def _cap(pieces: list[_Piece], max_messages: int) -> list[_Piece]:
    """条数超过上限时，把相邻的句子均匀分成 max_messages 组，每组并成一条。

    上限比换行这种硬边界优先：宁可并起来，也不超过要求的条数。
    """
    if len(pieces) <= max_messages:
        return pieces
    result: list[_Piece] = []
    start = 0
    for group in range(max_messages):
        size = -(-(len(pieces) - start) // (max_messages - group))  # 向上取整，前面的组多分一点
        chunk = pieces[start : start + size]
        start += size
        text = chunk[0][0]
        for position in range(1, len(chunk)):
            text = _join(text, chunk[position - 1][1], chunk[position][0])
        result.append((text, chunk[-1][1]))
    return result
