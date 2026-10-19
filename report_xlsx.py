"""Small dependency-free XLSX writer for the batch work report."""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from xml.sax.saxutils import escape
from zipfile import ZIP_DEFLATED, ZipFile


DETAIL_COLUMNS = [
    ("행", "row_number"), ("원어 단어", "word"), ("한국어 뜻", "meaning_ko"),
    ("상태", "status"), ("제작 판단·생략 이유", "decision_reason"),
    ("연상 장면", "scene"), ("기억 단서", "memory_cue"),
    ("뜻을 보여주는 단서", "visual_signal"),
    ("다람쥐 등장", "use_squirrel"), ("문자 사용", "use_text"),
    ("반복 사물", "object_keys"), ("참고한 기존 그림", "object_reference_files"),
    ("검토 지적 사항", "review_notes"), ("수정 횟수", "revision_count"),
    ("선택한 그림", "selected_version"),
    ("최종 판정·실패 사유", "final_reason"), ("납품 파일", "image_file"),
    ("API 호출 수", "api_calls"), ("입력 토큰", "input_tokens"),
    ("출력 토큰", "output_tokens"), ("이미지 토큰", "image_tokens"),
]


def _cell(value: object, style: int = 0) -> str:
    if value is None:
        value = ""
    if isinstance(value, bool):
        value = "예" if value else "아니요"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return f'<c s="{style}"><v>{value}</v></c>'
    # OOXML inline strings avoid shared-string bookkeeping and Excel formula injection.
    data = str(value).replace("\x00", "")
    if data[:1] in ("=", "+", "-", "@"):
        data = "'" + data
    return f'<c s="{style}" t="inlineStr"><is><t xml:space="preserve">{escape(data)}</t></is></c>'


def _sheet(rows: list[list[object]], widths: list[int], freeze: bool, filter_rows: bool) -> bytes:
    body = []
    for number, row in enumerate(rows, 1):
        style = 1 if number == 1 else 0
        if number > 1 and filter_rows and len(row) > 3:
            style = {"generated": 2, "revised": 2, "skipped": 3,
                     "discarded": 4, "error": 4}.get(str(row[3]), 0)
        body.append(f'<row r="{number}">' + "".join(_cell(v, style) for v in row) + "</row>")
    cols = "".join(f'<col min="{i}" max="{i}" width="{width}" customWidth="1"/>'
                   for i, width in enumerate(widths, 1))
    max_col = _col(len(widths))
    views = ('<sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" '
             'activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>' if freeze else "")
    autofilter = f'<autoFilter ref="A1:{max_col}{len(rows)}"/>' if filter_rows and rows else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'{views}<cols>{cols}</cols><sheetData>{"".join(body)}</sheetData>{autofilter}'
            '</worksheet>').encode("utf-8")


def _col(index: int) -> str:
    value = ""
    while index:
        index, rem = divmod(index - 1, 26)
        value = chr(65 + rem) + value
    return value


def write_report(path: Path, job: dict, items: list[dict]) -> None:
    counts = Counter(item["status"] for item in items)
    delivered = [i["image_file"] for i in items if i["status"] in ("generated", "revised")]
    summary = [
        ["시사보카 연상 삽화 작업보고서", "값"],
        ["작업 ID", job["id"]],
        ["시작 시각", job.get("created_at", "")],
        ["최근 갱신", job.get("updated_at", "")],
        ["작업 상태", job["status"]],
        ["입력 건수", len(items)],
        ["원본 선택 건수", counts["generated"]],
        ["수정본 선택 건수", counts["revised"]],
        ["수정 시도 건수", sum(bool(i.get("revision_count")) for i in items)],
        ["기존 사물 참고 건수", sum(bool(i.get("object_reference_files")) for i in items)],
        ["생략 건수", counts["skipped"]],
        ["이전 작업 폐기 건수", counts["discarded"]],
        ["오류 건수", counts["error"]],
        ["미완료 건수", sum(counts[s] for s in ("queued", "planning", "generating", "reviewing", "revising"))],
        ["납품 PNG 건수", len(delivered)],
        ["판단·검토 모델", "gpt-6-sol"],
        ["이미지 모델", "gpt-image-2"],
        ["이미지 설정", "gpt-image-2 low, 1024x1024 생성 → 800x800 불투명 #FFFFFF PNG"],
        ["성공한 API 작업 수", sum(i["api_calls"] for i in items)],
        ["입력 토큰", sum(i["input_tokens"] for i in items)],
        ["출력 토큰", sum(i["output_tokens"] for i in items)],
        ["이미지 토큰", sum(i["image_tokens"] for i in items)],
        ["납품 파일 목록", ", ".join(delivered)],
        ["비용", "단가 및 과금 세부 내역 미확인으로 미기재"],
    ]
    detail = [[label for label, _ in DETAIL_COLUMNS]]
    detail += [[", ".join(value) if isinstance(value := item.get(key, ""), list) else value
                for _, key in DETAIL_COLUMNS] for item in items]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with ZipFile(tmp, "w", ZIP_DEFLATED) as out:
        out.writestr("[Content_Types].xml", '''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>''')
        out.writestr("_rels/.rels", '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>''')
        out.writestr("xl/workbook.xml", '''<?xml version="1.0" encoding="UTF-8"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
<sheets><sheet name="요약" sheetId="1" r:id="rId1"/><sheet name="단어별 상세" sheetId="2" r:id="rId2"/></sheets></workbook>''')
        out.writestr("xl/_rels/workbook.xml.rels", '''<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>''')
        out.writestr("xl/styles.xml", '''<?xml version="1.0" encoding="UTF-8"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<fonts count="2"><font><sz val="11"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/></font></fonts>
<fills count="5"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF634629"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFE6F3E9"/></patternFill></fill><fill><patternFill patternType="solid"><fgColor rgb="FFFFE8E8"/></patternFill></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="5"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFill="1" applyFont="1"/><xf numFmtId="0" fontId="0" fillId="3" borderId="0" xfId="0" applyFill="1"/><xf numFmtId="0" fontId="0" fillId="1" borderId="0" xfId="0"/><xf numFmtId="0" fontId="0" fillId="4" borderId="0" xfId="0" applyFill="1"/></cellXfs>
</styleSheet>''')
        out.writestr("xl/worksheets/sheet1.xml", _sheet(summary, [31, 100], False, False))
        out.writestr("xl/worksheets/sheet2.xml", _sheet(detail, [8, 26, 28, 13, 52, 52, 40, 43, 14, 12, 28, 45, 55, 11, 14, 55, 28, 12, 13, 13, 13], True, True))
    tmp.replace(path)
