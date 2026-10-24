"""실제 서식(templates/) 전부에 검증 엔진을 돌려 보는 로컬 회귀 테스트 (M3 완료 기준).

내부 자료가 필요해서 `pytest -m local` 로만 돈다.

1. 규칙별 적용 서식 수가 기준과 같다.
2. 오탐 0: 모든 입력칸에 올바른 값을 넣으면 183개 서식 모두 아무것도 나오지 않는다.
   합계는 금액 칸만 더해 따로 계산한다. 검증 엔진이 합계 위의 숫자·날짜 칸까지 더하면 여기서 걸린다.
3. 일부러 넣은 오류는 그 자리에서 그 코드로 정확히 잡힌다.
"""

from __future__ import annotations

from collections import Counter

import pytest

from docflow.config import get_settings
from docflow.schema.model import FormSchema
from docflow.schema.rules import is_total_text
from docflow.validate import (
    AMOUNT_INVALID,
    AMOUNT_TOTAL_MISMATCH,
    DATE_INVALID,
    DATE_ORDER,
    REQUIRED_EMPTY,
    ROW_DUPLICATE,
    FormInput,
    applicable_rules,
    validate,
)
from tools.extract_schema import extract_file, is_form

pytestmark = pytest.mark.local

#: 규칙별 적용 서식 수. 2026-10 측정 (스키마 v2).
#: 옛 엔진(v1)의 177/78/95 와 기준이 다르다. 필수는 `required` 슬롯·자리표시자 누름틀,
#: 날짜는 입력 유형(라벨·누름틀 이름 포함), 합계는 작성자가 적는 합계 칸만 센다
#: (수식 합계는 우리가 계산하므로 틀릴 수 없어 규칙 대상이 아니다).
EXPECTED_APPLICABLE = {
    REQUIRED_EMPTY: 183,
    AMOUNT_TOTAL_MISMATCH: 49,
    DATE_INVALID: 107,
    DATE_ORDER: 107,
    AMOUNT_INVALID: 102,
    ROW_DUPLICATE: 54,
}

#: 입력 유형별 올바른 값
VALID = {"text": "가나다", "number": "3", "amount": "1,000", "date": "2026.10.12", "checkbox": "■"}
#: 금액 항목이 될 수 없는 유형. 합계 구간에 있어도 이 값들은 더하면 안 된다
NOT_AMOUNT = {"number", "date", "checkbox"}
ROWS = 2
#: 합계 오류를 넣을 수 있는(더할 금액 항목이 있는) 서식 수. 나머지 4개는 합계 칸만 있고
#: 항목이 수식·수량 칸이라 더할 것이 없다
EXPECTED_TOTAL_INJECTED = 45


@pytest.fixture(scope="module")
def schemas() -> dict[str, FormSchema]:
    settings = get_settings()
    files = sorted(f for f in settings.templates.rglob("*.hwpx") if is_form(f))
    if not files:
        pytest.skip("templates/ 가 없다")
    return {str(f.relative_to(settings.templates)): extract_file(f, settings) for f in files}


def valid_input(schema: FormSchema) -> FormInput:
    """모든 입력칸을 올바르게 채운 입력.

    합계 칸의 구간(반복영역 열 / 같은 열의 직전 합계 다음 칸들) 가운데 금액이 될 수 있는 칸에는
    1,000 을 넣고, 합계는 그 칸 수로 따로 계산한다. 구간의 연번·수량·날짜 칸은 유형대로 채우므로
    검증 엔진이 그 값까지 더하면 오탐으로 드러난다.
    """
    data = FormInput()
    for f in schema.fields:
        if f.role == "input":
            data.fields[f.name] = VALID[f.input_type]
    inputs = [s for s in schema.slots if s.role == "input"]
    totals = [s for s in inputs if is_total_text(s.label)]
    for s in inputs:
        data.cells[s.id] = VALID[s.input_type]
    total_rows = {(r.table, r.total_row): r for r in schema.repeats if r.total_row is not None}
    summed_cols = {
        (r.id, s.col) for s in totals if (r := total_rows.get((s.table, s.row))) is not None
    }
    for rep in schema.repeats:
        data.rows[rep.id] = [
            {
                c.col: "1,000"
                if (rep.id, c.col) in summed_cols and c.input_type not in NOT_AMOUNT
                else f"20260{i:02d}"  # 명단의 학번은 사람마다 다르다
                if "학번" in c.label
                else VALID[c.input_type]
                for c in rep.columns
                if c.role == "input"
            }
            for i in range(ROWS)
        ]

    for s in sorted(totals, key=lambda x: (x.table, x.col, x.row)):
        rep = total_rows.get((s.table, s.row))
        if rep is not None:
            col = next((c for c in rep.columns if c.col == s.col), None)
            count = ROWS if col is not None and col.input_type not in NOT_AMOUNT else 0
        else:
            prev = max(
                (t.row for t in totals if (t.table, t.col) == (s.table, s.col) and t.row < s.row),
                default=-1,
            )
            items = [
                x
                for x in inputs
                if (x.table, x.col) == (s.table, s.col)
                and prev < x.row < s.row
                and x.input_type not in NOT_AMOUNT
                and not is_total_text(x.label)
            ]
            for x in items:
                data.cells[x.id] = "1,000"
            count = len(items)
        data.cells[s.id] = f"{1000 * count:,}"
    return data


def test_규칙별_적용_서식_수(schemas):
    counts = Counter(rule for s in schemas.values() for rule in applicable_rules(s))
    assert len(schemas) == 183
    assert dict(counts) == EXPECTED_APPLICABLE


def test_올바른_값을_넣으면_아무것도_나오지_않는다(schemas):
    found = {
        k: [(i.code, i.where, i.message) for i in validate(s, valid_input(s))]
        for k, s in schemas.items()
    }
    found = {k: v for k, v in found.items() if v}
    detail = "\n".join(f"{k}: {v[:3]}" for k, v in list(found.items())[:15])
    assert not found, f"오탐 {sum(map(len, found.values()))}건 / 서식 {len(found)}개\n{detail}"


def _first(schema: FormSchema, pred):
    return next((s for s in schema.slots if s.role == "input" and pred(s)), None)


def _first_date(schema: FormSchema) -> str | None:
    """날짜 칸 하나의 자리 (표 칸 -> 누름틀 -> 반복영역 첫 행 순서)."""
    if (s := _first(schema, lambda x: x.input_type == "date")) is not None:
        return s.id
    for f in schema.fields:
        if f.role == "input" and f.input_type == "date":
            return f.name
    for r in schema.repeats:
        for c in r.columns:
            if c.role == "input" and c.input_type == "date":
                return f"{r.id}[0].c{c.col}"
    return None


def _with(base: FormInput, where: str, value: str) -> FormInput:
    """where 한 자리만 value 로 바꾼 입력."""
    cells, fields = dict(base.cells), dict(base.fields)
    rows = {k: [dict(r) for r in v] for k, v in base.rows.items()}
    if "[" in where:
        rid, rest = where.split("[")
        index, col = rest.split("].c")
        rows[rid][int(index)][int(col)] = value
    elif where in fields:
        fields[where] = value
    else:
        cells[where] = value
    return FormInput(cells=cells, fields=fields, rows=rows)


def test_일부러_넣은_오류를_그_자리에서_잡는다(schemas):
    """서식마다 규칙별로 오류 한 건을 넣고, 그 자리에서 그 코드 하나만 나오는지 본다."""
    missed: list[str] = []
    caught: Counter[str] = Counter()
    for k, schema in schemas.items():
        base = valid_input(schema)
        cases = []
        if (s := _first(schema, lambda x: x.required)) is not None:
            cases.append((s.id, "", REQUIRED_EMPTY))
        if (where := _first_date(schema)) is not None:
            cases.append((where, "2026.02.30", DATE_INVALID))
            cases.append((where, "2026.10.14 ~ 2026.10.12", DATE_ORDER))
        for t in (x for x in schema.slots if x.role == "input" and is_total_text(x.label)):
            if base.cells[t.id] != "0":  # 더할 금액 항목이 있는 합계 칸
                wrong = int(base.cells[t.id].replace(",", "")) + 7
                cases.append((t.id, f"{wrong:,}", AMOUNT_TOTAL_MISMATCH))
                break
        for where, value, code in cases:
            data = _with(base, where, value)
            got = [(i.code, i.where) for i in validate(schema, data)]
            if got == [(code, where)]:
                caught[code] += 1
            else:
                missed.append(f"{k} {code}@{where}: {got}")
    assert not missed, f"{len(missed)}건\n" + "\n".join(missed[:20])
    assert caught == {
        REQUIRED_EMPTY: 183, DATE_INVALID: 107, DATE_ORDER: 107,
        AMOUNT_TOTAL_MISMATCH: EXPECTED_TOTAL_INJECTED,
    }  # fmt: skip
