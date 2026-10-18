"""HWPX 본문 테스트 — 여러 문단 누름틀 쓰기, 구역 문단(블록) 읽기·지우기, 노드에 걸친 치환.

기안문(AID 1-01~10)에서 확인한 모양을 tests/hwpx_factory.py 로 재현한다. 본문 누름틀은
`1. 관련` 문단에서 시작해 `붙임 … 끝.` 문단에서 끝나고, 끝 문단에는 누름틀 밖의 글(결재란)이
이어진다.
"""

from __future__ import annotations

import pytest

from docflow.hwpx.document import HP, HwpxDocument, HwpxError
from tests import hwpx_factory as hf

SECTION = "Contents/section0.xml"
NS = {"hp": HP}


def open_body(tmp_path, *paras: str) -> HwpxDocument:
    return HwpxDocument.open(hf.build(tmp_path / "b.hwpx", hf.body(*paras)))


def reopen(doc: HwpxDocument, tmp_path) -> HwpxDocument:
    return HwpxDocument.open(doc.save(tmp_path / "out.hwpx"))


def top_texts(doc: HwpxDocument) -> list[str]:
    return [b.text for b in doc.blocks()]


def para_shapes(doc: HwpxDocument) -> list[str]:
    return [p.get("paraPrIDRef") for p in doc._root(SECTION)]


@pytest.fixture
def body_doc(tmp_path):
    return open_body(tmp_path, hf.para("[ 서식 1-01호 ]"), *hf.body_field(), hf.para("뒤 문단"))


# ------------------------------------------------------------ 여러 문단 누름틀
def test_줄마다_문단_하나로_쓴다(body_doc):
    body_doc.fill_text("content_body", "1. 목적\n  가. 첫째\n  나. 둘째\n붙임 계획서 1부.  끝.")
    assert top_texts(body_doc) == [
        "[ 서식 1-01호 ]",
        "1. 목적",
        "가. 첫째",
        "나. 둘째",
        "붙임 계획서 1부.  끝.결재란",
        "뒤 문단",
    ]


def test_줄이_많으면_사이_문단을_복제한다(body_doc):
    lines = ["시작", *[f"줄{i}" for i in range(6)], "끝"]
    body_doc.fill_text("content_body", "\n".join(lines))
    assert top_texts(body_doc)[1:-1] == [*lines[:-1], "끝결재란"]
    # 늘린 문단은 사이 문단의 문단 모양을 따른다
    assert para_shapes(body_doc) == ["11", "16", *["25"] * 6, "26", "11"]


def test_줄이_적으면_사이_문단을_지운다(body_doc):
    body_doc.fill_text("content_body", "처음\n마지막")
    assert top_texts(body_doc) == ["[ 서식 1-01호 ]", "처음", "마지막결재란", "뒤 문단"]


def test_한_줄이면_끝_문단의_본문만_비우고_누름틀_밖은_둔다(body_doc):
    body_doc.fill_text("content_body", "한 줄")
    assert top_texts(body_doc) == ["[ 서식 1-01호 ]", "한 줄", "결재란", "뒤 문단"]


def test_누름틀_짝과_이름은_그대로다(body_doc, tmp_path):
    body_doc.fill_text("content_body", "가\n나\n다")
    again = reopen(body_doc, tmp_path)
    [field] = again.fields()
    assert field.name == "content_body"
    assert field.value == "가나다"  # 문단을 넘는 값은 이어 붙여 읽힌다
    assert top_texts(again)[1:4] == ["가", "나", "다결재란"]


def test_시작_문단에_글이_없으면_새로_만든다(tmp_path):
    paras = hf.body_field()
    paras[0] = paras[0].replace('<hp:run charPrIDRef="22"><hp:t>1. 관련</hp:t></hp:run>', "")
    doc = open_body(tmp_path, *paras)
    doc.fill_text("content_body", "새 첫 줄\n끝 줄")
    assert top_texts(doc) == ["새 첫 줄", "끝 줄결재란"]


def test_같은_문단_안의_누름틀은_한_줄로_쓴다(tmp_path):
    doc = open_body(tmp_path, hf.para_runs(hf.field("docnumber", "○○처-000")))
    assert doc.fill_text("docnumber", "가\n나") == 1
    assert doc.fields()[0].value == "가 나"


def test_사이_문단에_표가_있으면_거부하고_문서를_바꾸지_않는다(tmp_path):
    paras = hf.body_field()
    paras[2] = hf.para_runs(f'<hp:run charPrIDRef="1">{hf.table(1, 1, [[hf.cell(0, 0)]])}</hp:run>')
    doc = open_body(tmp_path, *paras)
    before = top_texts(doc)
    with pytest.raises(HwpxError, match="표"):
        doc.fill_text("content_body", "가\n나")
    assert top_texts(doc) == before


def test_이름이_다른_누름틀은_그대로다(body_doc):
    assert body_doc.fill_text("없는이름", "가") == 0
    assert not body_doc._dirty


# ------------------------------------------------------------ 블록
def test_블록은_구역_바로_아래_문단이다(tmp_path):
    table = hf.table(1, 1, [[hf.cell(0, 0, hf.para("표 안 글"))]])
    doc = open_body(
        tmp_path,
        hf.para("머리"),
        hf.para_runs(f'<hp:run charPrIDRef="1">{table}<hp:t>표 뒤 글</hp:t></hp:run>'),
        hf.para(""),
    )
    blocks = doc.blocks()
    assert [(b.index, b.tables, b.text) for b in blocks] == [
        (0, [], "머리"),
        (1, [0], "표 뒤 글"),  # 표 안의 글은 빼고 센다
        (2, [], ""),
    ]


def test_첫_문단을_지우면_쪽_설정을_남는_첫_문단으로_옮긴다(tmp_path):
    doc = open_body(tmp_path, hf.para("변형 1"), hf.para("변형 2"), hf.para("변형 2 끝"))
    doc.remove_blocks(doc.blocks()[:1])
    again = reopen(doc, tmp_path)
    assert top_texts(again) == ["변형 2", "변형 2 끝"]
    first = again._root(SECTION)[0]
    assert first.find("hp:run/hp:secPr", NS) is not None
    assert len(again._root(SECTION).findall(".//hp:secPr", NS)) == 1


def test_모두_지우면_쪽_설정만_든_빈_문단을_남긴다(tmp_path):
    doc = open_body(tmp_path, hf.para("가"), hf.para("나"))
    assert doc.remove_blocks(doc.blocks()) == 2
    again = reopen(doc, tmp_path)
    assert top_texts(again) == [""]
    assert again._root(SECTION).find(".//hp:secPr", NS) is not None


def test_지우면_표_번호를_다시_센다(tmp_path):
    def table_para(text: str) -> str:
        table = hf.table(1, 1, [[hf.cell(0, 0, hf.para(text))]])
        return hf.para_runs(f'<hp:run charPrIDRef="1">{table}</hp:run>')

    doc = open_body(tmp_path, table_para("A"), table_para("B"), table_para("C"))
    assert len(doc.tables()) == 3
    doc.remove_blocks([b for b in doc.blocks() if b.tables != [1]])
    assert [t.cells[0].text for t in doc.tables()] == ["B"]
    doc.set_cell(0, 0, 0, "B2")
    assert reopen(doc, tmp_path).get_cell(0, 0, 0) == "B2"


# ------------------------------------------------------------ 노드에 걸친 치환
def split_para() -> str:
    """`사업담당자` / ` ` / `: ○ ○ ○  (날인)` 세 노드로 나뉜 서명 줄 (교통비 지급 내역서)."""
    return hf.para_runs(
        '<hp:run charPrIDRef="9"><hp:t>학과장 / 사업담당자</hp:t></hp:run>'
        '<hp:run charPrIDRef="10"><hp:t> </hp:t></hp:run>'
        '<hp:run charPrIDRef="9"><hp:t>: ○ ○ ○  (날인)</hp:t></hp:run>'
    )


def test_여러_노드에_걸친_문구도_바꾼다(tmp_path):
    doc = open_body(tmp_path, split_para())
    assert doc.replace_text({"사업담당자 : ○ ○ ○": "사업담당자 : 김 연 성"}) == 1
    assert top_texts(doc) == ["학과장 / 사업담당자 : 김 연 성  (날인)"]
    texts = [t.text for t in doc._root(SECTION).iter(f"{{{HP}}}t")]
    assert texts == ["학과장 / 사업담당자 : 김 연 성", "", "  (날인)"]


def test_치환은_문단을_넘지_않는다(tmp_path):
    doc = open_body(tmp_path, hf.para("앞 문단 끝"), hf.para("뒷 문단"))
    assert doc.replace_text({"끝뒷": "x"}) == 0


def test_한_문단에_여러_번_나오면_모두_바꾼다(tmp_path):
    doc = open_body(tmp_path, hf.para("20○○년 3월 ~ 20○○년 12월"))
    assert doc.replace_text({"20○○년": "2026년"}) == 2
    assert top_texts(doc) == ["2026년 3월 ~ 2026년 12월"]
