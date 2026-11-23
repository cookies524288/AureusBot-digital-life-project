"""心跳选中会话后，在后台补充图片说明和语音转写。"""

import logging
from collections.abc import Awaitable, Callable, Sequence

from aureus.organs import CallPurpose, ModelPreference, OrganCallError, OrganRequest, OrganResponse
from aureus.safety import CallRejectedError
from aureus.timeline import TimelineRecord

from .perception import MediaReference, Perception

logger = logging.getLogger(__name__)

AttachmentReader = Callable[[Perception, MediaReference], Awaitable[tuple[bytes, str] | None]]
ImageIndexer = Callable[[Perception, MediaReference, bytes], Awaitable[None]]
OrganCall = Callable[[OrganRequest], Awaitable[OrganResponse]]


class InboxMedia:
    """接收部署时绑定的媒体读取器及热更新的模型可用状态。"""

    def __init__(self, call: OrganCall) -> None:
        self._call = call
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
            if organ not in self.available and index is None:
                continue
            media = await read(source, attachment)
            if media is None:
                continue
            data, mime = media
            if organ in self.available:
                label = "图片识别" if image else "语音转写"
                prompt = "请客观描述这张图片，读出可辨认的文字。" if image else ""
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
                except (CallRejectedError, OrganCallError):
                    logger.debug("收件箱附件识别暂不可用（%s）", organ)
            if index is not None:
                await index(source, attachment, data)
        return notes
