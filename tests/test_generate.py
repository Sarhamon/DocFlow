"""문서 생성 테스트 — 스키마와 입력값으로 HWPX 를 만든다.

서식은 tests/hwpx_factory.py 로 만들고 스키마는 추출기로 뽑는다(변형 서식은 스키마를 직접 쓴다).
"""

from __future__ import annotations

import pytest

from docflow.generate import GenerateError, Leftover, assign_variants, generate
from docflow.hwpx.document import Block, HwpxDocument
from docflow.schema.extract import extract
from docflow.schema.model import FormSchema, SlotSpec, TableSpec, Variant
from docflow.validate import FormInput
from tests import hwpx_factory as hf
from tests.test_schema_extract import tbl, 교통비

SECTION = "Contents/section0.xml"


def make(tmp_path, xml: str, stem: str = "서식") -> tuple[FormSchema, object]:
    path = hf.build(tmp_path / f"{stem}.hwpx", xml)
    return extract(HwpxDocument.open(path), stem=stem), path


@pytest.fixture
def traffic(tmp_path):
    """교통비 지급 내역서: 학과 칸(○○○○○과) + 반복영역 3~5행(예시 행 포함) + 6행 수식 합계."""
    return make(tmp_path, hf.section(교통비()), "교통비")


def rows_of(path) -> list[list[str]]:
    return HwpxDocument.open(path).tables()[0].grid()


# ------------------------------------------------------------ 반복영역
def test_입력한_행만_쓰고_남는_행은_비운다(traffic, tmp_path):
    schema, template = traffic
    data = FormInput(
        cells={"t0.r0.c1": "컴퓨터소프트웨어과"},
        rows={"t0.r3-5": [{1: "김철수", 2: "국민"}, {1: "이영희", 2: "신한", 3: "7,000"}]},
    )
    out = generate(schema, data, template, tmp_path / "out.hwpx").path
    grid = rows_of(out)
    assert grid[3] == ["1", "김철수", "국민", "5,000"]  # 연번은 번호, 입금액은 미리 채운 값
    assert grid[4] == ["2", "이영희", "신한", "7,000"]
    assert grid[5] == ["", "", "", ""]  # 쓰지 않은 행의 연번·5,000 도 지운다
    assert grid[6][3] == "12,000"  # 합계 수식을 다시 계산했다


def test_예시_행은_첫_사람_값으로_덮는다(traffic, tmp_path):
    schema, template = traffic
    data = FormInput(cells={"t0.r0.c1": "과"}, rows={"t0.r3-5": [{1: "김철수"}]})
    grid = rows_of(generate(schema, data, template, tmp_path / "o.hwpx").path)
    assert "홍길동" not in [c for row in grid for c in row]


def test_수용_행보다_많으면_행을_늘린다(traffic, tmp_path):
    schema, template = traffic
    people = [{1: f"학생{i}"} for i in range(5)]
    data = FormInput(cells={"t0.r0.c1": "과"}, rows={"t0.r3-5": people})
    grid = rows_of(generate(schema, data, template, tmp_path / "o.hwpx").path)
    assert [row[0] for row in grid[3:8]] == ["1", "2", "3", "4", "5"]
    assert grid[8][3] == "25,000"


def test_입력하지_않은_반복영역은_모두_비운다(traffic, tmp_path):
    schema, template = traffic
    grid = rows_of(generate(schema, FormInput(), template, tmp_path / "o.hwpx").path)
    assert all(c == "" for row in grid[3:6] for c in row)
    assert grid[6][3] == "0"


def test_없는_열에_쓰면_거부한다(traffic, tmp_path):
    schema, template = traffic
    data = FormInput(rows={"t0.r3-5": [{9: "x"}]})
    with pytest.raises(GenerateError, match="없는 열"):
        generate(schema, data, template, tmp_path / "o.hwpx")


# ------------------------------------------------------------ 결재란·누름틀·칸
@pytest.fixture
def draft(tmp_path):
    body = (
        hf.field("apb4_sign", "○○○", fid="1")
        + hf.field("docnumber", "○○처-000000", fid="2")
        + tbl(2, 2, (0, 0, "결재"), (0, 1, "처장"), (1, 0, "00/00"), (1, 1, "○○○"))
    )
    return make(tmp_path, hf.section(body), "기안")


def test_결재란의_템플릿_잔여값을_비운다(draft, tmp_path):
    schema, template = draft
    data = FormInput(fields={"docnumber": "산학협력처-1"})
    out = HwpxDocument.open(generate(schema, data, template, tmp_path / "o.hwpx").path)
    assert {f.name: f.value for f in out.fields()} == {"apb4_sign": "", "docnumber": "산학협력처-1"}
    assert out.tables()[0].grid()[1] == ["", ""]


def test_비운_문서번호는_자리표시자를_남기지_않는다(draft, tmp_path):
    """문서번호는 결재 시스템이 매긴다. 비우고 만들어도 `○○처-000000` 이 남지 않고 경고도 없다."""
    schema, template = draft
    result = generate(schema, FormInput(), template, tmp_path / "o.hwpx")
    out = HwpxDocument.open(result.path)
    assert {f.name: f.value for f in out.fields()}["docnumber"] == ""
    assert [x for x in result.leftovers if "000000" in x.text] == []


def test_결재란_칸에는_쓰지_않는다(draft, tmp_path):
    schema, template = draft
    with pytest.raises(GenerateError, match="approval"):
        generate(schema, FormInput(cells={"t0.r1.c1": "김처장"}), template, tmp_path / "o.hwpx")


def test_서식에_없는_누름틀은_거부한다(draft, tmp_path):
    schema, template = draft
    with pytest.raises(GenerateError, match="없는 누름틀"):
        generate(schema, FormInput(fields={"nope": "x"}), template, tmp_path / "o.hwpx")


def test_여러_줄_값은_본문_누름틀의_문단으로_쓴다(tmp_path):
    schema, template = make(tmp_path, hf.body(*hf.body_field()), "본문")
    data = FormInput(fields={"content_body": "1. 목적\n  가. 첫째\n붙임 1부.  끝."})
    out = HwpxDocument.open(generate(schema, data, template, tmp_path / "o.hwpx").path)
    assert [b.text for b in out.blocks()] == ["1. 목적", "가. 첫째", "붙임 1부.  끝.결재란"]


def test_슬롯이_아닌_칸도_좌표로_쓴다(traffic, tmp_path):
    schema, template = traffic
    data = FormInput(cells={"t0.r0.c0": "학과명"})
    grid = rows_of(generate(schema, data, template, tmp_path / "o.hwpx").path)
    assert grid[0][0] == "학과명"


def test_좌표_형식이_아니면_거부한다(traffic, tmp_path):
    schema, template = traffic
    with pytest.raises(GenerateError, match="좌표"):
        generate(schema, FormInput(cells={"학과": "x"}), template, tmp_path / "o.hwpx")


def test_본문_문구를_바꾼다(tmp_path):
    xml = hf.body(hf.para("상기와 같이 확인함."), hf.para("20○○년 ○○월 ○○일"))
    schema, template = make(tmp_path, xml)
    result = generate(
        schema, FormInput(), template, tmp_path / "o.hwpx",
        replacements={"20○○년 ○○월 ○○일": "2026년 10월 12일"},
    )  # fmt: skip
    assert result.replaced == 1
    assert result.leftovers == []
    assert HwpxDocument.open(result.path).blocks()[1].text == "2026년 10월 12일"


# ------------------------------------------------------------ 남은 자리표시자
def test_쓰지_않은_자리의_자리표시자를_알려준다(tmp_path):
    xml = hf.body(
        hf.para_runs(
            f'<hp:run charPrIDRef="1">{tbl(1, 2, (0, 0, "학과"), (0, 1, "○○○○과"))}</hp:run>'
        ),
        hf.para("사업담당자 : ○ ○ ○  (날인)"),
        hf.para("기간 : 20OO.OO.OO.(월)"),
    )
    schema, template = make(tmp_path, xml)
    result = generate(schema, FormInput(), template, tmp_path / "o.hwpx")
    assert result.leftovers == [
        Leftover("cell", "t0.r0.c1", "○○○○과"),
        Leftover("text", "section0#1", "사업담당자 : ○ ○ ○  (날인)"),
    ]


def test_작성자가_쓴_값은_자리표시자처럼_보여도_알리지_않는다(traffic, tmp_path):
    schema, template = traffic
    data = FormInput(cells={"t0.r0.c1": "2026.10.12 13:00"})  # 00 이 들어 있다
    assert generate(schema, data, template, tmp_path / "o.hwpx").leftovers == []


# ------------------------------------------------------------ 변형
def table_para(text: str) -> str:
    return hf.para_runs(
        f'<hp:run charPrIDRef="1">{hf.table(1, 1, [[hf.cell(0, 0, hf.para(text))]])}</hp:run>'
    )


@pytest.fixture
def variants(tmp_path):
    """한 파일에 든 변형 두 개. 제목 문단 -> 표 문단 -> 빈 문단 순서다."""
    xml = hf.body(
        hf.para("[ 서식 1-01호 ]"), table_para("○○○ 1"), hf.para(""),
        hf.para("[ 서식 1-02호 ]"), table_para("○○○ 2"), hf.para(""),
    )  # fmt: skip
    template = hf.build(tmp_path / "v.hwpx", xml)
    schema = FormSchema(
        title="기안문",
        tables=[TableSpec(index=i, rows=1, cols=1) for i in range(2)],
        variants=[Variant(index=0, label="1-01호", first_table=0, last_table=0),
                  Variant(index=1, label="1-02호", first_table=1, last_table=1)],
        slots=[SlotSpec(table=i, row=0, col=0, placeholder=f"○○○ {i + 1}") for i in range(2)],
    )  # fmt: skip
    return schema, template


def test_고른_변형만_남긴다(variants, tmp_path):
    schema, template = variants
    data = FormInput(cells={"t1.r0.c0": "둘째 변형 값"}, variant=1)
    result = generate(schema, data, template, tmp_path / "o.hwpx")
    out = HwpxDocument.open(result.path)
    assert [b.text for b in out.blocks()] == ["[ 서식 1-02호 ]", "", ""]
    assert [t.cells[0].text for t in out.tables()] == ["둘째 변형 값"]
    assert result.removed_blocks == 3
    assert out._root(SECTION)[0].find(f".//{{{hf.HP}}}secPr") is not None


def test_고른_변형_밖의_자리표시자는_알리지_않는다(variants, tmp_path):
    schema, template = variants
    result = generate(schema, FormInput(variant=0), template, tmp_path / "o.hwpx")
    assert result.leftovers == [Leftover("cell", "t0.r0.c0", "○○○ 1")]


def test_변형이_있으면_골라야_한다(variants, tmp_path):
    schema, template = variants
    with pytest.raises(GenerateError, match="골라야"):
        generate(schema, FormInput(), template, tmp_path / "o.hwpx")
    with pytest.raises(GenerateError, match="없는 변형"):
        generate(schema, FormInput(variant=5), template, tmp_path / "o.hwpx")


def test_떼어_낸_서식은_정해진_변형만_남긴다(variants, tmp_path):
    schema, template = variants
    part = schema.split()[1]
    result = generate(part, FormInput(cells={"t1.r0.c0": "값"}), template, tmp_path / "o.hwpx")
    assert [b.text for b in HwpxDocument.open(result.path).blocks()] == ["[ 서식 1-02호 ]", "", ""]
    with pytest.raises(GenerateError, match="정해져"):
        generate(part, FormInput(variant=0), template, tmp_path / "o.hwpx")


def test_고르지_않은_변형의_칸에는_쓰지_않는다(variants, tmp_path):
    schema, template = variants
    data = FormInput(cells={"t0.r0.c0": "x"}, variant=1)
    with pytest.raises(GenerateError, match="고른 변형"):
        generate(schema, data, template, tmp_path / "o.hwpx")


def test_변형_사이_문단은_빈_문단까지_앞_변형이고_제목부터_뒤_변형이다():
    schema = FormSchema(
        title="t",
        tables=[TableSpec(index=i, rows=1, cols=1) for i in range(3)],
        variants=[Variant(index=0, label="a", first_table=0, last_table=0),
                  Variant(index=1, label="b", first_table=1, last_table=2)],
    )  # fmt: skip
    texts = ["머리", "", "본문", "", "", "제목 b", "", "본문 b", "", "끝"]
    tables = {1: [0], 7: [1], 8: [2]}
    blocks = [Block("s", i, tables.get(i, []), t) for i, t in enumerate(texts)]
    assert assign_variants(blocks, schema) == [0, 0, 0, 0, 0, 1, 1, 1, 1, 1]


def test_변형_이름이_없으면_빈_문단_다음_글부터_뒤_변형이다():
    schema = FormSchema(
        title="t",
        tables=[TableSpec(index=i, rows=1, cols=1) for i in range(2)],
        variants=[Variant(index=0, label="변형 1", first_table=0, last_table=0),
                  Variant(index=1, label="변형 2", first_table=1, last_table=1)],
    )  # fmt: skip
    texts = ["", "끝.", "", "", "다음 제목", "", ""]
    tables = {0: [0], 6: [1]}
    blocks = [Block("s", i, tables.get(i, []), t) for i, t in enumerate(texts)]
    assert assign_variants(blocks, schema) == [0, 0, 0, 0, 1, 1, 1]
