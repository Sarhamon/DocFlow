"""HWPX 표 셀·반복영역 쓰기 테스트.

셀 구조는 `교통비 지급 내역서`(AID 5-05)에서 확인한 것을 tests/hwpx_factory.py 로 재현한다.
"""

from __future__ import annotations

import zipfile

import pytest

from docflow.hwpx.document import (
    HP,
    CapacityError,
    CellNotFound,
    CoveredCell,
    FieldInCell,
    HwpxDocument,
    HwpxError,
    RowSpanConflict,
)
from tests import hwpx_factory as hf


@pytest.fixture
def doc(tmp_path):
    return HwpxDocument.open(hf.traffic_form(tmp_path / "form.hwpx"))


def reopen(doc: HwpxDocument, tmp_path) -> HwpxDocument:
    return HwpxDocument.open(doc.save(tmp_path / "out.hwpx"))


# ------------------------------------------------------------------ 읽기
def test_표_구조를_읽는다(doc):
    t = doc.tables()[0]
    assert (t.rows, t.cols) == (4, 4)
    assert t.grid()[1] == ["1", "홍길동", "2000000000", "5,000"]


def test_병합된_칸은_앵커만_존재한다(doc):
    t = doc.tables()[0]
    anchors = {(c.row, c.col): c for c in t.cells}
    assert anchors[(0, 0)].col_span == 2
    assert (0, 1) not in anchors


# ------------------------------------------------------------------ set_cell
def test_빈_셀에_값을_쓴다(doc):
    doc.set_cell(0, 2, 1, "김철수")
    assert doc.get_cell(0, 2, 1) == "김철수"


def test_쓴_값은_저장하고_다시_열어도_남는다(doc, tmp_path):
    doc.set_cell(0, 2, 1, "김철수")
    assert reopen(doc, tmp_path).get_cell(0, 2, 1) == "김철수"


def test_기존_값을_덮어쓴다(doc):
    doc.set_cell(0, 1, 1, "이영희")
    assert doc.get_cell(0, 1, 1) == "이영희"


def test_빈_문자열은_셀을_비운다(doc):
    doc.set_cell(0, 1, 1, "")
    assert doc.get_cell(0, 1, 1) == ""


def test_문자_모양과_문단_모양을_유지한다(doc):
    doc.set_cell(0, 2, 1, "김철수")
    ns = {"hp": HP}
    tc = next(
        tc
        for tc in doc._root("Contents/section0.xml").iterfind(".//hp:tc", ns)
        if tc.find("hp:cellAddr", ns).get("rowAddr") == "2"
        and tc.find("hp:cellAddr", ns).get("colAddr") == "1"
    )
    p = tc.find(".//hp:p", ns)
    assert p.get("paraPrIDRef") == "11"
    assert p.find("hp:run", ns).get("charPrIDRef") == "10"


def test_줄배치_캐시는_지운다(doc):
    """글자가 바뀌면 linesegarray 가 틀리므로 지워서 한글이 다시 계산하게 한다."""
    doc.set_cell(0, 1, 1, "아주 긴 이름으로 바꿈")
    ns = {"hp": HP}
    xml = doc._root("Contents/section0.xml")
    target = next(
        tc
        for tc in xml.iterfind(".//hp:tc", ns)
        if tc.find("hp:cellAddr", ns).get("rowAddr") == "1"
        and tc.find("hp:cellAddr", ns).get("colAddr") == "1"
    )
    assert target.find(".//hp:linesegarray", ns) is None


def test_여러_줄은_문단으로_나뉜다(doc):
    doc.set_cell(0, 2, 1, "첫째\n둘째\n셋째")
    assert doc.get_cell(0, 2, 1) == "첫째\n둘째\n셋째"


def test_줄이_줄면_남는_문단을_지운다(doc):
    doc.set_cell(0, 2, 1, "첫째\n둘째\n셋째")
    doc.set_cell(0, 2, 1, "하나")
    assert doc.get_cell(0, 2, 1) == "하나"


def test_제어문자_run_은_보존한다(doc):
    doc.set_cell(0, 0, 0, "새 제목")
    assert doc.get_cell(0, 0, 0) == "새 제목"
    ns = {"hp": HP}
    assert doc._root("Contents/section0.xml").find(".//hp:colPr", ns) is not None


def test_병합_셀은_앵커에_쓴다(doc):
    doc.set_cell(0, 0, 0, "새 제목")
    assert doc.get_cell(0, 0, 0) == "새 제목"


def test_병합으로_가려진_칸은_거부하고_앵커를_알려준다(doc):
    with pytest.raises(CoveredCell, match=r"\(0,0\)"):
        doc.set_cell(0, 0, 1, "x")


def test_없는_칸과_표는_거부한다(doc):
    with pytest.raises(CellNotFound):
        doc.set_cell(0, 9, 9, "x")
    with pytest.raises(CellNotFound):
        doc.set_cell(5, 0, 0, "x")


def test_누름틀이_든_칸은_거부한다(tmp_path):
    content = hf.para().replace('<hp:run charPrIDRef="10"/>', hf.field("a", "값"))
    rows = [[hf.cell(0, 0, content)]]
    doc = HwpxDocument.open(hf.build(tmp_path / "f.hwpx", hf.section(hf.table(1, 1, rows))))
    with pytest.raises(FieldInCell):
        doc.set_cell(0, 0, 0, "x")


def test_중첩_표가_든_문단은_지우지_않는다(tmp_path):
    inner = hf.table(1, 1, [[hf.cell(0, 0, hf.para("안쪽"))]])
    outer_content = hf.para("바깥").replace("</hp:run>", f"</hp:run><hp:run>{inner}</hp:run>", 1)
    rows = [[hf.cell(0, 0, outer_content)]]
    doc = HwpxDocument.open(hf.build(tmp_path / "f.hwpx", hf.section(hf.table(1, 1, rows))))
    assert len(doc.tables()) == 2
    doc.set_cell(0, 0, 0, "새 바깥")
    assert len(doc.tables()) == 2  # 안쪽 표가 복제되지 않는다
    assert doc.tables()[1].grid() == [["안쪽"]]
    assert "새 바깥" in doc.get_cell(0, 0, 0)


# ------------------------------------------------------------------ fill_rows
def test_반복영역에_여러_행을_쓴다(doc):
    n = doc.fill_rows(0, 2, [["", "가", "1", None], ["", "나", "2", None]], col=0)
    # "" 도 쓰기이므로 None 만 건너뛴다
    assert n == 6
    assert doc.get_cell(0, 2, 1) == "가"
    assert doc.get_cell(0, 3, 2) == "2"
    assert doc.get_cell(0, 2, 3) == "5,000"  # None 칸의 미리 채워진 값은 그대로


def test_행이_모자라면_아무것도_쓰지_않는다(doc):
    before = doc.tables()[0].grid()
    with pytest.raises(CapacityError, match="필요한데"):
        doc.fill_rows(0, 2, [["", "가"], ["", "나"], ["", "다"]])
    assert doc.tables()[0].grid() == before


def test_가려진_칸이_섞이면_아무것도_쓰지_않는다(doc):
    before = doc.tables()[0].grid()
    with pytest.raises(CoveredCell):
        doc.fill_rows(0, 0, [["a", "b"]], col=0)  # (0,1) 은 가려진 칸
    assert doc.tables()[0].grid() == before


# ------------------------------------------------------------------ 저장
def test_값을_안_쓴_구역은_바이트가_그대로다(tmp_path):
    p = hf.build(
        tmp_path / "two.hwpx",
        hf.section(hf.table(1, 1, [[hf.cell(0, 0)]])),
        hf.section(hf.table(1, 1, [[hf.cell(0, 0, hf.para("그대로"))]])),
    )
    doc = HwpxDocument.open(p)
    doc.set_cell(0, 0, 0, "바뀜")
    out = doc.save(tmp_path / "out.hwpx")
    with zipfile.ZipFile(p) as a, zipfile.ZipFile(out) as b:
        assert a.read("Contents/section1.xml") == b.read("Contents/section1.xml")
        assert a.read("Contents/section0.xml") != b.read("Contents/section0.xml")


def test_mimetype은_첫_항목이고_압축하지_않는다(doc, tmp_path):
    doc.set_cell(0, 2, 1, "x")
    out = doc.save(tmp_path / "out.hwpx")
    with zipfile.ZipFile(out) as zf:
        first = zf.infolist()[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED


def test_누름틀_채우기와_표_쓰기를_함께_쓴다(tmp_path):
    rows = [[hf.cell(0, 0)]]
    body = hf.field("docnumber", "○○○○처-000000") + hf.table(1, 1, rows)
    doc = HwpxDocument.open(hf.build(tmp_path / "f.hwpx", hf.section(body)))
    assert doc.fill({"docnumber": "교육처-123456"}) == 1
    doc.set_cell(0, 0, 0, "표 값")
    again = reopen(doc, tmp_path)
    assert again.fields()[0].value == "교육처-123456"
    assert again.get_cell(0, 0, 0) == "표 값"


# ------------------------------------------------------------------ 행 복제
@pytest.fixture
def total_doc(tmp_path):
    """반복영역은 1~3행, 4행이 합계인 표."""
    return HwpxDocument.open(hf.traffic_form_with_total(tmp_path / "total.hwpx"))


def _height(doc: HwpxDocument) -> int:
    ns = {"hp": HP}
    return int(doc._table_elements()[0][1].find("hp:sz", ns).get("height"))


def test_행을_복제하면_아래_행이_밀린다(total_doc):
    new = total_doc.insert_row(0, 3)
    assert new == 4
    t = total_doc.tables()[0]
    assert t.rows == 6
    assert [r[0] for r in t.grid()] == ["제목", "1", "2", "3", "", "합계"]
    assert t.grid()[5][3] == "15,000"  # 합계 행의 내용은 그대로


def test_복제한_행의_글자는_기본으로_비운다(total_doc):
    total_doc.insert_row(0, 3)
    assert total_doc.tables()[0].grid()[4] == ["", "", "", ""]


def test_clear_False면_글자를_그대로_복제한다(total_doc):
    total_doc.insert_row(0, 3, clear=False)
    assert total_doc.tables()[0].grid()[4] == ["3", "", "", "5,000"]


def test_복제한_행은_쓸_수_있다(total_doc):
    total_doc.insert_row(0, 3)
    total_doc.set_cell(0, 4, 1, "새 사람")
    assert total_doc.get_cell(0, 4, 1) == "새 사람"
    assert total_doc.get_cell(0, 5, 0) == "합계"


def test_복제하면_표_높이가_늘어난다(total_doc):
    assert _height(total_doc) == 14120
    total_doc.insert_row(0, 3)
    assert _height(total_doc) == 14120 + 2824


def test_병합된_행도_복제한다(total_doc):
    """합계 행은 (4,0)~(4,2) 가 병합이지만 모든 칸이 그 행에서 시작하므로 복제할 수 있다."""
    total_doc.insert_row(0, 4)
    t = total_doc.tables()[0]
    anchors = {(c.row, c.col): c for c in t.cells}
    assert anchors[(5, 0)].col_span == 3


def test_행병합이_걸친_행은_복제를_거부한다(tmp_path):
    rows = [
        [cell_ for cell_ in (hf.cell(0, 0, hf.para("a"), rowspan=2), hf.cell(0, 1))],
        [hf.cell(1, 1)],
    ]
    doc = HwpxDocument.open(hf.build(tmp_path / "f.hwpx", hf.section(hf.table(2, 2, rows))))
    before = doc.tables()[0].grid()
    with pytest.raises(RowSpanConflict):
        doc.insert_row(0, 0)  # (0,0) 이 2행에 걸쳐 있다
    with pytest.raises(RowSpanConflict):
        doc.insert_row(0, 1)  # 1행에는 위 행의 병합 때문에 칸이 하나뿐이다
    assert doc.tables()[0].grid() == before


def test_누름틀이_든_행은_복제를_거부하고_문서를_바꾸지_않는다(tmp_path):
    content = hf.para().replace('<hp:run charPrIDRef="10"/>', hf.field("a", "값"))
    rows = [[hf.cell(0, 0, content)]]
    doc = HwpxDocument.open(hf.build(tmp_path / "f.hwpx", hf.section(hf.table(1, 1, rows))))
    with pytest.raises(FieldInCell):
        doc.insert_row(0, 0)
    assert doc.tables()[0].rows == 1


def test_없는_행은_거부한다(total_doc):
    with pytest.raises(CellNotFound):
        total_doc.insert_row(0, 9)


# ------------------------------------------------------------------ fill_rows 확장
def test_영역_끝을_알려주면_합계_행을_덮어쓰지_않는다(total_doc):
    before = total_doc.tables()[0].grid()
    with pytest.raises(CapacityError):
        total_doc.fill_rows(0, 2, [["", "가"], ["", "나"], ["", "다"]], last_row=3)
    assert total_doc.tables()[0].grid() == before
    assert total_doc.get_cell(0, 4, 0) == "합계"


def test_extend면_행을_늘려서_쓴다(total_doc):
    people = [[str(i), f"사람{i}", None, None] for i in range(2, 7)]  # 5명, 2행부터
    total_doc.fill_rows(0, 2, people, last_row=3, extend=True)
    t = total_doc.tables()[0]
    assert t.rows == 5 + 3  # 반복영역 2~6행 + 위 2행 + 합계 1행
    assert [r[1] for r in t.grid()[2:7]] == ["사람2", "사람3", "사람4", "사람5", "사람6"]
    assert t.grid()[7][0] == "합계"


def test_늘어난_행의_미리채움_값은_비어있다(total_doc):
    total_doc.fill_rows(0, 2, [["2", "가", None, None]] * 3, last_row=3, extend=True)
    assert total_doc.get_cell(0, 4, 3) == ""  # 복제한 행의 5,000 은 비워진다
    assert total_doc.get_cell(0, 2, 3) == "5,000"  # 원래 행은 건드리지 않는다


def test_extend_도중_실패하면_행도_되돌린다(total_doc, tmp_path):
    before = total_doc.tables()[0].grid()
    h = _height(total_doc)
    bad = [["a", "b", "c", "d", "너무 많은 열"]] * 4  # 5번째 열은 표에 없다
    with pytest.raises(CellNotFound):
        total_doc.fill_rows(0, 2, bad, last_row=3, extend=True)
    assert total_doc.tables()[0].grid() == before
    assert _height(total_doc) == h
    assert reopen(total_doc, tmp_path).tables()[0].grid() == before


def test_영역이_표_밖이면_거부한다(total_doc):
    with pytest.raises(CellNotFound):
        total_doc.fill_rows(0, 2, [["x"]], last_row=9)
    with pytest.raises(CellNotFound):
        total_doc.fill_rows(0, 3, [["x"]], last_row=2)


# ------------------------------------------------------------------ 수식
@pytest.fixture
def fdoc(tmp_path):
    """합계가 =SUM(ABOVE) 수식인 표 (반복영역 1~3행, 합계 4행)."""
    return HwpxDocument.open(hf.traffic_form_with_formula(tmp_path / "f.hwpx"))


def _formula_params(doc: HwpxDocument) -> dict[str, str]:
    from docflow.hwpx.document import FIELD_BEGIN, _params

    begin = next(doc._root("Contents/section0.xml").iter(FIELD_BEGIN))
    return _params(begin)


def test_수식_칸을_찾는다(fdoc):
    [f] = fdoc.formula_cells()
    assert (f.table, f.row, f.col) == (0, 4, 3)
    assert f.formula == "=SUM(ABOVE)"
    assert f.result_format == "%g,"
    assert f.text == "15,000"


def test_수식_결과를_쓰면_표시값과_저장값이_함께_바뀐다(fdoc):
    fdoc.set_formula_result(0, 4, 3, "84,000")
    assert fdoc.get_cell(0, 4, 3) == "84,000"
    params = _formula_params(fdoc)
    assert params["LastResult"] == "84,000"
    assert params["Command"].endswith(";;84,000")
    assert params["Command"].startswith("=SUM(ABOVE)")  # 수식 자체는 그대로


def test_수식_결과는_저장하고_다시_열어도_남는다(fdoc, tmp_path):
    fdoc.set_formula_result(0, 4, 3, "84,000")
    again = reopen(fdoc, tmp_path)
    assert again.formula_cells()[0].text == "84,000"
    assert again.formula_cells()[0].formula == "=SUM(ABOVE)"


def test_수식이_없는_칸에는_거부한다(fdoc):
    with pytest.raises(HwpxError, match="수식이 없다"):
        fdoc.set_formula_result(0, 1, 3, "1")


def test_set_cell로_수식_칸을_덮어쓰지_못한다(fdoc):
    with pytest.raises(FieldInCell):
        fdoc.set_cell(0, 4, 3, "1")


def test_재계산하면_합계가_입력값을_따라간다(fdoc):
    fdoc.set_cell(0, 2, 3, "7,000")
    fdoc.set_cell(0, 3, 3, "3,000")
    result = fdoc.recalculate_formulas()
    assert fdoc.get_cell(0, 4, 3) == "15,000"  # 5,000 + 7,000 + 3,000
    assert result.updated == []  # 값이 같으므로 바뀐 것이 없다
    fdoc.set_cell(0, 3, 3, "9,000")
    result = fdoc.recalculate_formulas()
    assert fdoc.get_cell(0, 4, 3) == "21,000"
    assert [(u.row, u.col, u.text) for u in result.updated] == [(4, 3, "21,000")]
    assert result.skipped == []


def test_재계산은_여러번_해도_같다(fdoc):
    fdoc.set_cell(0, 2, 3, "1")
    fdoc.recalculate_formulas()
    assert fdoc.recalculate_formulas().updated == []


def test_행을_늘려_쓰고_재계산하면_합계가_맞다(fdoc):
    """행 복제 후 합계가 낡은 값으로 남던 문제의 회귀 테스트."""
    people = [[str(i), f"사람{i}", None, "7,000"] for i in range(2, 7)]  # 5명
    fdoc.fill_rows(0, 2, people, last_row=3, extend=True)
    assert fdoc.get_cell(0, 7, 3) == "15,000"  # 재계산 전에는 낡은 값
    fdoc.recalculate_formulas()
    assert fdoc.get_cell(0, 7, 3) == "40,000"  # 5,000 + 7,000 x 5


def test_수식끼리_이어진_계산도_맞춘다(tmp_path):
    """금액 = 수량 x 단가 (수식), 합계 = 금액들의 합 (수식)."""
    rows = [
        [hf.cell(0, 0, hf.para("수량")), hf.cell(0, 1, hf.para("단가")),
         hf.cell(0, 2, hf.para("금액"))],
        [hf.cell(1, 0, hf.para("2")), hf.cell(1, 1, hf.para("1,000")),
         hf.cell(1, 2, hf.formula_para("=PRODUCT(A?:B?)", "0", fid="501"))],
        [hf.cell(2, 0, hf.para("3")), hf.cell(2, 1, hf.para("500")),
         hf.cell(2, 2, hf.formula_para("=PRODUCT(A?:B?)", "0", fid="502"))],
        [hf.cell(3, 0, hf.para("합계"), colspan=2),
         hf.cell(3, 2, hf.formula_para("=SUM(ABOVE)", "0", fid="503"))],
    ]
    doc = HwpxDocument.open(hf.build(tmp_path / "c.hwpx", hf.section(hf.table(4, 3, rows))))
    result = doc.recalculate_formulas()
    assert [doc.get_cell(0, r, 2) for r in (1, 2, 3)] == ["2,000", "1,500", "3,500"]
    assert len(result.updated) == 3


def test_계산할_수_없는_수식은_그대로_두고_알려준다(tmp_path):
    rows = [
        [hf.cell(0, 0, hf.para("1")),
         hf.cell(0, 1, hf.formula_para("=AVERAGE(A1:A1)", "99", fid="601"))],
        [hf.cell(1, 0, hf.para("합계")),
         hf.cell(1, 1, hf.formula_para("=SUM(ABOVE)", "0", fid="602"))],
    ]
    doc = HwpxDocument.open(hf.build(tmp_path / "u.hwpx", hf.section(hf.table(2, 2, rows))))
    result = doc.recalculate_formulas()
    assert doc.get_cell(0, 0, 1) == "99"  # 낡았을 수 있어도 추측해서 바꾸지 않는다
    [(cell, reason)] = result.skipped
    assert (cell.row, cell.col) == (0, 1)
    assert "AVERAGE" in reason
    assert doc.get_cell(0, 1, 1) == "99"  # 위 칸이 숫자(99)이므로 합계는 99


def test_표를_지정해_재계산한다(tmp_path):
    f1 = hf.table(2, 1, [[hf.cell(0, 0, hf.para("5"))],
                         [hf.cell(1, 0, hf.formula_para("=SUM(ABOVE)", "0", fid="701"))]])
    f2 = hf.table(2, 1, [[hf.cell(0, 0, hf.para("7"))],
                         [hf.cell(1, 0, hf.formula_para("=SUM(ABOVE)", "0", fid="702"))]])
    doc = HwpxDocument.open(hf.build(tmp_path / "t.hwpx", hf.section(f1 + f2)))
    doc.recalculate_formulas(table=1)
    assert doc.get_cell(0, 1, 0) == "0"  # 표 0 은 건드리지 않는다
    assert doc.get_cell(1, 1, 0) == "7"


# ------------------------------------------------------------ 점검에서 찾은 문제의 회귀 테스트
def _valid_xml(doc: HwpxDocument, tmp_path) -> None:
    from xml.etree import ElementTree as ET

    out = doc.save(tmp_path / "xml.hwpx")
    with zipfile.ZipFile(out) as zf:
        for name in zf.namelist():
            if name.startswith("Contents/section"):
                ET.fromstring(zf.read(name))  # 깨졌으면 ParseError


def test_XML에_못_들어가는_제어문자는_지우고_쓴다(doc, tmp_path):
    """수직탭(\x0b) 같은 문자가 그대로 들어가면 XML 이 깨져 문서가 손상된다."""
    doc.set_cell(0, 2, 1, "김\x0b철수\x00\x1f")
    assert doc.get_cell(0, 2, 1) == "김철수"
    _valid_xml(doc, tmp_path)


def test_누름틀과_수식_값의_제어문자도_지운다(tmp_path):
    formula_cell = hf.cell(0, 0, hf.formula_para("=SUM(ABOVE)", "0"))
    body = hf.field("docnumber", "x") + hf.table(1, 1, [[formula_cell]])
    doc = HwpxDocument.open(hf.build(tmp_path / "f.hwpx", hf.section(body)))
    doc.fill({"docnumber": "A\x0bB"})
    doc.set_formula_result(0, 0, 0, "1\x0c2")
    assert doc.fields()[0].value == "AB"
    assert doc.get_cell(0, 0, 0) == "12"
    _valid_xml(doc, tmp_path)


def test_탭과_줄바꿈은_지우지_않는다(doc):
    doc.set_cell(0, 2, 1, "가\t나\n다")
    assert doc.get_cell(0, 2, 1) == "가\t나\n다"


def test_글자가_더_붙은_수식_칸도_재계산이_일관된다(tmp_path):
    """칸이 '90.0%' 처럼 수식 결과 + 글자일 때 재계산이 매번 '갱신'으로 나오면 안 된다."""
    pct = hf.para().replace(
        '<hp:run charPrIDRef="10"/>',
        hf.formula("=c1/b1*100", "0", fmt="%.1f", fid="801")
        + '<hp:run charPrIDRef="10"><hp:t>%</hp:t></hp:run>',
    )
    rows = [[hf.cell(0, 0, pct), hf.cell(0, 1, hf.para("10")), hf.cell(0, 2, hf.para("9"))]]
    doc = HwpxDocument.open(hf.build(tmp_path / "p.hwpx", hf.section(hf.table(1, 3, rows))))
    first = doc.recalculate_formulas()
    assert [u.text for u in first.updated] == ["90.0"]
    assert doc.get_cell(0, 0, 0) == "90.0%"  # 뒤에 붙은 % 는 그대로
    assert doc.recalculate_formulas().updated == []


def test_fill_rows가_중간에_실패하면_앞서_쓴_칸도_되돌린다(tmp_path):
    content = hf.para().replace('<hp:run charPrIDRef="10"/>', hf.field("a", "값"))
    rows = [[hf.cell(0, 0), hf.cell(0, 1, content)], [hf.cell(1, 0), hf.cell(1, 1)]]
    doc = HwpxDocument.open(hf.build(tmp_path / "f.hwpx", hf.section(hf.table(2, 2, rows))))
    with pytest.raises(FieldInCell):
        doc.fill_rows(0, 0, [["A", "B"], ["C", "D"]])
    assert doc.get_cell(0, 0, 0) == ""
    assert doc.get_cell(0, 1, 0) == ""


def test_중첩_표가_든_행은_복제를_거부한다(tmp_path):
    inner = hf.table(1, 1, [[hf.cell(0, 0, hf.para("안쪽"))]])
    outer = hf.para("바깥").replace("</hp:run>", f"</hp:run><hp:run>{inner}</hp:run>", 1)
    rows = [[hf.cell(0, 0, outer)]]
    doc = HwpxDocument.open(hf.build(tmp_path / "f.hwpx", hf.section(hf.table(1, 1, rows))))
    with pytest.raises(RowSpanConflict, match="중첩 표"):
        doc.insert_row(0, 0)
    assert len(doc.tables()) == 2


# ------------------------------------------------------------ 제자리 치환 (본문 누름틀)
def _multi_paragraph_field() -> str:
    """여러 문단에 걸친 누름틀 (기안문 content_body 와 같은 모양)."""
    begin = (
        '<hp:run charPrIDRef="10"><hp:ctrl><hp:fieldBegin id="900" type="CLICK_HERE" '
        'name="content_body"/></hp:ctrl><hp:t>일 시 : </hp:t><hp:t>20○○년 3월</hp:t></hp:run>'
    )
    end = (
        '<hp:run charPrIDRef="10"><hp:t>금액 0,000,000원</hp:t>'
        '<hp:ctrl><hp:fieldEnd beginIDRef="900"/></hp:ctrl></hp:run>'
    )
    p1 = f'<hp:p id="1" paraPrIDRef="1" styleIDRef="0">{begin}</hp:p>'
    p2 = f'<hp:p id="2" paraPrIDRef="1" styleIDRef="0">{end}</hp:p>'
    outside = (
        '<hp:p id="3" paraPrIDRef="1" styleIDRef="0">'
        "<hp:run><hp:t>20○○년 밖</hp:t></hp:run></hp:p>"
    )
    return p1 + p2 + outside


def _body_doc(tmp_path) -> HwpxDocument:
    xml = hf.section("").replace("</hs:sec>", _multi_paragraph_field() + "</hs:sec>")
    return HwpxDocument.open(hf.build(tmp_path / "b.hwpx", xml))


def test_자리표시자를_제자리에서_바꾼다(tmp_path):
    doc = _body_doc(tmp_path)
    n = doc.replace_text({"20○○년": "2026년", "0,000,000원": "1,500,000원"})
    assert n == 3  # 본문 2곳 + 본문 밖 1곳
    values = [t.text for t in doc._root("Contents/section0.xml").iter(f"{{{HP}}}t")]
    assert "2026년 3월" in values
    assert "금액 1,500,000원" in values


def test_문단_구조는_그대로다(tmp_path):
    doc = _body_doc(tmp_path)
    ns = {"hp": HP}
    before = len(doc._root("Contents/section0.xml").findall(".//hp:p", ns))
    doc.replace_text({"20○○년": "2026년"}, field="content_body")
    assert len(doc._root("Contents/section0.xml").findall(".//hp:p", ns)) == before
    assert doc.fields()[0].name == "content_body"


def test_누름틀을_지정하면_그_안만_바꾼다(tmp_path):
    doc = _body_doc(tmp_path)
    n = doc.replace_text({"20○○년": "2026년"}, field="content_body")
    assert n == 1  # 본문 밖의 '20○○년 밖' 은 그대로
    texts = [t.text for t in doc._root("Contents/section0.xml").iter(f"{{{HP}}}t")]
    assert "20○○년 밖" in texts


def test_바꿀_문구가_없으면_0이고_문서는_그대로다(tmp_path):
    doc = _body_doc(tmp_path)
    assert doc.replace_text({"없는문구": "x"}) == 0
    assert not doc._dirty


def test_빈_문구로_바꾸면_지운다(tmp_path):
    doc = _body_doc(tmp_path)
    doc.replace_text({"일 시 : ": ""}, field="content_body")
    values = [t.text for t in doc._root("Contents/section0.xml").iter(f"{{{HP}}}t")]
    assert "" in values


def test_치환_결과는_저장하고_다시_열어도_남는다(tmp_path):
    doc = _body_doc(tmp_path)
    doc.replace_text({"20○○년": "2026년"}, field="content_body")
    again = reopen(doc, tmp_path)
    texts = [t.text for t in again._root("Contents/section0.xml").iter(f"{{{HP}}}t")]
    assert "2026년 3월" in texts


def test_치환_값의_제어문자도_지운다(tmp_path):
    doc = _body_doc(tmp_path)
    doc.replace_text({"20○○년": "20\x0b26년"}, field="content_body")
    texts = [t.text for t in doc._root("Contents/section0.xml").iter(f"{{{HP}}}t")]
    assert "2026년 3월" in texts
    _valid_xml(doc, tmp_path)
