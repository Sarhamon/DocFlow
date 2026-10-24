"""계좌번호·주민등록번호 형식, 예금주·성명 불일치 경고 (IN_15 확장, 명세 F3)."""

from __future__ import annotations

import pytest

from docflow.schema.model import FormSchema, RepeatColumn, RepeatSpec, SlotSpec, TableSpec
from docflow.validate import (
    ACCOUNT_INVALID,
    HOLDER_MISMATCH,
    RRN_INVALID,
    FormInput,
    RuleConfig,
    ValidationConfig,
    applicable_rules,
    validate,
)


def schema() -> FormSchema:
    return FormSchema(
        title="인건비 지급",
        tables=[TableSpec(index=0, rows=8, cols=4)],
        slots=[SlotSpec(table=0, row=0, col=1, label="계좌번호", required=False),
               SlotSpec(table=0, row=1, col=1, label="주민등록번호", required=False)],
        repeats=[RepeatSpec(table=0, header_rows=[2], start_row=3, end_row=7, columns=[
            RepeatColumn(col=0, label="성명"), RepeatColumn(col=1, label="예금주"),
            RepeatColumn(col=2, label="교통비 입금계좌 계좌번호"),
            RepeatColumn(col=3, label="입금액(원)", input_type="amount")])],
    )  # fmt: skip


def codes(data: FormInput, config=None) -> list[tuple[str, str]]:
    issues = validate(schema(), data, config or ValidationConfig())
    return [(i.code, i.severity) for i in issues]


@pytest.mark.parametrize(
    "value", ["123-45-678901", "110 123 456789", "1002123456789", "3333-01-1234567"]
)
def test_올바른_계좌번호는_통과한다(value):
    assert codes(FormInput(cells={"t0.r0.c1": value})) == []


@pytest.mark.parametrize("value", ["123-45", "국민 123-45-678901", "12345678901234567", "모름"])
def test_계좌번호_모양이_아니면_경고한다(value):
    assert codes(FormInput(cells={"t0.r0.c1": value})) == [(ACCOUNT_INVALID, "warning")]


@pytest.mark.parametrize(
    "value", ["900101-1234567", "9001011234567", "040506 - 3123456", "000229-4123456",
              "900101-1******"],
)  # fmt: skip
def test_올바른_주민등록번호는_통과한다(value):
    assert codes(FormInput(cells={"t0.r1.c1": value})) == []


@pytest.mark.parametrize(
    "value", ["901301-1234567", "900230-1234567", "900101-9234567", "900101-123456", "90-01-01"]
)
def test_주민등록번호_모양이_아니면_경고한다(value):
    assert codes(FormInput(cells={"t0.r1.c1": value})) == [(RRN_INVALID, "warning")]


def test_외국인등록번호·국적도_받는_칸은_보지_않는다():
    s = schema()
    s.slots[1].label = "주민등록번호 (외국인의 경우 외국인등록번호/국적)"
    assert validate(s, FormInput(cells={"t0.r1.c1": "베트남"})) == []
    assert RRN_INVALID not in applicable_rules(s)


def test_명단의_계좌번호도_본다():
    rows = {"t0.r3-7": [{0: "김철수", 2: "123-45-678901"}, {0: "이영희", 2: "123"}]}
    issues = validate(schema(), FormInput(rows=rows))
    assert [(i.code, i.location.index) for i in issues] == [(ACCOUNT_INVALID, 1)]


def test_예금주가_성명과_다르면_경고한다():
    rows = {"t0.r3-7": [{0: "김철수", 1: "김 철수"}, {0: "이영희", 1: "이민수"}, {0: "박민수"}]}
    issues = validate(schema(), FormInput(rows=rows))
    assert [(i.code, i.location.index, i.location.col) for i in issues] == [(HOLDER_MISMATCH, 1, 1)]
    assert "이민수" in issues[0].message and "이영희" in issues[0].message


def test_성명_예금주가_한_열이면_보지_않는다():
    s = schema()
    s.repeats[0].columns[0].label = "성명 (예금주)"
    s.repeats[0].columns[1].label = "학번"
    assert HOLDER_MISMATCH not in applicable_rules(s)


def test_적용_규칙과_끄기():
    assert {ACCOUNT_INVALID, RRN_INVALID, HOLDER_MISMATCH} <= applicable_rules(schema())
    off = ValidationConfig(
        default=RuleConfig(disabled=frozenset({ACCOUNT_INVALID, HOLDER_MISMATCH}))
    )
    data = FormInput(cells={"t0.r0.c1": "123"}, rows={"t0.r3-7": [{0: "가", 1: "나"}]})
    assert codes(data, off) == []
