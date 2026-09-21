# -*- coding: utf-8 -*-
"""규칙 기반 검증 테스트.

실제 서식은 내부 자료라 저장소에 없다. 그래서 스키마를 파일에서 읽지 않고
코드로 만든다. 구조는 `교통비 지급 내역서`(AID 5-05)에서 그대로 가져왔다.
"""
from __future__ import annotations

from datetime import date

from docflow.schema.form import FieldSpec, FormSchema, RepeatSpec, SlotSpec
from docflow.validate import (
    cell_id,
    check_amount_totals,
    check_dates,
    check_required,
    fillable_fields,
    find_dates,
    parse_amount,
    validate,
)

#: 교통비 지급 내역서의 입금액 열. 데이터는 7~16행, 합계는 17행이다.
DATA_ROWS = range(7, 17)
TOTAL_ROW = 17
COL = 11


def 슬롯(row: int, label: str = "", kind: str = "placeholder", ph: str = "5,000") -> SlotSpec:
    return SlotSpec(
        id=cell_id(0, row, COL),
        table=0,
        row=row,
        col=COL,
        label=label or str(row),
        kind=kind,
        placeholder=ph,
    )


def 내역서_스키마(total_rows=(TOTAL_ROW,), data_rows=DATA_ROWS) -> FormSchema:
    slots = [슬롯(r) for r in data_rows]
    slots += [슬롯(r, label="합계", ph="50,000") for r in total_rows]
    return FormSchema(form_id="5-05", title="교통비 지급 내역서", slots=slots)


def 값(rows: dict[int, str]) -> dict[str, str]:
    return {cell_id(0, r, COL): v for r, v in rows.items()}


def 다_채운_값() -> dict[str, str]:
    return 값({r: "5,000" for r in DATA_ROWS} | {TOTAL_ROW: "50,000"})


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


# ---------------------------------------------------------------- 금액 합계


def test_합계가_맞으면_통과():
    assert check_amount_totals(내역서_스키마(), 다_채운_값()) == []


def test_합계가_틀리면_잡는다():
    values = 값({r: "5,000" for r in DATA_ROWS} | {TOTAL_ROW: "49,000"})
    issues = check_amount_totals(내역서_스키마(), values)

    assert len(issues) == 1
    assert issues[0].code == "AMOUNT_TOTAL_MISMATCH"
    assert issues[0].where == cell_id(0, TOTAL_ROW, COL)
    assert "50,000" in issues[0].message
    assert "49,000" in issues[0].message


def test_예시_행도_합계에_포함한다():
    """행 7은 예시 행(`홍길동`)이라 반복 영역 밖이지만 데이터 행이다.

    반복 영역(8~16)만 더하면 45,000 이 나와 오탐이 난다.
    """
    빠뜨린 = 값({r: "5,000" for r in range(8, 17)} | {TOTAL_ROW: "50,000"})
    issues = check_amount_totals(내역서_스키마(), 빠뜨린)

    assert len(issues) == 1
    assert "45,000" in issues[0].message


def test_일부만_채워도_채운_것끼리_검사한다():
    values = 값({7: "5,000", 8: "3,000", TOTAL_ROW: "8,000"})
    assert check_amount_totals(내역서_스키마(), values) == []


def test_합계가_비어_있으면_넘어간다():
    """빈 합계는 '필수 누락' 규칙이 볼 몫이라 합계 규칙에서는 오류가 아니다."""
    values = 값({r: "5,000" for r in DATA_ROWS})
    assert check_amount_totals(내역서_스키마(), values) == []


def test_데이터가_하나도_없으면_넘어간다():
    assert check_amount_totals(내역서_스키마(), 값({TOTAL_ROW: "50,000"})) == []


def test_소계와_합계는_구간을_나눠_센다():
    """소계(행 10)가 있으면 그다음 행부터 다시 센다. 소계를 두 번 더하지 않는다."""
    schema = 내역서_스키마(total_rows=(10, TOTAL_ROW), data_rows=range(7, 10))
    schema.slots += [슬롯(r) for r in range(11, 17)]

    values = 값(
        {7: "1,000", 8: "1,000", 9: "1,000", 10: "3,000"}
        | {r: "2,000" for r in range(11, 17)}
        | {TOTAL_ROW: "12,000"}
    )
    assert check_amount_totals(schema, values) == []


# ---------------------------------------------------------------- 필수 누락


def test_자리표시자_칸이_비면_잡는다():
    values = 값({r: "5,000" for r in DATA_ROWS})  # 합계만 비었다
    issues = check_required(내역서_스키마(), values)

    assert len(issues) == 1
    assert issues[0].code == "REQUIRED_EMPTY"
    assert issues[0].where == cell_id(0, TOTAL_ROW, COL)
    assert "합계" in issues[0].message


def test_다_채우면_통과():
    assert check_required(내역서_스키마(), 다_채운_값()) == []


def test_공백만_있으면_비어_있는_것으로_본다():
    values = 다_채운_값()
    values[cell_id(0, TOTAL_ROW, COL)] = "   "
    assert len(check_required(내역서_스키마(), values)) == 1


def test_항목명만_보고_잡은_빈_칸은_필수가_아니다():
    """`kind="empty"` 는 인접 셀 추론이라 안 채우는 게 정상인 칸이 섞인다."""
    schema = FormSchema(
        form_id="5-05",
        title="교통비 지급 내역서",
        slots=[슬롯(3, label="입금통장 사본", kind="empty", ph="")],
    )
    assert check_required(schema, {}) == []


def test_반복_영역_안의_칸은_필수가_아니다():
    """명단은 수용 행을 다 채우지 않는 게 정상이다."""
    schema = 내역서_스키마()
    schema.repeats = [
        RepeatSpec(id="t0.r8-16", table=0, start_row=8, end_row=16, header=["입금액"])
    ]
    issues = check_required(schema, 값({7: "5,000", TOTAL_ROW: "5,000"}))

    assert issues == []


def test_메시지에_서식_예시를_담는다():
    issues = check_required(내역서_스키마(), {})
    행7 = [i for i in issues if i.where == cell_id(0, 7, COL)][0]
    assert "5,000" in 행7.message


def test_메시지를_한_줄로_편다():
    """항목명과 자리표시자에는 줄바꿈이 섞여 있다 (`[세부과제명]\\nActivity명`)."""
    schema = FormSchema(
        form_id="5-05",
        title="교통비 지급 내역서",
        slots=[슬롯(0, label="[세부과제명]\nActivity명", ph="[20○○ AID]\n○○○과")],
    )
    message = check_required(schema, {})[0].message

    assert "\n" not in message
    assert "[세부과제명] Activity명" in message


# ---------------------------------------------------------------- 날짜

#: 날짜 칸 하나짜리 스키마. 자리표시자가 날짜 칸인지 가르는 기준이 된다.
def 날짜_스키마(ph: str = "2000.00.00", label: str = "일시") -> FormSchema:
    return FormSchema(
        form_id="5-05",
        title="교통비 지급 내역서",
        slots=[슬롯(2, label=label, ph=ph)],
    )


def 날짜_값(v: str) -> dict[str, str]:
    return 값({2: v})


def test_날짜_뽑기():
    assert find_dates("2026.03.15") == [date(2026, 3, 15)]
    assert find_dates("2026-03-15") == [date(2026, 3, 15)]
    assert find_dates("2026년 3월 15일") == [date(2026, 3, 15)]
    assert find_dates("2026.03.15 ~ 2026.03.20") == [date(2026, 3, 15), date(2026, 3, 20)]


def test_달력에_없는_날은_None():
    assert find_dates("2026.02.30") == [None]
    assert find_dates("날짜 없음") == []


def test_시각의_물결은_기간이_아니다():
    """`13:00~17:00` 의 물결은 시각을 나눈다. 날짜는 하나뿐이다."""
    assert find_dates("2026.5.2.(목) 13:00~17:00") == [date(2026, 5, 2)]
    assert check_dates(날짜_스키마("2024.5.2.(목) 13:00~17:00"), 날짜_값("2026.5.2.(목) 13:00~17:00")) == []


def test_날짜가_맞으면_통과():
    assert check_dates(날짜_스키마(), 날짜_값("2026.03.15")) == []


def test_없는_날짜를_잡는다():
    issues = check_dates(날짜_스키마(), 날짜_값("2026.02.30"))

    assert len(issues) == 1
    assert issues[0].code == "DATE_INVALID"
    assert issues[0].where == cell_id(0, 2, COL)


def test_날짜로_못_읽으면_잡는다():
    issues = check_dates(날짜_스키마(), 날짜_값("다음 주 화요일"))

    assert len(issues) == 1
    assert issues[0].code == "DATE_INVALID"
    assert "다음 주 화요일" in issues[0].message


def test_기간이_거꾸로면_잡는다():
    issues = check_dates(날짜_스키마("0000.00.00 ~ 0000.00.00", "사업기간"), 날짜_값("2026.03.20 ~ 2026.03.15"))

    assert len(issues) == 1
    assert issues[0].code == "DATE_ORDER"
    assert "2026.03.20" in issues[0].message


def test_같은_날짜_기간은_통과():
    schema = 날짜_스키마("0000.00.00 ~ 0000.00.00", "사업기간")
    assert check_dates(schema, 날짜_값("2026.03.15 ~ 2026.03.15")) == []


def test_날짜_칸이_아니면_안_본다():
    """`○○○○처-000000 2000.00.00` 은 문서번호가 붙어 있어 날짜 칸이 아니다."""
    schema = 날짜_스키마("○○○○처-000000 2000.00.00", "시행")
    assert check_dates(schema, 날짜_값("아무 값")) == []


def test_빈_날짜_칸은_넘어간다():
    """빈 칸은 '필수 누락' 규칙이 볼 몫이다."""
    assert check_dates(날짜_스키마(), 날짜_값("  ")) == []


# ---------------------------------------------------------------- 누름틀

#: 기안문 누름틀. 이름과 자리표시자는 실제 서식(AID 1-01~10)에서 가져왔다.
기안문_누름틀 = [
    FieldSpec(name="docnumber", type="CLICK_HERE", placeholder="○○○○처-000000"),
    FieldSpec(name="enforcedate", type="CLICK_HERE", placeholder="2000.00.00"),
    FieldSpec(name="telephone", type="CLICK_HERE", placeholder="031-441-0000"),
    FieldSpec(name="address", type="CLICK_HERE", placeholder="경기도 안양시 만안구 양화로 37번길 34"),
    FieldSpec(name="publication", type="CLICK_HERE", placeholder="공개"),
    FieldSpec(name="apb4_date", type="CLICK_HERE", placeholder="00/00"),
    FieldSpec(name="apb4_sign", type="CLICK_HERE", placeholder="○○○"),
    FieldSpec(name="", type="FORMULA", placeholder="3,080,000"),
]


def 기안문_스키마() -> FormSchema:
    return FormSchema(form_id="1-01~10", title="일반기안문", fields=list(기안문_누름틀))


def test_수식과_결재란은_채울_누름틀이_아니다():
    """이름 없는 FORMULA 는 한글이 계산하고, apbN_* 는 결재선이 채운다."""
    names = [name for name, _ in fillable_fields(기안문_스키마())]

    assert "docnumber" in names
    assert "" not in names
    assert not [n for n in names if n.startswith("apb")]


def test_누름틀이_비면_잡는다():
    issues = check_required(기안문_스키마(), {}, {})
    걸린 = {i.where for i in issues}

    assert "docnumber" in 걸린
    assert "enforcedate" in 걸린
    assert "telephone" in 걸린


def test_이미_값이_박힌_누름틀은_필수가_아니다():
    """주소·공개여부는 자리표시자가 아니라 실제 값이라 채울 곳이 아니다."""
    걸린 = {i.where for i in check_required(기안문_스키마(), {}, {})}

    assert "address" not in 걸린
    assert "publication" not in 걸린


def test_결재란은_비어도_안_잡는다():
    걸린 = {i.where for i in check_required(기안문_스키마(), {}, {})}

    assert "apb4_date" not in 걸린
    assert "apb4_sign" not in 걸린


def test_누름틀을_채우면_통과():
    fields = {"docnumber": "교무처-001234", "enforcedate": "2026.03.15", "telephone": "031-441-1234"}
    assert check_required(기안문_스키마(), {}, fields) == []


def test_누름틀도_날짜를_본다():
    """`enforcedate` 는 자리표시자가 `2000.00.00` 이라 날짜 칸이다."""
    issues = check_dates(기안문_스키마(), {}, {"enforcedate": "2026.02.30"})

    assert len(issues) == 1
    assert issues[0].code == "DATE_INVALID"
    assert issues[0].where == "enforcedate"


def test_누름틀_없이도_동작한다():
    """표만 있는 서식이 절반 가까이다. fields 를 안 넘겨도 돌아야 한다."""
    assert validate(내역서_스키마(), 다_채운_값()) == []


# ---------------------------------------------------------------- 통합


def test_validate_는_두_규칙을_함께_돌린다():
    values = 값({r: "5,000" for r in range(7, 16)} | {TOTAL_ROW: "50,000"})  # 16행 누락
    codes = {i.code for i in validate(내역서_스키마(), values)}

    assert codes == {"REQUIRED_EMPTY", "AMOUNT_TOTAL_MISMATCH"}


def test_다_맞으면_아무것도_안_나온다():
    assert validate(내역서_스키마(), 다_채운_값()) == []
