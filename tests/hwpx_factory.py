"""테스트용 HWPX 를 코드로 만든다.

실제 서식은 내부 자료라 저장소에 없다. 그래서 `교통비 지급 내역서`(AID 5-05)에서 확인한
셀 구조(빈 셀은 `<run />`, 병합은 앵커 셀만 존재, 문단마다 linesegarray)를 그대로 흉내 낸다.
"""

from __future__ import annotations

import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

HP = "http://www.hancom.co.kr/hwpml/2011/paragraph"
HS = "http://www.hancom.co.kr/hwpml/2011/section"

LINESEG = (
    '<hp:linesegarray><hp:lineseg textpos="0" vertpos="0" vertsize="1000" textheight="1000" '
    'baseline="850" spacing="600" horzpos="0" horzsize="4900" flags="393216"/></hp:linesegarray>'
)


def para(text: str = "", char: str = "10", pr: str = "11", ctrl: str = "") -> str:
    """문단 하나. 빈 문자열이면 <t> 없는 빈 run 이 된다."""
    runs = ""
    if ctrl:
        runs += f'<hp:run charPrIDRef="{char}">{ctrl}</hp:run>'
    if text:
        runs += f'<hp:run charPrIDRef="{char}"><hp:t>{escape(text)}</hp:t></hp:run>'
    elif not ctrl:
        runs += f'<hp:run charPrIDRef="{char}"/>'
    return (
        f'<hp:p id="2147483648" paraPrIDRef="{pr}" styleIDRef="0" pageBreak="0" '
        f'columnBreak="0" merged="0">{runs}{LINESEG}</hp:p>'
    )


def cell(row: int, col: int, content: str = "", *, colspan: int = 1, rowspan: int = 1) -> str:
    """셀 하나. content 는 문단 XML (para() 의 결과) 또는 빈 문자열."""
    body = content or para()
    return (
        '<hp:tc name="" header="0" hasMargin="0" protect="0" editable="0" dirty="0" '
        'borderFillIDRef="8">'
        '<hp:subList id="" textDirection="HORIZONTAL" lineWrap="BREAK" vertAlign="CENTER" '
        'linkListIDRef="0" linkListNextIDRef="0" textWidth="0" textHeight="0" '
        f'hasTextRef="0" hasNumRef="0">{body}</hp:subList>'
        f'<hp:cellAddr colAddr="{col}" rowAddr="{row}"/>'
        f'<hp:cellSpan colSpan="{colspan}" rowSpan="{rowspan}"/>'
        '<hp:cellSz width="5000" height="2824"/>'
        '<hp:cellMargin left="141" right="141" top="141" bottom="141"/></hp:tc>'
    )


def table(rows: int, cols: int, cells_by_row: list[list[str]], height: int | None = None) -> str:
    trs = "".join(f"<hp:tr>{''.join(r)}</hp:tr>" for r in cells_by_row)
    size = (
        f'<hp:sz width="20000" widthRelTo="ABSOLUTE" height="{height}" heightRelTo="ABSOLUTE" '
        'protect="0"/>'
        if height is not None
        else ""
    )
    return (
        f'<hp:tbl id="1" rowCnt="{rows}" colCnt="{cols}" cellSpacing="0" '
        f'borderFillIDRef="6" noAdjust="0">{size}{trs}</hp:tbl>'
    )


def field(name: str, value: str, fid: str = "100") -> str:
    """누름틀이 든 run. fieldBegin / 값 / fieldEnd."""
    return (
        f'<hp:run charPrIDRef="10"><hp:ctrl><hp:fieldBegin id="{fid}" type="CLICK_HERE" '
        f'name="{name}"/></hp:ctrl><hp:t>{escape(value)}</hp:t>'
        f'<hp:ctrl><hp:fieldEnd beginIDRef="{fid}"/></hp:ctrl></hp:run>'
    )


def section(body: str) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        f'<hs:sec xmlns:hs="{HS}" xmlns:hp="{HP}">'
        f'<hp:p id="1" paraPrIDRef="0" styleIDRef="0" pageBreak="0" columnBreak="0" merged="0">'
        f'<hp:run charPrIDRef="0">{body}</hp:run></hp:p></hs:sec>'
    )


def build(path: Path, *sections: str) -> Path:
    """mimetype + section 들로 최소 HWPX 를 만든다."""
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(zipfile.ZipInfo("mimetype"), "application/hwp+zip", zipfile.ZIP_STORED)
        zf.writestr("version.xml", "<v/>", zipfile.ZIP_DEFLATED)
        for i, sec in enumerate(sections):
            zf.writestr(f"Contents/section{i}.xml", sec, zipfile.ZIP_DEFLATED)
    return path


def traffic_form(path: Path) -> Path:
    """교통비 지급 내역서를 줄여 만든 표 (4행 x 4열).

    0행: (0,0)~(0,1) 병합 제목 + 헤더 두 칸
    1행: 예시 행 (연번 1 / 홍길동 / 학번 / 5,000)
    2~3행: 빈 반복 행. 입금액은 5,000 이 미리 채워져 있다.
    """
    rows = [
        [
            cell(0, 0, para("제목", ctrl='<hp:ctrl><hp:colPr id="" type="NEWSPAPER"/></hp:ctrl>'),
                 colspan=2),
            cell(0, 2, para("학번")),
            cell(0, 3, para("입금액(원)")),
        ],
        [
            cell(1, 0, para("1")),
            cell(1, 1, para("홍길동")),
            cell(1, 2, para("2000000000")),
            cell(1, 3, para("5,000")),
        ],
        [cell(2, 0, para("2")), cell(2, 1), cell(2, 2), cell(2, 3, para("5,000"))],
        [cell(3, 0, para("3")), cell(3, 1), cell(3, 2), cell(3, 3, para("5,000"))],
    ]
    return build(path, section(table(4, 4, rows)))


def traffic_form_with_total(path: Path) -> Path:
    """traffic_form 에 합계 행을 붙인 표 (5행 x 4열, 표 높이 14120).

    1행 예시, 2~3행 빈 반복 행(반복영역은 1~3행), 4행이 합계다.
    합계 행은 (4,0)~(4,2) 병합 라벨 + 수식 칸이다.
    """
    rows = [
        [
            cell(0, 0, para("제목"), colspan=2),
            cell(0, 2, para("학번")),
            cell(0, 3, para("입금액(원)")),
        ],
        [
            cell(1, 0, para("1")),
            cell(1, 1, para("홍길동")),
            cell(1, 2, para("2000000000")),
            cell(1, 3, para("5,000")),
        ],
        [cell(2, 0, para("2")), cell(2, 1), cell(2, 2), cell(2, 3, para("5,000"))],
        [cell(3, 0, para("3")), cell(3, 1), cell(3, 2), cell(3, 3, para("5,000"))],
        [cell(4, 0, para("합계"), colspan=3), cell(4, 3, para("15,000"))],
    ]
    return build(path, section(table(5, 4, rows, height=14120)))


def formula(expr: str, result: str, fmt: str = "%g,", fid: str = "500") -> str:
    """수식(FORMULA 누름틀)이 든 run. 한글이 저장하는 구조를 그대로 흉내 낸다."""
    return (
        f'<hp:run charPrIDRef="10"><hp:ctrl><hp:fieldBegin id="{fid}" type="FORMULA" name="" '
        'editable="0" dirty="0" zorder="-1" fieldid="1" metaTag="">'
        '<hp:parameters cnt="5" name=""><hp:integerParam name="Prop">8</hp:integerParam>'
        f'<hp:stringParam name="Command">{escape(expr)}??{fmt};;{result}</hp:stringParam>'
        f'<hp:stringParam name="Formula">{escape(expr)}</hp:stringParam>'
        f'<hp:stringParam name="ResultFormat">{fmt}</hp:stringParam>'
        f'<hp:stringParam name="LastResult">{result}</hp:stringParam>'
        '</hp:parameters></hp:fieldBegin></hp:ctrl>'
        f'<hp:t>{result}</hp:t><hp:ctrl><hp:fieldEnd beginIDRef="{fid}" fieldid="1"/></hp:ctrl>'
        "<hp:t/></hp:run>"
    )


def formula_para(expr: str, result: str, fmt: str = "%g,", fid: str = "500") -> str:
    return para().replace('<hp:run charPrIDRef="10"/>', formula(expr, result, fmt, fid))


def traffic_form_with_formula(path: Path) -> Path:
    """합계가 수식(=SUM(ABOVE))인 표. traffic_form_with_total 과 같은 모양이다.

    반복영역은 1~3행, 4행이 합계다. 입금액은 3행 모두 5,000, 합계 수식의 저장값은 15,000.
    """
    rows = [
        [
            cell(0, 0, para("제목"), colspan=2),
            cell(0, 2, para("학번")),
            cell(0, 3, para("입금액(원)")),
        ],
        [cell(1, 0, para("1")), cell(1, 1, para("홍길동")), cell(1, 2), cell(1, 3, para("5,000"))],
        [cell(2, 0, para("2")), cell(2, 1), cell(2, 2), cell(2, 3, para("5,000"))],
        [cell(3, 0, para("3")), cell(3, 1), cell(3, 2), cell(3, 3, para("5,000"))],
        [
            cell(4, 0, para("합계"), colspan=3),
            cell(4, 3, formula_para("=SUM(ABOVE)", "15,000")),
        ],
    ]
    return build(path, section(table(5, 4, rows, height=14120)))
