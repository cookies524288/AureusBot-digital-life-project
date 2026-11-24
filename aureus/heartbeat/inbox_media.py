"""心跳选中会话后，在后台补充图片说明和语音转写。

普通图片说明用 MaiBot 1.3.15 的简体中文模板（GPL-3.0），来源见 THIRD_PARTY.md。
"""

import asyncio
import logging
import sqlite3
from collections.abc import Awaitable, Callable, Sequence
from hashlib import sha256

from aureus.memory import EmojiStore
from aureus.organs import CallPurpose, ModelPreference, OrganCallError, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError
from aureus.timeline import TimelineRecord

from .emoji_recognition import build_emoji_analysis_request, parse_emoji_tags, prepare_emoji_image
from .inbox_store import InboxImageNote, InboxStore
from .perception import MediaReference, Perception

logger = logging.getLogger(__name__)

_IMAGE_DESCRIPTION = (
    "请用中文详细描述这张图片的内容。如果有文字，请把文字描述概括出来，请留意其主题、直观感受，"
    "输出为一段平文本，最多100字，请注意不要分点，就输出一段文本"
)
AttachmentReader = Callable[[Perception, MediaReference], Awaitable[tuple[bytes, str] | None]]
ImageIndexer = Callable[[Perception, MediaReference, bytes], Awaitable[None]]
OrganCall = Callable[[OrganRequest], Awaitable[OrganResponse]]


def build_image_description_request(
    preference: ModelPreference = ModelPreference.PREFERRED,
    *,
    data: bytes | None = None,
    mime: str | None = None,
) -> OrganRequest:
    """组装普通图片描述请求；预览不提供媒体字节和类型。"""
    return OrganRequest(
        "vlm",
        _IMAGE_DESCRIPTION,
        purpose=CallPurpose.OPTIONAL,
        model_preference=preference,
        media_bytes=data,
        media_type=mime,
    )


class InboxMedia:
    """接收部署时绑定的媒体读取器及热更新的模型可用状态。"""

    def __init__(
        self,
        call: OrganCall,
        emoji_store: EmojiStore | None = None,
        inbox_store: InboxStore | None = None,
    ) -> None:
        self._call = call
        self._emoji_store = emoji_store
        self._inbox_store = inbox_store
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
            identifier = message.metadata.get("inbox_message_id")
            notes = await self._describe(
                source, preference, self.read, identifier if type(identifier) is int else None
            )
            if notes:
                content = "\n".join([message.content, *notes])
                message = message.model_copy(update={"content": content})
            enriched.append(message)
        return enriched

    async def _describe(
        self,
        source: Perception,
        preference: ModelPreference,
        read: AttachmentReader,
        identifier: int | None,
    ) -> list[str]:
        notes: list[str] = []
        for position, attachment in enumerate(source.attachments):
            image = attachment.kind == "image"
            is_emoji = image and source.channel == "qq" and attachment.is_emoji
            organ = "vlm" if image else "voice"
            index = self.index_image if image and "image_embedding" in self.available else None
            collect = self._emoji_store if is_emoji else None
            if organ not in self.available and index is None and collect is None:
                continue
            media = await read(source, attachment)
            if media is None:
                continue
            data, mime = media
            if is_emoji:
                if collect is not None:
                    await self._save_image_note(
                        identifier,
                        source,
                        position,
                        InboxImageNote("emoji_catalog", sha256(data).hexdigest()),
                    )
                if description := await self._recognize_emoji(collect, preference, data, mime):
                    notes.append(f"[表情识别] {description}")
            elif organ in self.available:
                request = (
                    build_image_description_request(preference, data=data, mime=mime)
                    if image
                    else OrganRequest(
                        organ,
                        "",
                        purpose=CallPurpose.OPTIONAL,
                        model_preference=preference,
                        media_bytes=data,
                        media_type=mime,
                    )
                )
                if description := await self._suggest(request):
                    label = "图片识别" if image else "语音转写"
                    notes.append(f"[{label}] {description}")
                    if image:
                        await self._save_image_note(
                            identifier, source, position, InboxImageNote("vlm", description)
                        )
            if index is not None:
                await index(source, attachment, data)
        return notes

    async def _save_image_note(
        self, identifier: int | None, source: Perception, position: int, note: InboxImageNote
    ) -> None:
        if self._inbox_store is None or identifier is None:
            return
        try:
            await asyncio.to_thread(
                self._inbox_store.save_image_note, identifier, source, position, note
            )
        except sqlite3.Error:
            logger.warning("未保存收件箱附件的已有描述")

    async def _recognize_emoji(
        self, store: EmojiStore | None, preference: ModelPreference, data: bytes, mime: str
    ) -> str:
        registration_digest = None
        if store is not None:
            try:
                entry = await asyncio.to_thread(store.collect, data)
            except (OSError, sqlite3.Error, ValueError):
                logger.warning("QQ 表情收集未完成")
                return ""
            if entry.is_banned or entry.is_removed:
                return ""
            if entry.is_registered:
                if entry.tags:
                    return "，".join(entry.tags) if "vlm" in self.available else ""
            elif entry.description or entry.tags:
                return ""
            else:
                registration_digest = entry.digest

        if "vlm" not in self.available:
            return ""
        try:
            image_type, payload, payload_mime = await asyncio.to_thread(
                prepare_emoji_image, data, mime
            )
        except ValueError:
            logger.warning("QQ 表情不能安全识别，保留原件")
            return ""
        request = build_emoji_analysis_request(
            image_type, preference, data=payload, mime=payload_mime
        )
        if not (tags := parse_emoji_tags(await self._suggest(request))):
            return ""
        if store is not None and registration_digest is not None:
            try:
                registered = await asyncio.to_thread(
                    store.register_recognition, registration_digest, tags
                )
                if not registered:
                    return ""
            except (OSError, sqlite3.Error, ValueError):
                logger.warning("QQ 表情登记未完成")
                return ""
        return "，".join(tags)

    async def _suggest(self, request: OrganRequest) -> str:
        try:
            return (await self._call(request)).suggestion.strip()
        except (CallRejectedError, OrganCallError):
            logger.debug("收件箱附件识别暂不可用（%s）", request.organ_id)
            return ""
