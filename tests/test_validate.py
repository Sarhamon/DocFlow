"""규칙 기반 검증 테스트 (스키마 v2).

실제 서식은 내부 자료라 저장소에 없다. 그래서 스키마를 코드로 만든다. 구조는
`교통비 지급 내역서`(AID 5-05), `정산서`(앵커 3-01), `일반기안문`(AID 1-01~10)에서 가져왔다.
옛 엔진의 오탐 사례 테스트는 이름을 유지해 그대로 옮겼다.
"""

from __future__ import annotations

from datetime import date

import pytest

from docflow.schema.model import (
    FieldSpec,
    FormSchema,
    RepeatColumn,
    RepeatSpec,
    SlotSpec,
    TableSpec,
    Variant,
)
from docflow.validate import (
    AMOUNT_INVALID,
    AMOUNT_TOTAL_MISMATCH,
    DATE_INVALID,
    DATE_ORDER,
    FILL_LEFT,
    REQUIRED_EMPTY,
    ROW_DUPLICATE,
    Exclusions,
    FormInput,
    RuleConfig,
    ValidationConfig,
    applicable_rules,
    check_amount_totals,
    check_cell,
    check_field,
    check_repeat_cell,
    find_dates,
    parse_amount,
    validate,
)


def tables(n: int = 1) -> list[TableSpec]:
    return [TableSpec(index=i, rows=20, cols=12) for i in range(n)]


# ------------------------------------------------------------------ 서식
def 교통비() -> FormSchema:
    """헤더 5~6행, 데이터 7~16행(7행 예시), 합계 17행. 합계 칸은 작성자가 적는다."""
    return FormSchema(
        title="교통비 지급 내역서",
        template="t/교통비.hwpx",
        tables=tables(),
        slots=[
            SlotSpec(table=0, row=1, col=2, label="학과/부서명", placeholder="○○○○○과"),
            SlotSpec(table=0, row=2, col=5, label="일시", input_type="date",
                     placeholder="2024.5.2.(목) 13:00~17:00"),
            SlotSpec(table=0, row=3, col=2, label="첨부", origin="empty", required=False),
            SlotSpec(table=0, row=17, col=11, label="합계", input_type="amount",
                     placeholder="50,000"),
            SlotSpec(table=0, row=0, col=9, label="담당", role="approval", required=False,
                     placeholder="00/00"),
        ],
        repeats=[
            RepeatSpec(
                table=0, header_rows=[5, 6], start_row=7, end_row=16, total_row=17,
                has_example=True,
                columns=[
                    RepeatColumn(col=0, label="연번", input_type="number", prefilled="1"),
                    RepeatColumn(col=1, label="성명"),
                    RepeatColumn(col=3, label="학번"),
                    RepeatColumn(col=4, label="활동일", input_type="date"),
                    RepeatColumn(col=11, label="입금액(원)", input_type="amount",
                                 prefilled="5,000"),
                ],
            )
        ],
    )  # fmt: skip


def 명단(*amounts: str) -> list[dict[int, str]]:
    return [{0: str(i + 1), 1: f"학생{i}", 11: a} for i, a in enumerate(amounts)]


def 교통비_입력(total: str = "15,000", amounts=("5,000", "5,000", "5,000")) -> FormInput:
    return FormInput(
        cells={"t0.r1.c2": "전자공학과", "t0.r2.c5": "2026.10.12(월) 13:00~17:00",
               "t0.r17.c11": total},
        rows={"t0.r7-16": 명단(*amounts)},
    )  # fmt: skip


def 정산서() -> FormSchema:
    """항목이 행마다 고정된 표. 금액 열(2)에 소계(4행)와 합계(8행)가 있다."""
    item = [SlotSpec(table=0, row=r, col=2, label=f"항목{r}", origin="empty", required=False)
            for r in (1, 2, 3, 5, 6, 7)]  # fmt: skip
    totals = [SlotSpec(table=0, row=r, col=2, label=lab, placeholder="0,000,000")
              for r, lab in ((4, "소계"), (8, "합 계"))]  # fmt: skip
    return FormSchema(title="정산서", tables=tables(), slots=item + totals)


def 기안문() -> FormSchema:
    """누름틀 이름·자리표시자는 실제 서식(AID 1-01~10)에서 가져왔다."""
    return FormSchema(
        title="일반기안문",
        tables=tables(),
        fields=[
            FieldSpec(name="docnumber", type="CLICK_HERE", placeholder="○○○○처-000000"),
            FieldSpec(name="enforcedate", type="CLICK_HERE", input_type="date",
                      placeholder="2000.00.00"),
            FieldSpec(name="address", type="CLICK_HERE",
                      placeholder="경기도 안양시 만안구 양화로 37번길 34"),
            FieldSpec(name="publication", type="CLICK_HERE", placeholder="공개"),
            FieldSpec(name="apb4_date", type="CLICK_HERE", role="approval",
                      input_type="date", placeholder="00/00"),
        ],
    )  # fmt: skip


def codes(issues) -> list[tuple[str, str]]:
    return [(i.code, i.where) for i in issues]


# ------------------------------------------------------------------ 값 읽기
def test_금액_파싱():
    assert parse_amount("5,000") == 5000
    assert parse_amount("5,000원") == 5000
    assert parse_amount("₩ 1,234,567") == 1234567


@pytest.mark.parametrize("text", ["", "해당없음", "원"])
def test_금액이_아니면_None(text):
    assert parse_amount(text) is None


def test_날짜_뽑기():
    assert find_dates("2026.10.12 ~ 2026-10-14") == [date(2026, 10, 12), date(2026, 10, 14)]
    assert find_dates("2026년 3월 1일") == [date(2026, 3, 1)]


def test_달력에_없는_날은_None():
    assert find_dates("2026.02.30") == [None]


# ------------------------------------------------------------------ 문서 전체
def test_다_맞으면_아무것도_안_나온다():
    assert validate(교통비(), 교통비_입력()) == []


def test_validate_는_세_규칙을_함께_돌린다():
    data = 교통비_입력(total="99,000")
    data.cells["t0.r1.c2"] = ""
    data.cells["t0.r2.c5"] = "2026.02.30"
    assert sorted(i.code for i in validate(교통비(), data)) == [
        AMOUNT_TOTAL_MISMATCH, DATE_INVALID, REQUIRED_EMPTY,
    ]  # fmt: skip


# ------------------------------------------------------------------ 필수 누락
def test_자리표시자_칸이_비면_잡는다():
    data = 교통비_입력()
    del data.cells["t0.r1.c2"]
    [issue] = validate(교통비(), data)
    assert (issue.code, issue.severity, issue.where) == (REQUIRED_EMPTY, "error", "t0.r1.c2")
    assert issue.location.label == "학과/부서명"
    assert (issue.location.table, issue.location.row, issue.location.col) == (0, 1, 2)
    assert issue.action


def test_공백만_있으면_비어_있는_것으로_본다():
    data = 교통비_입력()
    data.cells["t0.r1.c2"] = "  \n "
    assert codes(validate(교통비(), data)) == [(REQUIRED_EMPTY, "t0.r1.c2")]


def test_메시지에_서식_예시를_담는다():
    data = 교통비_입력()
    del data.cells["t0.r1.c2"]
    assert "○○○○○과" in validate(교통비(), data)[0].message


def test_항목명만_보고_잡은_빈_칸은_필수가_아니다():
    """첨부 확인란처럼 안 채우는 게 정상인 칸이 섞인다 (오탐 사례)."""
    assert "t0.r3.c2" not in [i.where for i in validate(교통비(), 교통비_입력())]


def test_반복_영역_안의_칸은_필수가_아니다():
    """명단은 수용 행을 다 채우지 않는 게 정상이다 (오탐 사례)."""
    data = 교통비_입력()
    data.rows["t0.r7-16"] = [{0: "1", 11: "5,000"}]  # 성명을 비웠고 9행은 아예 없다
    data.cells["t0.r17.c11"] = "5,000"
    assert validate(교통비(), data) == []


def test_결재란은_비어도_안_잡는다():
    assert "t0.r0.c9" not in [i.where for i in validate(교통비(), FormInput())]


# ------------------------------------------------------------------ 누름틀
def 기안문_입력(**over: str) -> FormInput:
    return FormInput(fields={"docnumber": "교육처-123", "enforcedate": "2026.10.12", **over})


def test_누름틀을_채우면_통과():
    assert validate(기안문(), 기안문_입력()) == []


def test_누름틀이_비면_잡는다():
    assert codes(validate(기안문(), 기안문_입력(docnumber=""))) == [
        (REQUIRED_EMPTY, "docnumber")
    ]


def test_이미_값이_박힌_누름틀은_필수가_아니다():
    """주소·공개여부는 서식에 이미 실제 값이 들어 있다 (오탐 사례)."""
    wheres = [i.where for i in validate(기안문(), FormInput())]
    assert "address" not in wheres and "publication" not in wheres


def test_결재선_누름틀은_비어도_날짜가_틀려도_안_잡는다():
    data = 기안문_입력(apb4_date="13/45")
    assert validate(기안문(), data) == []


def test_누름틀도_날짜를_본다():
    assert codes(validate(기안문(), 기안문_입력(enforcedate="2026.13.01"))) == [
        (DATE_INVALID, "enforcedate")
    ]


# ------------------------------------------------------------------ 날짜
def test_없는_날짜를_잡는다():
    data = 교통비_입력()
    data.cells["t0.r2.c5"] = "2026.02.30"
    [issue] = validate(교통비(), data)
    assert issue.code == DATE_INVALID and "2026.02.30" in issue.message


def test_날짜로_못_읽으면_잡는다():
    data = 교통비_입력()
    data.cells["t0.r2.c5"] = "다음 주 월요일"
    assert codes(validate(교통비(), data)) == [(DATE_INVALID, "t0.r2.c5")]


def test_기간이_거꾸로면_잡는다():
    data = 교통비_입력()
    data.cells["t0.r2.c5"] = "2026.10.14 ~ 2026.10.12"
    assert codes(validate(교통비(), data)) == [(DATE_ORDER, "t0.r2.c5")]


def test_같은_날짜_기간은_통과():
    data = 교통비_입력()
    data.cells["t0.r2.c5"] = "2026.10.12 ~ 2026.10.12"
    assert validate(교통비(), data) == []


def test_시각의_물결은_기간이_아니다():
    """`13:00~17:00` 은 시각이다. 날짜가 하나뿐이라 순서를 볼 게 없다 (오탐 사례)."""
    data = 교통비_입력()
    data.cells["t0.r2.c5"] = "2026.10.12(월) 17:00~13:00"
    assert validate(교통비(), data) == []


def test_날짜_칸이_아니면_안_본다():
    data = 교통비_입력()
    data.cells["t0.r1.c2"] = "2026.02.30 신설 학과"
    assert validate(교통비(), data) == []


def test_반복영역_칸도_날짜를_본다():
    data = 교통비_입력()
    data.rows["t0.r7-16"][1][4] = "2026.02.30"
    [issue] = validate(교통비(), data)
    assert (issue.code, issue.where) == (DATE_INVALID, "t0.r7-16[1].c4")
    assert (issue.location.kind, issue.location.row, issue.location.index) == ("repeat", 8, 1)


# ------------------------------------------------------------------ 금액 합계
def test_합계가_틀리면_잡는다():
    [issue] = validate(교통비(), 교통비_입력(total="20,000"))
    assert (issue.code, issue.where) == (AMOUNT_TOTAL_MISMATCH, "t0.r17.c11")
    assert "15,000원" in issue.message and "20,000원" in issue.message
    assert "15,000원" in issue.action


def test_예시_행도_합계에_포함한다():
    """옛 엔진에서는 예시 행이 반복영역 밖이라 한 줄 모자라게 더해 오탐이 났다."""
    data = 교통비_입력(total="10,000", amounts=("7,000", "3,000"))
    assert validate(교통비(), data) == []


def test_일부만_채워도_채운_것끼리_검사한다():
    data = 교통비_입력(total="5,000", amounts=("5,000", "", "해당없음"))
    assert validate(교통비(), data) == []


def test_합계가_비어_있으면_넘어간다():
    """빈 합계는 '필수 누락' 이 잡는다. 같은 칸에 두 번 경고하지 않는다."""
    issues = validate(교통비(), 교통비_입력(total=""))
    assert codes(issues) == [(REQUIRED_EMPTY, "t0.r17.c11")]


def test_데이터가_하나도_없으면_넘어간다():
    data = 교통비_입력()
    data.rows = {}
    assert check_amount_totals(교통비(), data) == []


def test_소계와_합계는_구간을_나눠_센다():
    """정산서처럼 항목이 고정된 표: 소계(4행) 다음 행부터 다시 센다. 소계를 두 번 더하지 않는다."""
    cells = {f"t0.r{r}.c2": "1,000" for r in (1, 2, 3)} | {"t0.r4.c2": "3,000"}
    cells |= {f"t0.r{r}.c2": "2,000" for r in (5, 6, 7)} | {"t0.r8.c2": "6,000"}
    assert validate(정산서(), FormInput(cells=cells)) == []
    cells["t0.r8.c2"] = "9,000"  # 소계까지 더한 값을 적으면 틀린 것이다
    assert codes(validate(정산서(), FormInput(cells=cells))) == [
        (AMOUNT_TOTAL_MISMATCH, "t0.r8.c2")
    ]


def test_연번_열의_합계_칸은_연번을_더하지_않는다():
    """학생모니터링 활동비 지급 내역서: `합계 : 10,500원 × 00시간` 이 연번 열에 있다 (오탐 사례)."""
    schema = 교통비()
    schema.slots.append(SlotSpec(table=0, row=17, col=0, label="합계", placeholder="00시간"))
    data = 교통비_입력()
    data.cells["t0.r17.c0"] = "합계 : 10,500원 × 30시간 = 315,000원"
    assert validate(schema, data) == []


def test_합계_위의_날짜_칸은_더하지_않는다():
    """연구수당 기여도 평가서: 같은 열에 날짜 칸이 있어 2026 이 금액으로 더해졌다 (오탐 사례)."""
    schema = 정산서()
    schema.slots.append(SlotSpec(table=0, row=0, col=2, label="작성일", input_type="date",
                                 placeholder="2000.00.00"))  # fmt: skip
    cells = {f"t0.r{r}.c2": "1,000" for r in (1, 2, 3)} | {"t0.r4.c2": "3,000"}
    cells |= {"t0.r0.c2": "2026.10.12", "t0.r8.c2": "0"}
    assert validate(schema, FormInput(cells=cells)) == []


def test_금액이_아닌_글자는_더하지_않는다():
    cells = {"t0.r1.c2": "1,000", "t0.r2.c2": "약 2,000원 예상", "t0.r3.c2": "1,000원"}
    cells |= {"t0.r4.c2": "2,000", "t0.r8.c2": "0"}
    assert validate(정산서(), FormInput(cells=cells)) == []


def test_수식_합계는_우리가_계산하므로_보지_않는다():
    schema = 교통비()
    schema.slots = [s for s in schema.slots if s.label != "합계"]  # 합계 칸이 수식이면 슬롯이 없다
    assert check_amount_totals(schema, 교통비_입력(total="1")) == []


# ------------------------------------------------------------------ 변형
def test_고른_변형의_칸만_본다():
    schema = FormSchema(
        title="기안문",
        tables=tables(2),
        variants=[Variant(index=0, label="1-01호", first_table=0, last_table=0),
                  Variant(index=1, label="1-02호", first_table=1, last_table=1)],
        slots=[SlotSpec(table=0, row=0, col=0, label="제목", placeholder="○○○"),
               SlotSpec(table=1, row=0, col=0, label="제목", placeholder="○○○")],
    )  # fmt: skip
    assert codes(validate(schema, FormInput(variant=1))) == [(REQUIRED_EMPTY, "t1.r0.c0")]
    assert len(validate(schema, FormInput())) == 2


# ------------------------------------------------------------------ 설정
def test_규칙을_끌_수_있다():
    config = ValidationConfig(default=RuleConfig(disabled=frozenset({AMOUNT_TOTAL_MISMATCH})))
    assert validate(교통비(), 교통비_입력(total="1"), config) == []


def test_서식별로_예외를_줄_수_있다():
    """다른 서식에는 영향이 없다."""
    config = ValidationConfig(forms={"t/교통비.hwpx": RuleConfig(ignore=frozenset({"t0.r1.c2"}))})
    data = 교통비_입력()
    del data.cells["t0.r1.c2"]
    assert validate(교통비(), data, config) == []
    other = 교통비().model_copy(update={"template": "t/다른서식.hwpx"})
    assert len(validate(other, data, config)) == 1


def test_심각도를_바꿀_수_있다():
    config = ValidationConfig(default=RuleConfig(severity={DATE_ORDER: "warning"}))
    data = 교통비_입력()
    data.cells["t0.r2.c5"] = "2026.10.14 ~ 2026.10.12"
    assert [i.severity for i in validate(교통비(), data, config)] == ["warning"]


def test_제외_규칙을_끄면_항목명만_있던_빈_칸도_필수다():
    config = ValidationConfig(default=RuleConfig(exclusions=Exclusions(optional_empty=False)))
    assert codes(validate(교통비(), 교통비_입력(), config)) == [(REQUIRED_EMPTY, "t0.r3.c2")]


# ------------------------------------------------------------------ 칸 단위 호출
def test_칸_하나를_바로_점검한다():
    assert codes(check_cell(교통비(), "t0.r1.c2", "")) == [(REQUIRED_EMPTY, "t0.r1.c2")]
    assert check_cell(교통비(), "t0.r1.c2", "전자공학과") == []
    assert codes(check_cell(교통비(), "t0.r2.c5", "2026.13.01")) == [(DATE_INVALID, "t0.r2.c5")]


def test_칸_단위와_문서_전체가_같은_판정을_낸다():
    data = 교통비_입력()
    data.cells["t0.r2.c5"] = "2026.10.14 ~ 2026.10.12"
    assert check_cell(교통비(), "t0.r2.c5", data.cells["t0.r2.c5"]) == validate(교통비(), data)


def test_누름틀과_반복영역_칸도_바로_점검한다():
    assert codes(check_field(기안문(), "enforcedate", "2026.02.30")) == [
        (DATE_INVALID, "enforcedate")
    ]
    assert check_field(기안문(), "apb4_date", "") == []
    assert codes(check_repeat_cell(교통비(), "t0.r7-16", 2, 4, "2026.02.30")) == [
        (DATE_INVALID, "t0.r7-16[2].c4")
    ]


def test_없는_자리를_가리키면_KeyError():
    with pytest.raises(KeyError):
        check_cell(교통비(), "t9.r9.c9", "")
    with pytest.raises(KeyError):
        check_field(기안문(), "nope", "")
    with pytest.raises(KeyError):
        check_repeat_cell(교통비(), "t0.r7-16", 0, 99, "")


# ------------------------------------------------------------------ 적용 규칙
def test_서식에_적용되는_규칙을_알려준다():
    assert applicable_rules(교통비()) == {
        REQUIRED_EMPTY, DATE_INVALID, DATE_ORDER, AMOUNT_TOTAL_MISMATCH, AMOUNT_INVALID,
        ROW_DUPLICATE, FILL_LEFT,
    }  # fmt: skip
    assert applicable_rules(정산서()) == {REQUIRED_EMPTY, AMOUNT_TOTAL_MISMATCH, FILL_LEFT}


# ------------------------------------------------------------------ 금액 형식 (IN_15)
@pytest.mark.parametrize("value", ["5,000", "5,000원", "₩ 5,000", r"\ 650,000", "650,000원정",
                                   "해당없음", "-"])  # fmt: skip
def test_금액으로_읽히는_값은_통과한다(value):
    assert check_repeat_cell(교통비(), "t0.r7-16", 0, 11, value) == []


def test_금액_칸에_글자를_넣으면_경고한다():
    [issue] = check_repeat_cell(교통비(), "t0.r7-16", 0, 11, "오천원")
    assert (issue.code, issue.severity) == (AMOUNT_INVALID, "warning")
    [issue] = check_cell(교통비(), "t0.r17.c11", "만오천")
    assert issue.code == AMOUNT_INVALID


def test_금액_형식_경고도_심각도를_설정으로_바꾼다():
    config = ValidationConfig(RuleConfig(severity={AMOUNT_INVALID: "error"}))
    [issue] = check_cell(교통비(), "t0.r17.c11", "만오천", config)
    assert issue.severity == "error"


# ------------------------------------------------------------------ 명단 중복 (IN_16)
def test_명단에_같은_학번이_두_번_나오면_두_번째_행에_경고한다():
    data = 교통비_입력()
    for row, number in zip(data.rows["t0.r7-16"], ("2026001", "2026002", "2026 001"),
                           strict=True):  # fmt: skip
        row[3] = number
    [issue] = [i for i in validate(교통비(), data) if i.code == ROW_DUPLICATE]
    assert (issue.severity, issue.where) == ("warning", "t0.r7-16[2].c3")
    assert "1번째 행" in issue.message


def test_학번이_비었거나_다르면_중복이_아니다():
    data = 교통비_입력()
    for row, number in zip(data.rows["t0.r7-16"], ("", "", "2026003"), strict=True):
        row[3] = number
    assert validate(교통비(), data) == []


def test_같은_이름은_동명이인일_수_있어_보지_않는다():
    data = 교통비_입력()
    for row in data.rows["t0.r7-16"]:
        row[1] = "김철수"
    assert validate(교통비(), data) == []


def test_채우지_않은_자리가_남으면_FILL_LEFT_경고():
    """제출 문장은 문장째 미리 채워 두므로 날짜·이름을 고치지 않고 내기 쉽다."""
    left = check_cell(정산서(), "t0.r1.c2", "제출합니다. 20○○년 ○○월 ○○일 담당 : ○ ○ ○ (날인)")
    assert [(i.code, i.severity) for i in left] == [(FILL_LEFT, "warning")]
    done = "제출합니다. 2026년 10월 30일 담당 : 김민수 (날인)"
    assert check_cell(정산서(), "t0.r1.c2", done) == []
