from __future__ import annotations

import html
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET


NS = {
    "main": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "rel": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "pkgrel": "http://schemas.openxmlformats.org/package/2006/relationships",
}


def sheet_names(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        workbook = ET.fromstring(archive.read("xl/workbook.xml"))
        names = []
        for sheet in workbook.findall(".//main:sheet", NS):
            if sheet.attrib.get("state") == "hidden":
                continue
            names.append(sheet.attrib["name"])
        return names


def read_rows(path: Path, sheet_name: str | None = None, limit: int | None = None) -> list[list[Any]]:
    with zipfile.ZipFile(path) as archive:
        target = _sheet_target(archive, sheet_name)
        shared = _shared_strings(archive)
        root = ET.fromstring(archive.read(target))
        rows: list[list[Any]] = []
        for row in root.findall(".//main:sheetData/main:row", NS):
            values: list[Any] = []
            for cell in row.findall("main:c", NS):
                column_index = _column_index(cell.attrib.get("r", "A1"))
                while len(values) < column_index - 1:
                    values.append("")
                values.append(_cell_value(cell, shared))
            rows.append(values)
            if limit and len(rows) >= limit:
                break
        return rows


def write_workbook(path: Path, sheets: dict[str, list[list[Any]]], *, styled_review: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", _content_types(len(sheets), styled_review))
        archive.writestr("_rels/.rels", _root_rels())
        archive.writestr("xl/workbook.xml", _workbook_xml(list(sheets)))
        archive.writestr("xl/_rels/workbook.xml.rels", _workbook_rels(len(sheets), styled_review))
        if styled_review:
            archive.writestr("xl/styles.xml", _styles_xml())
        for index, (_, rows) in enumerate(sheets.items(), start=1):
            archive.writestr(f"xl/worksheets/sheet{index}.xml", _sheet_xml(rows, styled_review=styled_review and index == 1))


def _sheet_target(archive: zipfile.ZipFile, sheet_name: str | None) -> str:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    rels = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels.findall("pkgrel:Relationship", NS)}
    sheets = workbook.findall(".//main:sheet", NS)
    selected = None
    for sheet in sheets:
        if sheet.attrib.get("state") == "hidden":
            continue
        if sheet_name is None or sheet.attrib["name"] == sheet_name:
            selected = sheet
            break
    if selected is None:
        raise KeyError(f"Worksheet not found: {sheet_name}")
    rel_id = selected.attrib[f"{{{NS['rel']}}}id"]
    target = rel_map[rel_id]
    target = target.lstrip("/")
    return target if target.startswith("xl/") else "xl/" + target


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    values = []
    for item in root.findall("main:si", NS):
        texts = [node.text or "" for node in item.findall(".//main:t", NS)]
        values.append("".join(texts))
    return values


def _cell_value(cell: ET.Element, shared: list[str]) -> Any:
    value = cell.find("main:v", NS)
    inline = cell.find("main:is/main:t", NS)
    if inline is not None:
        return inline.text or ""
    if value is None:
        return ""
    if cell.attrib.get("t") == "s":
        return shared[int(value.text or "0")]
    return value.text or ""


def _column_index(reference: str) -> int:
    letters = "".join(ch for ch in reference if ch.isalpha())
    total = 0
    for letter in letters:
        total = total * 26 + (ord(letter.upper()) - 64)
    return total


def _content_types(sheet_count: int, styled: bool) -> str:
    sheets = "".join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1, sheet_count + 1))
    styles = '<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>' if styled else ""
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
{sheets}{styles}</Types>'''


def _root_rels() -> str:
    return '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>'''


def _workbook_xml(names: list[str]) -> str:
    sheets = "".join(f'<sheet name="{html.escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i, name in enumerate(names, start=1))
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="{NS['main']}" xmlns:r="{NS['rel']}"><sheets>{sheets}</sheets></workbook>'''


def _workbook_rels(sheet_count: int, styled: bool) -> str:
    rels = "".join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1, sheet_count + 1))
    if styled:
        rels += f'<Relationship Id="rId{sheet_count + 1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    return f'''<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="{NS['pkgrel']}">{rels}</Relationships>'''


def _sheet_xml(rows: list[list[Any]], *, styled_review: bool) -> str:
    xml_rows = []
    for r_index, row in enumerate(rows, start=1):
        cells = []
        for c_index, value in enumerate(row, start=1):
            ref = f"{_column_name(c_index)}{r_index}"
            style = _style_for(c_index, r_index) if styled_review else 0
            if isinstance(value, (int, float)) and value is not None:
                cells.append(f'<c r="{ref}" s="{style}"><v>{value}</v></c>')
            else:
                cells.append(f'<c r="{ref}" s="{style}" t="inlineStr"><is><t>{html.escape("" if value is None else str(value))}</t></is></c>')
        xml_rows.append(f'<row r="{r_index}">{"".join(cells)}</row>')
    auto_filter = f'<autoFilter ref="A1:{_column_name(len(rows[0]) if rows else 1)}{len(rows)}"/>' if styled_review and rows else ""
    freeze = '<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' if styled_review else ""
    return f'''<?xml version="1.0" encoding="UTF-8"?><worksheet xmlns="{NS['main']}">{freeze}<sheetData>{"".join(xml_rows)}</sheetData>{auto_filter}</worksheet>'''


def _styles_xml() -> str:
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="{NS['main']}">
<numFmts count="3"><numFmt numFmtId="164" formatCode="$#,##0.00"/><numFmt numFmtId="165" formatCode="0.00%"/><numFmt numFmtId="166" formatCode="0"/></numFmts>
<fonts count="2"><font/><font><b/><color rgb="FFFFFFFF"/></font></fonts>
<fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FFD71920"/></patternFill></fill></fills>
<borders count="1"><border/></borders>
<cellStyleXfs count="1"><xf/></cellStyleXfs>
<cellXfs count="5"><xf/><xf fontId="1" fillId="2" applyFont="1" applyFill="1"/><xf numFmtId="164" applyNumberFormat="1"/><xf numFmtId="165" applyNumberFormat="1"/><xf numFmtId="166" applyNumberFormat="1"/></cellXfs>
</styleSheet>'''


def _style_for(column: int, row: int) -> int:
    if row == 1:
        return 1
    if column in {5, 6, 7, 9, 10, 11, 12, 13, 15, 16}:
        return 2
    if column in {14, 17}:
        return 3
    return 0


def _column_name(index: int) -> str:
    name = ""
    while index:
        index, rem = divmod(index - 1, 26)
        name = chr(65 + rem) + name
    return name
