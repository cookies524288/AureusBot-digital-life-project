"""从已经获准的聊天附件中摘取正文；只读固定文档部件，不执行内容或访问外部引用。"""

import re
import shutil
import subprocess
import zlib
from collections.abc import Iterable, Iterator
from io import BytesIO
from pathlib import Path
from threading import Thread, Timer
from xml.etree import ElementTree as Xml
from zipfile import ZIP_DEFLATED, ZIP_STORED, BadZipFile, ZipFile

_MIME_PREFIX = "application/vnd.openxmlformats-officedocument."
_OFFICE = {
    ".docx": ("word/document.xml", _MIME_PREFIX + "wordprocessingml.document"),
    ".xlsx": ("xl/workbook.xml", _MIME_PREFIX + "spreadsheetml.sheet"),
    ".pptx": ("ppt/presentation.xml", _MIME_PREFIX + "presentationml.presentation"),
}
_ARCHIVE_BYTES = 8 * 1024 * 1024
_MAX_MEMBERS = 512
_ZIP_DIRECTORY_BYTES = 128 * 1024
_ZIP_END_WINDOW = 22 + 65535
_XML_BYTES = 2 * 1024 * 1024
_TOTAL_XML_BYTES = 4 * 1024 * 1024
_TEXT_CHARS = 64 * 1024
_PDF_PAGES = 8
_PDF_SECONDS = 5
_REL_ID = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"


def document_mime(data: bytes, name: str) -> str | None:
    """根据后缀和签名识别待验证的容器；内容和格式由读取器再核对。"""
    suffix = Path(name).suffix.lower()
    if data.startswith(b"%PDF-") and suffix == ".pdf":
        return "application/pdf"
    if suffix in _OFFICE and data.startswith(b"PK\x03\x04"):
        return _OFFICE[suffix][1]
    return None


class _Parts:
    def __init__(self, package: ZipFile) -> None:
        self.package = package
        self.remaining = _TOTAL_XML_BYTES

    def xml(self, name: str) -> Xml.Element:
        info = self.package.getinfo(name)
        if (
            info.file_size > min(self.remaining, _XML_BYTES)
            or info.compress_type not in {ZIP_STORED, ZIP_DEFLATED}
            or info.flag_bits & 1
        ):
            raise ValueError("部件超过读取限额或格式不受支持")
        with self.package.open(info) as stream:
            data = stream.read(info.file_size + 1)
        self.remaining -= len(data)
        encoding = "utf-16" if data.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
        text = data.decode(encoding)
        if "<!DOCTYPE" in text or "<!ENTITY" in text:
            raise ValueError("文档正文不需要实体或外部声明")
        return Xml.fromstring(text)


def _paragraphs(root: Xml.Element, path: str) -> Iterator[str]:
    for paragraph in root.iter(path + "p"):
        yield "".join(node.text or "" for node in paragraph.iter(path + "t"))


def _linked_parts(
    parts: _Parts, main: str, nodes: Iterable[Xml.Element], kind: str, pattern: str
) -> Iterator[tuple[Xml.Element, Xml.Element]]:
    folder, name = main.split("/")
    relations = parts.xml(f"{folder}/_rels/{name}.rels")
    targets = {}
    for relation in relations.findall("{*}Relationship"):
        if relation.get("TargetMode") == "External" or not relation.get("Type", "").endswith(kind):
            continue
        target = relation.get("Target", "").removeprefix(f"/{folder}/")
        if re.fullmatch(pattern, target):
            targets[relation.get("Id")] = f"{folder}/{target}"
    for node in nodes:
        key = node.get(_REL_ID)
        if key and key in targets:
            yield node, parts.xml(targets[key])


def _sheet_text(parts: _Parts, workbook: Xml.Element, main: str) -> Iterator[str]:
    sheets = (
        sheet
        for sheet in workbook.findall("{*}sheets/{*}sheet")
        if sheet.get("state", "visible") == "visible"
    )
    shared: list[str] | None = None
    for sheet, root in _linked_parts(
        parts, main, sheets, "/worksheet", r"worksheets/sheet[1-9][0-9]*\.xml"
    ):
        sheet_name = sheet.get("name", "")[:64]
        for row in root.findall("{*}sheetData/{*}row"):
            if row.get("hidden") in {"1", "true"}:
                continue
            for cell in row.findall("{*}c"):
                value = cell.findtext("{*}v", "")
                if cell.get("t") == "s":
                    if shared is None:
                        shared = [
                            "".join(node.text or "" for node in entry.findall(".//{*}t"))
                            for entry in parts.xml("xl/sharedStrings.xml").findall("{*}si")
                        ]
                    value = shared[int(value)] if value.isascii() and value.isdecimal() else ""
                elif cell.get("t") == "inlineStr":
                    value = "".join(node.text or "" for node in cell.findall("{*}is//{*}t"))
                if value:
                    coordinate = cell.get("r", "")[:16]
                    yield f"{sheet_name} {coordinate}：{value[:_TEXT_CHARS]}"


def _slide_text(parts: _Parts, presentation: Xml.Element, main: str) -> Iterator[str]:
    order = presentation.findall("{*}sldIdLst/{*}sldId")
    for _, root in _linked_parts(parts, main, order, "/slide", r"slides/slide[1-9][0-9]*\.xml"):
        if root.get("show") not in {"0", "false"}:
            yield from _paragraphs(root, "{http://schemas.openxmlformats.org/drawingml/2006/main}")


def _office_text(data: bytes, suffix: str) -> str:
    end = data.rfind(b"PK\x05\x06", max(0, len(data) - _ZIP_END_WINDOW))
    if end < 0 or int.from_bytes(data[end + 12 : end + 16], "little") > _ZIP_DIRECTORY_BYTES:
        return ""
    with ZipFile(BytesIO(data)) as package:
        names = package.namelist()
        if (
            len(names) > _MAX_MEMBERS
            or len(set(names)) != len(names)
            or any(
                name.startswith("/") or ".." in name.split("/") or "\\" in name or ":" in name
                for name in names
            )
        ):
            return ""
        parts = _Parts(package)
        main, mime = _OFFICE[suffix]
        manifest = parts.xml("[Content_Types].xml")
        if not any(
            part.get("PartName") == f"/{main}" and part.get("ContentType") == mime + ".main+xml"
            for part in manifest.findall("{*}Override")
        ):
            return ""
        root = parts.xml(main)
        if suffix == ".docx":
            body = root.find("{*}body")
            if body is None:
                return ""
            lines = _paragraphs(
                body, "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
            )
        else:
            lines = (
                _sheet_text(parts, root, main)
                if suffix == ".xlsx"
                else _slide_text(parts, root, main)
            )
        result = []
        remaining = _TEXT_CHARS
        for line in lines:
            if line.strip():
                result.append(line[:remaining])
                remaining -= len(result[-1]) + 1
                if remaining <= 0:
                    break
        return "\n".join(result)


def _pdf_text(data: bytes) -> str:
    executable = shutil.which("pdftotext")
    if executable is None:
        return ""
    with subprocess.Popen(
        [executable, "-q", "-f", "1", "-l", str(_PDF_PAGES), "-enc", "UTF-8", "-nopgbrk", "-", "-"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdin is not None and process.stdout is not None
        input_stream = process.stdin

        def feed() -> None:
            try:
                with input_stream:
                    input_stream.write(data)
            except OSError:
                pass

        writer = Thread(target=feed)
        timer = Timer(_PDF_SECONDS, process.kill)
        writer.start()
        timer.start()
        try:
            output = process.stdout.read(_TEXT_CHARS * 4 + 1)
            if len(output) > _TEXT_CHARS * 4 or process.wait() != 0:
                return ""
            return output.decode("utf-8").strip()[:_TEXT_CHARS]
        finally:
            if process.poll() is None:
                process.kill()
            timer.cancel()
            timer.join()
            writer.join()


def packaged_document_text(data: bytes, name: str) -> str:
    """从真实文档取得有界正文；不支持、超限、无文字或解码失败时不猜。"""
    if len(data) > _ARCHIVE_BYTES or not (mime := document_mime(data, name)):
        return ""
    try:
        if mime == "application/pdf":
            return _pdf_text(data)
        return _office_text(data, Path(name).suffix.lower())
    except (
        BadZipFile,
        EOFError,
        KeyError,
        IndexError,
        NotImplementedError,
        Xml.ParseError,
        ValueError,
        OSError,
        zlib.error,
    ):
        return ""
