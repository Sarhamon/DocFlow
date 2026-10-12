"""서식 스키마 v2 모델 테스트.

구조는 `교통비 지급 내역서`(AID 5-05)와 `일반기안문(샘플)`(앵커 1-01~04호)에서 확인한 것이다.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from docflow.schema.model import (
    SCHEMA_VERSION,
    FieldSpec,
    FormSchema,
    FormulaSpec,
    RepeatColumn,
    RepeatSpec,
    SlotSpec,
    TableSpec,
    Variant,
)


def tables(n: int) -> list[TableSpec]:
    return [TableSpec(index=i, rows=1, cols=1) for i in range(n)]


def 교통비_반복영역() -> RepeatSpec:
    """헤더 5~6행, 데이터 7~16행(7행은 예시 행), 합계 17행."""
    return RepeatSpec(
        table=0,
        header_rows=[5, 6],
        start_row=7,
        end_row=16,
        total_row=17,
        has_example=True,
        columns=[
            RepeatColumn(col=0, label="연번", prefilled="1"),
            RepeatColumn(col=1, label="성명"),
            RepeatColumn(col=7, label="교통비 입금계좌 은행명"),
            RepeatColumn(col=11, label="교통비 입금계좌 입금액(원)", input_type="amount",
                         prefilled="5,000"),
        ],
    )


def 교통비_스키마() -> FormSchema:
    return FormSchema(
        form_id="5-05",
        title="교통비 지급 내역서",
        program="AID",
        category="5. 학생지원비",
        tables=tables(3),
        slots=[
            SlotSpec(table=0, row=1, col=2, label="학과/부서명", placeholder="○○○○○과"),
            SlotSpec(table=0, row=2, col=5, label="일시", input_type="date",
                     placeholder="2024.5.2.(목)"),
        ],
        formulas=[FormulaSpec(table=0, row=17, col=11, formula="=SUM(ABOVE)",
                              result_format="%g,")],
        repeats=[교통비_반복영역()],
    )


# ------------------------------------------------------------------ 반복영역
def test_반복영역은_예시_행을_첫_데이터_행으로_본다():
    rep = 교통비_반복영역()
    assert (rep.start_row, rep.end_row, rep.capacity) == (7, 16, 10)
    assert rep.has_example


def test_합계_행은_영역_밖이다():
    """옛 모델은 합계 행(17)까지 영역(8~17)에 넣었다."""
    rep = 교통비_반복영역()
    assert rep.total_row == 17
    assert rep.total_row > rep.end_row


def test_헤더가_두_줄이어도_열_이름을_담는다():
    cols = {c.col: c for c in 교통비_반복영역().columns}
    assert cols[7].label == "교통비 입금계좌 은행명"
    assert cols[11].prefilled == "5,000"
    assert cols[1].prefilled is None


def test_반복영역_id는_좌표에서_만들어진다():
    assert 교통비_반복영역().id == "t0.r7-16"


def test_끝_행이_시작_행보다_작으면_거부한다():
    with pytest.raises(ValidationError, match="end_row"):
        RepeatSpec(table=0, start_row=5, end_row=4)


def test_합계_행이_데이터_행_위에_있으면_거부한다():
    with pytest.raises(ValidationError, match="합계 행"):
        RepeatSpec(table=0, start_row=5, end_row=9, total_row=9)


def test_헤더_행이_데이터_행_아래에_있으면_거부한다():
    with pytest.raises(ValidationError, match="헤더 행"):
        RepeatSpec(table=0, start_row=5, end_row=9, header_rows=[5])


def test_예시_항목_행은_헤더와_데이터_사이에_있어야_한다():
    RepeatSpec(table=0, header_rows=[0], example_rows=[1, 2], start_row=3, end_row=5)
    with pytest.raises(ValidationError, match="예시 항목 행"):
        RepeatSpec(table=0, header_rows=[0], example_rows=[3], start_row=3, end_row=5)


def test_합계_행이_없는_반복영역도_된다():
    rep = RepeatSpec(table=0, start_row=2, end_row=11)
    assert rep.total_row is None
    assert rep.capacity == 10


# ------------------------------------------------------------------ 칸·수식
def test_슬롯_id는_좌표에서_만들어진다():
    assert SlotSpec(table=1, row=2, col=3).id == "t1.r2.c3"


def test_수식은_입력_슬롯과_따로_둔다():
    s = 교통비_스키마()
    assert [f.id for f in s.formulas] == ["t0.r17.c11"]
    assert all(slot.id != "t0.r17.c11" for slot in s.slots)


def test_작성자가_채우는_칸만_골라낸다():
    s = FormSchema(
        title="x",
        tables=tables(1),
        slots=[
            SlotSpec(table=0, row=0, col=0, label="이름"),
            SlotSpec(table=0, row=1, col=0, label="결재일", role="approval"),
            SlotSpec(table=0, row=2, col=0, label="합계", role="computed"),
        ],
    )
    assert [x.label for x in s.input_slots()] == ["이름"]


def test_누름틀_횟수는_1_이상이다():
    with pytest.raises(ValidationError):
        FieldSpec(name="docnumber", type="CLICK_HERE", occurrences=0)


def test_결재란_누름틀을_표시한다():
    f = FieldSpec(name="apb4_date", type="CLICK_HERE", role="approval", placeholder="00/00")
    assert f.role == "approval"
    assert FieldSpec(name="docnumber", type="CLICK_HERE").role == "input"


# ------------------------------------------------------------------ 변형
def 기안문_스키마() -> FormSchema:
    """1-01~04호: 표 64개가 16개씩 4변형, 누름틀은 변형마다 한 번씩 (36종 x 4)."""
    return FormSchema(
        form_id="1-01~04",
        title="일반기안문(샘플)",
        tables=tables(64),
        variants=[
            Variant(index=i, label=f"{i + 1:02d}호", first_table=i * 16, last_table=i * 16 + 15)
            for i in range(4)
        ],
        fields=[FieldSpec(name="docnumber", type="CLICK_HERE", occurrences=4)],
    )


def test_표가_속한_변형을_찾는다():
    s = 기안문_스키마()
    assert [s.variant_of(t) for t in (0, 15, 16, 47, 48, 63)] == [0, 0, 1, 2, 3, 3]


def test_변형이_없으면_None이다():
    assert 교통비_스키마().variant_of(0) is None


def test_변형_범위가_뒤집히면_거부한다():
    with pytest.raises(ValidationError, match="last_table"):
        Variant(index=0, label="01호", first_table=5, last_table=4)


def test_변형이_없는_표를_가리키면_거부한다():
    with pytest.raises(ValidationError, match="변형"):
        FormSchema(
            title="x",
            tables=tables(2),
            variants=[Variant(index=0, label="01호", first_table=0, last_table=5)],
        )


# ------------------------------------------------------------------ 참조·직렬화
@pytest.mark.parametrize(
    "bad",
    [
        {"slots": [SlotSpec(table=3, row=0, col=0)]},
        {"formulas": [FormulaSpec(table=3, row=0, col=0, formula="=1")]},
        {"repeats": [RepeatSpec(table=3, start_row=0, end_row=1)]},
    ],
)
def test_없는_표를_가리키는_항목은_거부한다(bad):
    with pytest.raises(ValidationError, match="없는 표"):
        FormSchema(title="x", tables=tables(2), **bad)


def test_JSON으로_저장하고_다시_읽어도_같다():
    s = 교통비_스키마()
    again = FormSchema.model_validate_json(s.model_dump_json())
    assert again == s
    assert again.repeats[0].capacity == 10  # 계산되는 값은 다시 계산된다


def test_저장된_JSON에는_id와_수용_행수가_들어있다():
    data = 교통비_스키마().model_dump()
    assert data["repeats"][0]["id"] == "t0.r7-16"
    assert data["repeats"][0]["capacity"] == 10
    assert data["slots"][0]["id"] == "t0.r1.c2"


def test_스키마_버전이_기록된다():
    assert 교통비_스키마().schema_version == SCHEMA_VERSION == 2
