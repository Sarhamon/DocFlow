# -*- coding: utf-8 -*-
"""금액 합계 검증 규칙 테스트.

실제 서식은 내부 자료라 저장소에 없다. 그래서 스키마를 파일에서 읽지 않고
코드로 만든다. 구조는 `교통비 지급 내역서`(AID 5-05)에서 그대로 가져왔다.
"""
from __future__ import annotations

from docflow.schema.form import FormSchema, SlotSpec
from docflow.validate import cell_id, parse_amount, validate

#: 교통비 지급 내역서의 입금액 열. 데이터는 7~16행, 합계는 17행이다.
DATA_ROWS = range(7, 17)
TOTAL_ROW = 17
COL = 11


def 내역서_스키마(total_rows=(TOTAL_ROW,), data_rows=DATA_ROWS) -> FormSchema:
    slots = [
        SlotSpec(id=cell_id(0, r, COL), table=0, row=r, col=COL, label=str(r))
        for r in data_rows
    ]
    slots += [
        SlotSpec(id=cell_id(0, r, COL), table=0, row=r, col=COL, label="합계")
        for r in total_rows
    ]
    return FormSchema(form_id="5-05", title="교통비 지급 내역서", slots=slots)


def 값(rows: dict[int, str]) -> dict[str, str]:
    return {cell_id(0, r, COL): v for r, v in rows.items()}


# ---------------------------------------------------------------- parse_amount


def test_금액_파싱():
    assert parse_amount("5,000") == 5000
    assert parse_amount("5,000원") == 5000
    assert parse_amount("₩ 1,234,567") == 1234567
    assert parse_amount("0") == 0


def test_금액이_아니면_None():
    assert parse_amount("") is None
    assert parse_amount("합계") is None
    assert parse_amount("해당없음") is None


# ---------------------------------------------------------------- 합계 규칙


def test_합계가_맞으면_통과():
    values = 값({r: "5,000" for r in DATA_ROWS} | {TOTAL_ROW: "50,000"})
    assert validate(내역서_스키마(), values) == []


def test_합계가_틀리면_잡는다():
    values = 값({r: "5,000" for r in DATA_ROWS} | {TOTAL_ROW: "49,000"})
    issues = validate(내역서_스키마(), values)

    assert len(issues) == 1
    assert issues[0].code == "AMOUNT_TOTAL_MISMATCH"
    assert issues[0].where == cell_id(0, TOTAL_ROW, COL)
    assert "50,000" in issues[0].message
    assert "49,000" in issues[0].message


def test_예시_행도_합계에_포함한다():
    """행 7은 예시 행(`홍길동`)이라 반복 영역 밖이지만 데이터 행이다.

    반복 영역(8~16)만 더하면 45,000 이 나와 오탐이 난다.
    """
    values = 값({r: "5,000" for r in DATA_ROWS} | {TOTAL_ROW: "50,000"})
    assert validate(내역서_스키마(), values) == []

    빠뜨린 = 값({r: "5,000" for r in range(8, 17)} | {TOTAL_ROW: "50,000"})
    issues = validate(내역서_스키마(), 빠뜨린)
    assert len(issues) == 1
    assert "45,000" in issues[0].message


def test_일부만_채워도_채운_것끼리_검사한다():
    values = 값({7: "5,000", 8: "3,000", TOTAL_ROW: "8,000"})
    assert validate(내역서_스키마(), values) == []


def test_합계가_비어_있으면_넘어간다():
    """빈 합계는 '필수 누락' 규칙이 볼 몫이라 여기서는 오류가 아니다."""
    values = 값({r: "5,000" for r in DATA_ROWS})
    assert validate(내역서_스키마(), values) == []


def test_데이터가_하나도_없으면_넘어간다():
    assert validate(내역서_스키마(), 값({TOTAL_ROW: "50,000"})) == []


def test_소계와_합계는_구간을_나눠_센다():
    """소계(행 10)가 있으면 그다음 행부터 다시 센다. 소계를 두 번 더하지 않는다."""
    schema = 내역서_스키마(total_rows=(10, TOTAL_ROW), data_rows=range(7, 10))
    schema.slots += [
        SlotSpec(id=cell_id(0, r, COL), table=0, row=r, col=COL, label=str(r))
        for r in range(11, 17)
    ]

    values = 값(
        {7: "1,000", 8: "1,000", 9: "1,000", 10: "3,000"}
        | {r: "2,000" for r in range(11, 17)}
        | {TOTAL_ROW: "12,000"}
    )
    assert validate(schema, values) == []
