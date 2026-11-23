"""心跳选中会话后，在后台补充图片说明和语音转写。

普通图片说明用 MaiBot 1.3.15 的简体中文模板（GPL-3.0），来源见 THIRD_PARTY.md。
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence

from aureus.memory import EmojiStore
from aureus.organs import CallPurpose, ModelPreference, OrganCallError, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError
from aureus.timeline import TimelineRecord

from .perception import MediaReference, Perception

logger = logging.getLogger(__name__)

_IMAGE_DESCRIPTION = (
    "请用中文详细描述这张图片的内容。如果有文字，请把文字描述概括出来，请留意其主题、直观感受，"
    "输出为一段平文本，最多100字，请注意不要分点，就输出一段文本"
)
_EMOJI_DESCRIPTION = (
    "请客观描述这张图片，读出可辨认的文字。请用一句简短中文注明画面和它表达的情绪。"
)

AttachmentReader = Callable[[Perception, MediaReference], Awaitable[tuple[bytes, str] | None]]
ImageIndexer = Callable[[Perception, MediaReference, bytes], Awaitable[None]]
OrganCall = Callable[[OrganRequest], Awaitable[OrganResponse]]


class InboxMedia:
    """接收部署时绑定的媒体读取器及热更新的模型可用状态。"""

    def __init__(self, call: OrganCall, emoji_store: EmojiStore | None = None) -> None:
        self._call = call
        self._emoji_store = emoji_store
        self.read: AttachmentReader | None = None
        self.index_image: ImageIndexer | None = None
        self.available: frozenset[str] = frozenset()

    async def enrich(
        self,
        messages: Sequence[TimelineRecord],
        sources: Sequence[Perception],
        preference: ModelPreference,
    ) -> Sequence[TimelineRecord]:
        """把识别结果加入本次对话材料，保留时间线中的正文与原始附件。"""
        if self.read is None or not sources:
            return messages
        enriched: list[TimelineRecord] = []
        for message, source in zip(messages, sources, strict=True):
            notes = await self._describe(source, preference, self.read)
            if notes:
                content = "\n".join([message.content, *notes])
                message = message.model_copy(update={"content": content})
            enriched.append(message)
        return enriched

    async def _describe(
        self, source: Perception, preference: ModelPreference, read: AttachmentReader
    ) -> list[str]:
        notes: list[str] = []
        for attachment in source.attachments:
            image = attachment.kind == "image"
            organ = "vlm" if image else "voice"
            index = self.index_image if image and "image_embedding" in self.available else None
            collect = self._emoji_store if source.channel == "qq" and attachment.is_emoji else None
            if organ not in self.available and index is None and collect is None:
                continue
            media = await read(source, attachment)
            if media is None:
                continue
            data, mime = media
            needs_registration = False
            if collect is not None:
                try:
                    entry = await asyncio.to_thread(collect.collect, data)
                    needs_registration = not (
                        entry.is_registered or entry.is_banned or entry.description
                    )
                except (OSError, sqlite3.Error, ValueError):
                    logger.warning("QQ 表情收集未完成")
            if organ in self.available:
                label = "表情识别" if attachment.is_emoji else "图片识别" if image else "语音转写"
                prompt = ""
                if image:
                    prompt = _EMOJI_DESCRIPTION if attachment.is_emoji else _IMAGE_DESCRIPTION
                try:
                    result = await self._call(
                        OrganRequest(
                            organ,
                            prompt,
                            purpose=CallPurpose.OPTIONAL,
                            model_preference=preference,
                            media_bytes=data,
                            media_type=mime,
                        )
                    )
                    if description := result.suggestion.strip():
                        notes.append(f"[{label}] {description}")
                        if collect is not None and needs_registration:
                            try:
                                await asyncio.to_thread(
                                    collect.collect, data, description=description, register=True
                                )
                            except (OSError, sqlite3.Error, ValueError):
                                logger.warning("QQ 表情登记未完成")
                except (CallRejectedError, OrganCallError):
                    logger.debug("收件箱附件识别暂不可用（%s）", organ)
            if index is not None:
                await index(source, attachment, data)
        return notes
