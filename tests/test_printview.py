"""HWPX → 인쇄용 HTML (PDF로 저장). 서식 모양대로 그리는지, 쪽을 한글처럼 나누는지."""

from __future__ import annotations

import re

from docflow.hwpx.printview import render
from tests import hwpx_factory as hf
from tests.test_schema_extract import tbl


def para_at(text: str, vertpos: int, *, page_break: bool = False) -> str:
    """한글이 저장한 줄 위치(vertpos)가 붙은 문단."""
    return (
        f'<hp:p id="0" paraPrIDRef="0" styleIDRef="0" pageBreak="{int(page_break)}" '
        f'columnBreak="0" merged="0"><hp:run charPrIDRef="0"><hp:t>{text}</hp:t></hp:run>'
        f'<hp:linesegarray><hp:lineseg textpos="0" vertpos="{vertpos}" vertsize="1000" '
        'textheight="1000" baseline="850" spacing="600" horzpos="0" horzsize="40000" '
        'flags="0"/></hp:linesegarray></hp:p>'
    )


def html_of(tmp_path, *paras: str) -> str:
    return render(hf.build(tmp_path / "doc.hwpx", hf.body(*paras)).read_bytes(), "문서")


def pages(page: str) -> int:
    return page.count('<section class="page"')


def test_문단과_표를_그린다(tmp_path):
    table = tbl(2, 2, (0, 0, "회의명", 2), (1, 0, "일시"), (1, 1, "2026.11.17"))
    page = html_of(
        tmp_path,
        hf.para("상기와 같이 <제출> 합니다."),
        hf.para_runs(f'<hp:run charPrIDRef="0">{table}</hp:run>'),
    )
    assert "상기와 같이 &lt;제출&gt; 합니다." in page  # 글자는 이스케이프한다
    assert page.count("<table") == 1 and 'colspan="2"' in page
    assert "2026.11.17" in page
    assert pages(page) == 1


def test_한글이_새_쪽에서_시작한_문단부터_새_쪽이다(tmp_path):
    page = html_of(
        tmp_path,
        para_at("1쪽 첫 줄", 0),
        para_at("1쪽 끝", 60000),
        para_at("2쪽 첫 줄", 0),  # 줄 위치가 다시 작아졌다 = 한글이 새 쪽에 놓았다
        para_at("3쪽", 1800, page_break=True),  # 쪽 나누기
    )
    assert pages(page) == 3
    sections = re.findall(r'<section class="page".*?</section>', page, re.S)
    assert "1쪽 끝" in sections[0] and "2쪽 첫 줄" in sections[1] and "3쪽" in sections[2]


def test_종이_아래에_붙은_글상자는_쪽_위에_따로_놓고_본문은_그_위까지_쓴다(tmp_path):
    box = (
        '<hp:rect id="1"><hp:drawText><hp:subList vertAlign="TOP">'
        + hf.para("시행 2026. 11. 10.")
        + '</hp:subList></hp:drawText><hp:sz width="51660" height="33411"/>'
        '<hp:pos treatAsChar="0" vertRelTo="PAPER" horzRelTo="PAPER" vertAlign="BOTTOM" '
        'horzAlign="CENTER" vertOffset="2835" horzOffset="0"/></hp:rect>'
    )
    page = html_of(
        tmp_path, hf.para("본문"), hf.para_runs(f'<hp:run charPrIDRef="0">{box}</hp:run>')
    )
    assert pages(page) == 1
    assert re.search(r'<div class="float" style="left:[\d.]+mm;top:[\d.]+mm">.*시행 2026', page)
    room = float(re.search(r'data-room="([\d.]+)"', page).group(1))
    assert 0.5 < room < 1  # 글상자 위까지만 본문 자리


def test_인쇄_안내와_자동_인쇄(tmp_path):
    path = hf.build(tmp_path / "doc.hwpx", hf.body(hf.para("본문")))
    plain = render(path.read_bytes(), "문서")
    assert 'class="bar no-print"' not in plain and "window.print(), 300" not in plain
    shown = render(path.read_bytes(), "문서", toolbar=True, auto=True)
    assert 'class="bar no-print"' in shown and "window.print(), 300" in shown
