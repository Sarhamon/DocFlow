"""서식 예시 그대로 남은 값, 연락처·이메일 형식 경고 (피드백 2026-11-03: 이상한 값도 잡는가)."""

from __future__ import annotations

import pytest

from docflow.schema.model import (
    FieldSpec,
    FormSchema,
    RepeatColumn,
    RepeatSpec,
    SlotSpec,
    TableSpec,
)
from docflow.validate import (
    CONTACT_INVALID,
    EXAMPLE_LEFT,
    FormInput,
    applicable_rules,
    validate,
)


def schema() -> FormSchema:
    return FormSchema(
        title="기안문",
        fields=[FieldSpec(name="telephone", type="CLICK_HERE", placeholder="031-441-0000",
                          input_type="text"),
                FieldSpec(name="email", type="CLICK_HERE", placeholder="000000@yeonsung.ac.kr",
                          input_type="text")],
        tables=[TableSpec(index=0, rows=6, cols=3)],
        slots=[SlotSpec(table=0, row=0, col=1, label="예산", placeholder="0,000,000원",
                        input_type="amount"),
               SlotSpec(table=0, row=1, col=1, label="참여 인원", placeholder="00명",
                        input_type="text"),
               SlotSpec(table=0, row=2, col=1, label="담당자 이메일", input_type="text",
                        required=False)],
        repeats=[RepeatSpec(table=0, header_rows=[3], start_row=4, end_row=5, columns=[
            RepeatColumn(col=0, label="성명"), RepeatColumn(col=1, label="연락처(H.P)")])],
    )  # fmt: skip


def filled(**over: str) -> FormInput:
    fields = {"telephone": "031-441-1234", "email": "kim@yeonsung.ac.kr"}
    cells = {"t0.r0.c1": "1,500,000원", "t0.r1.c1": "25명"}
    for key, value in over.items():
        (fields if key in fields else cells)[key] = value
    return FormInput(fields=fields, cells=cells)


def codes(data: FormInput) -> list[tuple[str, str, str]]:
    return [(i.code, i.severity, i.where) for i in validate(schema(), data)]


def test_올바르게_채우면_아무것도_나오지_않는다():
    assert codes(filled()) == []


# ------------------------------------------------------------ 서식 예시 그대로
@pytest.mark.parametrize(
    ("where", "value"),
    [("t0.r0.c1", "0,000,000원"), ("t0.r0.c1", "0,000,000"), ("t0.r1.c1", "00 명"),
     ("telephone", "031-441-0000"), ("email", "000000@yeonsung.ac.kr")],
)  # fmt: skip
def test_서식_예시를_그대로_두면_경고한다(where, value):
    assert codes(filled(**{where: value})) == [(EXAMPLE_LEFT, "warning", where)]


def test_그럴듯한_예시는_같은_값을_적어도_보지_않는다():
    """정산서 예시 `1,000,000` 은 실제로 100만 원을 적은 것일 수 있다."""
    s = schema()
    s.slots[0].placeholder = "1,000,000"
    assert validate(s, filled(**{"t0.r0.c1": "1,000,000"})) == []


def test_예시가_실제_값인_누름틀은_같아도_보지_않는다():
    """주소·누리집처럼 서식에 학교 값이 박힌 누름틀은 그대로 두는 게 정상이다."""
    s = schema()
    s.fields.append(FieldSpec(name="address", type="CLICK_HERE", placeholder="경기도 안양시"))
    assert validate(s, FormInput(fields={**filled().fields, "address": "경기도 안양시"},
                                 cells=filled().cells)) == []  # fmt: skip


# ------------------------------------------------------------ 연락처·이메일
@pytest.mark.parametrize(
    "value",
    ["031-441-1234", "010 1234 5678", "01012345678", "1588-0000", "(031) 441-1234",
     "+82-10-1234-5678", "031-441-1234 (내선 123)", "031-441-1234, 010-1234-5678"],
)  # fmt: skip
def test_올바른_전화번호는_통과한다(value):
    assert codes(filled(telephone=value)) == []


@pytest.mark.parametrize("value", ["모름", "031-441", "공일공-1234-5678", "0314411234567890"])
def test_전화번호_모양이_아니면_경고한다(value):
    assert codes(filled(telephone=value)) == [(CONTACT_INVALID, "warning", "telephone")]


@pytest.mark.parametrize("value", ["kim@yeonsung.ac.kr", "a.b+c@mail.co", "a@x.kr, b@y.kr"])
def test_올바른_이메일은_통과한다(value):
    assert codes(filled(email=value)) == []


@pytest.mark.parametrize(
    "value", ["kim", "kim@", "kim@yeonsung", "김 @ 연성.kr", "@yeonsung.ac.kr"]
)
def test_이메일_모양이_아니면_경고한다(value):
    assert codes(filled(email=value)) == [(CONTACT_INVALID, "warning", "email")]


def test_항목명으로_표_칸과_명단_열도_본다():
    data = filled(**{"t0.r2.c1": "담당자에게 문의"})
    data.rows["t0.r4-5"] = [{0: "김철수", 1: "010-1234-5678"}, {0: "이영희", 1: "없음"}]
    got = [(i.code, i.location.kind) for i in validate(schema(), data)]
    assert got == [(CONTACT_INVALID, "cell"), (CONTACT_INVALID, "repeat")]


def test_적용_서식_조회에_나온다():
    assert {EXAMPLE_LEFT, CONTACT_INVALID} <= applicable_rules(schema())
