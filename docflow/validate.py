# -*- coding: utf-8 -*-
"""규칙 기반 검증 — 입력값이 서식 규칙을 지키는지 확인한다.

결재 문서라 같은 입력에 같은 판정이 나와야 하므로 재현 가능한 규칙만 여기서
다룬다. 지침 해석이 필요한 정성 검증은 LLM 몫이다 (README 3.2).

입력값은 셀 주소를 키로 하는 평평한 딕셔너리다. 주소 형식은 스키마의
`SlotSpec.id` 와 같은 `t{표}.r{행}.c{열}` 이다.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from docflow.schema.form import FormSchema, is_date_placeholder, is_total_label

#: "5,000", "5,000원", "₩ 5,000" 에서 숫자 부분만 뽑는다.
_AMOUNT_RE = re.compile(r"-?\d[\d,]*")


def parse_amount(text: str) -> int | None:
    """금액 문자열을 정수로. 금액으로 읽히지 않으면 None."""
    m = _AMOUNT_RE.search(str(text).replace(" ", ""))
    if not m:
        return None
    return int(m.group().replace(",", ""))


def cell_id(table: int, row: int, col: int) -> str:
    return f"t{table}.r{row}.c{col}"


@dataclass(frozen=True)
class Issue:
    """검증에 걸린 한 건."""

    code: str
    message: str
    where: str = ""


def _repeat_rows(schema: FormSchema) -> set[tuple[int, int]]:
    """반복 영역이 덮는 (표, 행) 좌표."""
    return {
        (rep.table, row)
        for rep in schema.repeats
        for row in range(rep.start_row, rep.end_row + 1)
    }


def check_required(schema: FormSchema, values: dict[str, str]) -> list[Issue]:
    """자리표시자가 박혀 있던 칸이 비어 있는지 확인한다.

    자리표시자는 채워야 할 곳의 가장 확실한 신호라 항목명 추론보다 우선한다
    (README 2③). 항목명만 보고 잡은 빈 칸(`kind="empty"`)은 추론이라 여기서
    다루지 않는다 — 첨부 확인란처럼 안 채우는 게 정상인 칸이 섞인다.

    반복 영역 안의 칸도 뺀다. 명단은 수용 행을 다 채우지 않는 게 정상이다.
    """
    covered = _repeat_rows(schema)

    issues: list[Issue] = []
    for slot in schema.slots:
        if slot.kind != "placeholder" or (slot.table, slot.row) in covered:
            continue
        if str(values.get(slot.id, "")).strip():
            continue

        # 항목명과 자리표시자에는 줄바꿈이 섞여 있다. 한 줄로 펴서 보여준다.
        label = " ".join(slot.label.split())
        example = " ".join(slot.placeholder.split())
        if label:
            message = f"'{label}' 칸이 비어 있습니다."
        else:
            message = "비어 있는 칸이 있습니다."
        if example:
            message += f" (서식 예시: {example})"
        issues.append(Issue(code="REQUIRED_EMPTY", message=message, where=slot.id))
    return issues


def check_amount_totals(schema: FormSchema, values: dict[str, str]) -> list[Issue]:
    """금액 열의 합이 합계 셀과 맞는지 확인한다.

    합계 셀은 스키마의 `label` 로 찾는다. 반복 영역을 기준으로 삼지 않는 이유는,
    명단 첫 줄이 예시 행(`홍길동`)이라 반복 영역에서 빠지기 때문이다. 그대로
    더하면 한 줄이 모자라 오탐이 난다. 대신 같은 열에서 직전 합계 다음 행부터
    센다. 소계와 합계가 같이 있는 표도 이 방식이면 구간이 맞는다.
    """
    totals: dict[tuple[int, int], list[int]] = {}
    for slot in schema.slots:
        if is_total_label(slot.label):
            totals.setdefault((slot.table, slot.col), []).append(slot.row)

    issues: list[Issue] = []
    for (table, col), rows in sorted(totals.items()):
        lower = -1
        for total_row in sorted(rows):
            total_at = cell_id(table, total_row, col)
            declared = parse_amount(values.get(total_at, ""))

            items = []
            for row in range(lower + 1, total_row):
                amount = parse_amount(values.get(cell_id(table, row, col), ""))
                if amount is not None:
                    items.append(amount)
            lower = total_row

            # 안 채운 칸은 '필수 누락' 규칙이 볼 몫이라 여기서는 넘어간다.
            if declared is None or not items:
                continue

            actual = sum(items)
            if actual != declared:
                issues.append(
                    Issue(
                        code="AMOUNT_TOTAL_MISMATCH",
                        message=(
                            f"{len(items)}개 항목의 합은 {actual:,}원인데 "
                            f"합계에 {declared:,}원이 적혀 있습니다."
                        ),
                        where=total_at,
                    )
                )
    return issues


#: 입력값에 쓰인 날짜. 자리표시자와 달리 실제 숫자만 받는다.
_DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def find_dates(text: str) -> list[date | None]:
    """값에 담긴 날짜를 등장 순서대로 반환한다.

    날짜 모양이지만 달력에 없는 날(2026.02.30)은 None 으로 남긴다.
    """
    found: list[date | None] = []
    for year, month, day in _DATE_RE.findall(str(text)):
        try:
            found.append(date(int(year), int(month), int(day)))
        except ValueError:
            found.append(None)
    return found


def check_dates(schema: FormSchema, values: dict[str, str]) -> list[Issue]:
    """날짜 칸의 값이 실제 날짜인지, 기간이 거꾸로가 아닌지 확인한다.

    기간은 `2000.00.00 ~ 2000.00.00` 처럼 칸 하나에 들어 있다. 그런데
    `2024.5.2.(목) 13:00~17:00` 의 물결은 시각을 나누는 것이라, 물결로 쪼개지
    않고 값에서 날짜만 뽑아 순서를 본다.
    """
    issues: list[Issue] = []
    for slot in schema.slots:
        if not is_date_placeholder(slot.placeholder):
            continue

        raw = " ".join(str(values.get(slot.id, "")).split())
        if not raw:
            continue  # 빈 칸은 '필수 누락' 규칙이 볼 몫이다

        label = " ".join(slot.label.split()) or slot.id
        found = find_dates(raw)

        if not found:
            issues.append(
                Issue("DATE_INVALID", f"'{label}' 을 날짜로 읽을 수 없습니다: {raw}", slot.id)
            )
            continue
        if None in found:
            issues.append(
                Issue("DATE_INVALID", f"'{label}' 에 없는 날짜가 있습니다: {raw}", slot.id)
            )
            continue

        dates = [d for d in found if d is not None]
        for start, end in zip(dates, dates[1:]):
            if start > end:
                issues.append(
                    Issue(
                        "DATE_ORDER",
                        f"'{label}' 의 기간이 거꾸로입니다: "
                        f"{start:%Y.%m.%d} 이 {end:%Y.%m.%d} 보다 뒤입니다.",
                        slot.id,
                    )
                )
                break
    return issues


def validate(schema: FormSchema, values: dict[str, str]) -> list[Issue]:
    """서식 스키마와 입력값을 받아 위반 사항을 모아 반환한다."""
    return (
        check_required(schema, values)
        + check_amount_totals(schema, values)
        + check_dates(schema, values)
    )
