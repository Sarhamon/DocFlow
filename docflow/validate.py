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

from docflow.schema.form import FormSchema, is_total_label

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


def validate(schema: FormSchema, values: dict[str, str]) -> list[Issue]:
    """서식 스키마와 입력값을 받아 위반 사항을 모아 반환한다."""
    return check_amount_totals(schema, values)
