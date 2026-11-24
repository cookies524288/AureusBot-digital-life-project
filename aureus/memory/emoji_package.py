"""Aureus 素材集：定长边界、无路径的 JSON 和内容寻址原图。"""

from base64 import b64decode, b64encode
from binascii import Error
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aureus.memory.emoji import (
    MAX_EMOJI_BYTES,
    MAX_EMOJI_DESCRIPTION,
    EmojiMaterial,
    _validate_image,
)

PACKAGE_BYTES = 32 * 1024 * 1024
PACKAGE_MEMBERS = 20
SOURCE_LICENSE_CHARS = 500
_ENCODED_IMAGE_LIMIT = 4 * ((MAX_EMOJI_BYTES + 2) // 3)


class _Member(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    description: Annotated[str, Field(max_length=MAX_EMOJI_DESCRIPTION)]
    source_license: Annotated[str, Field(min_length=1, max_length=SOURCE_LICENSE_CHARS)]
    image: Annotated[str, Field(min_length=1, max_length=_ENCODED_IMAGE_LIMIT)]


class _Package(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    format: Literal["aureus.emoji.materials"]
    version: Literal[1]
    members: list[_Member] = Field(min_length=1, max_length=PACKAGE_MEMBERS)


def read_emoji_package(raw: bytes) -> list[EmojiMaterial]:
    """读固定字段，逐项复核格式、大小和摘要；无任何写入。"""
    if len(raw) > PACKAGE_BYTES:
        raise ValueError("素材集超过 32 MiB。")
    try:
        package = _Package.model_validate_json(raw)
        result = {}
        for member in package.members:
            data = b64decode(member.image, validate=True)
            if sha256(data).hexdigest() != member.digest or not member.source_license.strip():
                raise ValueError
            if member.digest in result:
                raise ValueError
            _validate_image(data)
            result[member.digest] = EmojiMaterial(
                member.digest, data, member.description.strip(), member.source_license.strip()
            )
    except (ValidationError, Error, ValueError):
        raise ValueError("素材集格式、许可声明、图片内容或摘要不符合 Aureus v1 规则。") from None
    return list(result.values())


def make_emoji_package(materials: list[EmojiMaterial]) -> bytes:
    """生成仅含选中成员原图、描述与声明的 Aureus 素材集。"""
    size = sum(4 * ((len(item.data) + 2) // 3) for item in materials)
    if size > PACKAGE_BYTES:
        raise ValueError("选中图像超过素材集上限，请分批导出。")
    result = (
        _Package(
            format="aureus.emoji.materials",
            version=1,
            members=[
                _Member(
                    digest=item.digest,
                    description=item.description,
                    source_license=item.source_license,
                    image=b64encode(item.data).decode("ascii"),
                )
                for item in materials
            ],
        )
        .model_dump_json()
        .encode()
    )
    if len(result) > PACKAGE_BYTES:
        raise ValueError("素材集超过 32 MiB，请分批导出。")
    return result
