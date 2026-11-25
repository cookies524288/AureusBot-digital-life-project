"""验证和生成与 MaiBot 1.3.15 公开格式兼容的语言资源包。"""

import json
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from aureus.memory.language import LanguageContent, LanguageKind, LanguagePackageRow, LanguageStore
from aureus.memory.language_matching import JargonStatus, LanguageEntry

MAX_PACKAGE_BYTES = 8 * 1024 * 1024
_MAX_ROWS = 10_000
_MAX_TEXT = 20_000
type Text = Annotated[str, Field(max_length=_MAX_TEXT)]
type Count = Annotated[int, Field(ge=0, le=2**63 - 1)]


class _Fields(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class _Target(_Fields):
    platform: Text
    id: Text
    type: Literal["group", "private"]
    account_id: Text | None = None
    scope: Text | None = None
    count: Count = 0


class _Jargon(_Fields):
    content: Text
    meaning: Text = ""
    count: Count = 0
    is_jargon: bool = False
    is_complete: bool = False
    is_global: bool = False
    created_by: Literal["manual", "ai"] = "manual"
    targets: list[_Target] | None = Field(default=None, max_length=_MAX_ROWS)
    aureus_jargon_status: Literal[JargonStatus.CANDIDATE] | None = None
    aureus_jargon_source: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")] | None = None


class _Expression(_Fields):
    situation: Text
    style: Text
    content_list: Text = "[]"
    count: Count = 0
    last_active_time: Text | None = None
    create_time: Text | None = None
    checked: bool = False
    modified_by: Text | None = None
    aureus_source_chat_name: Text | None = None


class _Package(_Fields):
    success: Literal[True] = True
    version: Literal[1]
    exported_at: Text
    count: Count


class _JargonPackage(_Package):
    type: Literal["maibot.jargon.export"]
    include_chat_info: bool = False
    jargons: list[_Jargon] = Field(max_length=_MAX_ROWS)


class _ExpressionPackage(_Package):
    type: Literal["maibot.expression.export"]
    source_chat_name: Text
    expressions: list[_Expression] = Field(max_length=_MAX_ROWS)


def read_package(kind: LanguageKind, data: bytes) -> list[LanguagePackageRow]:
    """完整验证选定类别的文件后，返回待保存的原件；不写入数据库。"""
    try:
        if kind == LanguageKind.JARGON:
            jargon = _JargonPackage.model_validate_json(data)
            items = [
                LanguagePackageRow(
                    LanguageContent(item.content, item.meaning),
                    item.count,
                    "",
                    item.model_dump_json(
                        exclude={
                            field
                            for field in ("aureus_jargon_status", "aureus_jargon_source")
                            if getattr(item, field) is None
                        }
                    ),
                )
                for item in jargon.jargons
            ]
            count = jargon.count
        else:
            expression = _ExpressionPackage.model_validate_json(data)
            items = []
            for item in expression.expressions:
                examples = json.loads(item.content_list)
                if not isinstance(examples, list) or not all(
                    isinstance(value, str) for value in examples
                ):
                    raise ValueError
                example = next((value for value in examples if value), "")
                source = item.aureus_source_chat_name
                items.append(
                    LanguagePackageRow(
                        LanguageContent(item.style, item.situation, example),
                        item.count,
                        expression.source_chat_name if source is None else source,
                        item.model_dump_json(exclude={"aureus_source_chat_name"}),
                    )
                )
            count = expression.count
        if count != len(items) or any(not entry.content.text.strip() for entry in items):
            raise ValueError
        return items
    except (ValidationError, ValueError, RecursionError):
        raise ValueError("资源包损坏，或类别、版本、数量或字段不符合 MaiBot v1 格式。") from None


def make_package(store: LanguageStore, kind: LanguageKind, timestamp: str) -> bytes:
    """导出统一语言库的指定类别；包原件的范围及统计按原始字段返回。"""
    originals = store.package_originals(kind)
    items: list[dict[str, object]] = []
    for entry in store.entries(kind, include_disabled=True):
        if entry.is_removed:
            continue
        if kind == LanguageKind.EXPRESSION and entry.restricted_scope is not None:
            continue
        if entry.entry_key in originals:
            source, raw = originals[entry.entry_key]
            item = json.loads(raw)
            if kind == LanguageKind.EXPRESSION:
                item["aureus_source_chat_name"] = source
        elif kind == LanguageKind.JARGON:
            item = _Jargon(
                content=entry.text,
                meaning=entry.detail,
                count=entry.count,
                is_complete=bool(entry.detail),
            ).model_dump()
            item["aureus_jargon_source"] = sha256(entry.entry_key.encode()).hexdigest()
        else:
            examples = [entry.example] if entry.example else []
            item = _Expression(
                situation=entry.detail,
                style=entry.text,
                count=entry.count,
                content_list=json.dumps(examples, ensure_ascii=False),
            ).model_dump(exclude={"aureus_source_chat_name"})
        if kind == LanguageKind.JARGON:
            _export_jargon_status(item, entry)
        items.append(item)
    package: dict[str, object] = {
        "success": True,
        "version": 1,
        "type": f"maibot.{kind}.export",
        "exported_at": timestamp,
        "count": len(items),
    }
    if kind == LanguageKind.JARGON:
        package.update(
            include_chat_info=any(item.get("targets") is not None for item in items), jargons=items
        )
    else:
        package.update(source_chat_name="Aureus", expressions=items)
    data = json.dumps(package, ensure_ascii=False).encode()
    if len(data) > MAX_PACKAGE_BYTES or len(items) > _MAX_ROWS:
        raise ValueError("语言库超过单份资源包上限，暂不能导出整库。")
    return data


def _export_jargon_status(item: dict[str, object], entry: LanguageEntry) -> None:
    if item.get("aureus_jargon_source") is None:
        item.pop("aureus_jargon_source", None)
    item["is_jargon"] = entry.jargon_status == JargonStatus.CONFIRMED
    if entry.jargon_status == JargonStatus.CANDIDATE:
        item["aureus_jargon_status"] = JargonStatus.CANDIDATE
    else:
        item.pop("aureus_jargon_status", None)
