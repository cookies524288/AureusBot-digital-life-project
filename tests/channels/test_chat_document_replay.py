"""交给回环 QQ 真正的 Office/PDF 附件，不把名称、外链或未读部件当正文。"""

import shutil
from io import BytesIO
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from aureus.common import ManualClock
from aureus.heartbeat.inbox_store import ConversationKey
from aureus.heartbeat.media_extract import document_excerpt
from aureus.timeline import VisibilityScope
from tests.channels.test_chat_media_samples import install_media_server
from tests.channels.test_qq_behavior_replay import START, open_replay
from tests.channels.test_qq_runtime import ACCOUNT, GROUP, event
from tests.prompt_assertions import dialogue_text

_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PACKAGE = "http://schemas.openxmlformats.org/package/2006"
_MAIN = "http://schemas.openxmlformats.org/"
_MIME = "application/vnd.openxmlformats-officedocument."
_WORD = _MAIN + "wordprocessingml/2006/main"
_SHEET = _MAIN + "spreadsheetml/2006/main"
_SLIDE = _MAIN + "presentationml/2006/main"
_DRAW = _MAIN + "drawingml/2006/main"
_TEXT = {
    "docx": "风车旁的木桌",
    "xlsx": "露台 A1：青瓷杯",
    "pptx": "先去蓝色南门",
    "pdf": "copper windmill at noon",
}


def relationships(*rows: tuple[str, str, str]) -> str:
    return (
        f'<Relationships xmlns="{_PACKAGE}/relationships">'
        + "".join(
            f'<Relationship Id="{identifier}" Type="{_REL}/{kind}" Target="{target}"/>'
            for identifier, kind, target in rows
        )
        + "</Relationships>"
    )


def office_members(kind: str) -> dict[str, str]:
    if kind == "docx":
        main, content_type = "word/document.xml", "wordprocessingml.document"
        overrides = {}
        members = {
            main: f'<w:document xmlns:w="{_WORD}"><w:body>'
            "<w:p><w:r><w:t>第一条</w:t></w:r></w:p>"
            "<w:p><w:r><w:t>风车旁的</w:t></w:r><w:r><w:t>木桌</w:t></w:r></w:p>"
            "<w:sectPr/></w:body></w:document>"
        }
    elif kind == "xlsx":
        main, content_type = "xl/workbook.xml", "spreadsheetml.sheet"
        overrides = {
            "xl/sharedStrings.xml": "spreadsheetml.sharedStrings+xml",
            "xl/worksheets/sheet2.xml": "spreadsheetml.worksheet+xml",
        }
        members = {
            main: f'<workbook xmlns="{_SHEET}" xmlns:r="{_REL}"><sheets>'
            '<sheet name="露台" sheetId="2" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": relationships(
                ("rId1", "worksheet", "worksheets/sheet2.xml"),
                ("rId2", "sharedStrings", "sharedStrings.xml"),
            ),
            "xl/sharedStrings.xml": f'<sst xmlns="{_SHEET}" count="1" uniqueCount="1">'
            "<si><t>青瓷杯</t></si></sst>",
            "xl/worksheets/sheet2.xml": f'<worksheet xmlns="{_SHEET}"><sheetData><row r="1">'
            '<c r="A1" t="s"><v>0</v></c><c r="B1"><v>17</v></c>'
            '<c r="C1" t="inlineStr"><is><t>蓝碟</t></is></c></row></sheetData></worksheet>',
        }
    else:
        main, content_type = "ppt/presentation.xml", "presentationml.presentation"
        overrides = {f"ppt/slides/slide{index}.xml": "presentationml.slide+xml" for index in (1, 2)}
        members = {
            main: f'<p:presentation xmlns:p="{_SLIDE}" xmlns:r="{_REL}"><p:sldIdLst>'
            '<p:sldId id="257" r:id="rId2"/><p:sldId id="256" r:id="rId1"/>'
            '</p:sldIdLst><p:notesSz cx="6858000" cy="9144000"/></p:presentation>',
            "ppt/_rels/presentation.xml.rels": relationships(
                ("rId1", "slide", "slides/slide1.xml"), ("rId2", "slide", "slides/slide2.xml")
            ),
        }
        for number, words in ((1, "再去白色北门"), (2, _TEXT["pptx"])):
            members[f"ppt/slides/slide{number}.xml"] = (
                f'<p:sld xmlns:p="{_SLIDE}" xmlns:a="{_DRAW}"><p:cSld><p:spTree>'
                '<p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>'
                '<p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2" name="合成标题"/>'
                "<p:cNvSpPr/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/>"
                f"<a:p><a:r><a:t>{words}</a:t></a:r></a:p></p:txBody>"
                "</p:sp></p:spTree></p:cSld></p:sld>"
            )
    overrides[main] = content_type + ".main+xml"
    members["[Content_Types].xml"] = (
        f'<Types xmlns="{_PACKAGE}/content-types">'
        '<Default Extension="rels" '
        'ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        + "".join(
            f'<Override PartName="/{name}" ContentType="{_MIME}{mime}"/>'
            for name, mime in overrides.items()
        )
        + "</Types>"
    )
    members["_rels/.rels"] = relationships(("rId1", "officeDocument", main))
    members["docProps/unused.xml"] = "<metadata>不应呈现的附带元数据</metadata>"
    return members


def archive(members: dict[str, str]) -> bytes:
    with BytesIO() as buffer:
        with ZipFile(buffer, "w", compression=ZIP_DEFLATED) as package:
            for path, text in members.items():
                package.writestr(path, text.encode())
        return buffer.getvalue()


def pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 24 150 Td ({text}) Tj ET".encode()
    records = (
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 240 200] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream",
    )
    result = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, record in enumerate(records, 1):
        offsets.append(len(result))
        result.extend(f"{number} 0 obj\n".encode() + record + b"\nendobj\n")
    start = len(result)
    result.extend(b"xref\n0 6\n0000000000 65535 f \n")
    for offset in offsets:
        result.extend(f"{offset:010d} 00000 n \n".encode())
    result.extend(f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n".encode())
    return bytes(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["docx", "xlsx", "pptx", "pdf"])
async def test_only_the_scoped_document_body_reaches_chat(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    if kind == "pdf" and not shutil.which("pdftotext"):
        pytest.skip("这个回放需要可选本地 PDF 文本解码器")
    group_message = event(
        5781,
        2574,
        "",
        group=GROUP,
        message=[
            {"type": "file", "data": {"file_id": "document-eighteen", "name": f"庭院.{kind}"}}
        ],
    )
    data = pdf(_TEXT[kind]) if kind == "pdf" else archive(office_members(kind))
    actions = install_media_server(monkeypatch, group_message, data)
    async with open_replay(tmp_path, monkeypatch, ManualClock(START)) as replay:
        await replay.receive(group_message)
        await replay.step()
        prompt = dialogue_text(replay.planner.calls[-1][1])
        assert _TEXT[kind] in prompt and "不应呈现的附带元数据" not in prompt
        if kind == "pptx":
            assert prompt.index(_TEXT[kind]) < prompt.index("再去白色北门")
        elif kind == "xlsx":
            assert "露台 B1：17" in prompt and "露台 C1：蓝碟" in prompt
        store = replay.core.inbox_store
        assert store is not None
        key = ConversationKey("qq", str(ACCOUNT), "group", str(GROUP))
        [saved] = store.list_read_history(
            key, VisibilityScope.GROUP, before_id=None, keyword=_TEXT[kind], limit=2
        )
        assert saved.perception.route and saved.perception.route.message_id == "5781"
        assert any(_TEXT[kind] in note for note in saved.media_notes)
        assert actions.count("get_group_file_url") == actions.count("download_file_stream") == 1
        assert replay.fake.messages.empty()


def test_only_visible_fixed_parts_are_read() -> None:
    members = office_members("xlsx")
    relation = "xl/_rels/workbook.xml.rels"
    for target in ("../worksheets/sheet2.xml", "https://example.invalid/sheet2.xml"):
        changed = members | {relation: members[relation].replace("worksheets/sheet2.xml", target)}
        assert not document_excerpt(archive(changed), "fixture.xlsx", "")
    assert not document_excerpt(archive(members | {"../unused.xml": "测试"}), "fixture.xlsx", "")
    assert not document_excerpt(archive(members), "fixture.docx", "")
    assert not document_excerpt(archive(members), "fixture.xlsm", "")
    external = members[relation].replace('Id="rId1"', 'Id="rId1" TargetMode="External"')
    assert not document_excerpt(archive(members | {relation: external}), "fixture.xlsx", "")
    hidden = members["xl/workbook.xml"].replace('sheetId="2"', 'sheetId="2" state="hidden"')
    assert not document_excerpt(archive(members | {"xl/workbook.xml": hidden}), "fixture.xlsx", "")


def test_document_excerpt_limits_and_preserves_a_question_match() -> None:
    members = office_members("docx")
    main = "word/document.xml"
    text = members[main].replace("第一条", "待查内容" * 700 + " 唯一问题的答案在这里 ")
    excerpt = document_excerpt(archive(members | {main: text}), "fixture.docx", "唯一问题的答案")
    assert len(excerpt) <= 2000 and "唯一问题的答案在这里" in excerpt
    large = members[main].replace("第一条", "a" * (2 * 1024 * 1024))
    assert not document_excerpt(archive(members | {main: large}), "fixture.docx", "")
    assert not document_excerpt(b"PK\x03\x04not-a-package", "fixture.docx", "")
    declaration = "<!DOCTYPE document>" + members[main]
    assert not document_excerpt(archive(members | {main: declaration}), "fixture.docx", "")


def test_unavailable_pdf_extraction_has_no_excerpt(monkeypatch: pytest.MonkeyPatch) -> None:
    assert not document_excerpt(pdf(""), "empty.pdf", "")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert not document_excerpt(pdf(_TEXT["pdf"]), "no-decoder.pdf", "")
