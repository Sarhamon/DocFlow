"""실제 서식(templates/)의 수식 305개로 계산기를 대조하는 로컬 회귀 테스트.

내부 자료가 필요해서 `pytest -m local` 로만 돈다. 기준은 칸에 표시된 값이다.
서식 제작자가 예시 내용만 지우고 수식 결과를 남겨둔 낡은 값이 있어서(위쪽 칸이 비었는데
300 이 표시된 경우 등) 전부 일치하지는 않는다. 그래서 두 가지를 본다.

1. 모든 수식을 해석한다 (FormulaError 0건). 지원하지 않는 문법이 새로 나오면 여기서 잡힌다.
2. 일치 건수가 기준 이하로 떨어지지 않는다. 계산기가 퇴보하면 여기서 잡힌다.
"""

from __future__ import annotations

import pytest

from docflow.config import get_settings
from docflow.hwpx.document import HwpxDocument
from docflow.hwpx.formula import FormulaError, evaluate, format_result, parse_number

pytestmark = pytest.mark.local

#: 2026-10 측정: 305개 중 220개가 표시값과 일치 (나머지 85개는 서식에 남은 낡은 값, 수식 오류는 0)
EXPECTED_TOTAL = 305
MATCH_FLOOR = 215


def test_서식_수식을_모두_해석하고_표시값과_맞는다():
    settings = get_settings()
    files = sorted(settings.templates.rglob("*.hwpx"))
    if not files:
        pytest.skip("templates/ 가 없다")

    total = matched = 0
    errors: list[str] = []
    for path in files:
        doc = HwpxDocument.open(path)
        grids = {t.index: t.grid() for t in doc.tables()}
        for f in doc.formula_cells():
            total += 1
            try:
                value = evaluate(f.formula, grids[f.table], f.row, f.col)
                text = format_result(value, f.result_format)
            except FormulaError as e:
                errors.append(f"{path.name} {f.formula}: {e}")
                continue
            shown = f.text.replace("%", "")
            if text == shown or value == parse_number(shown):
                matched += 1

    assert not errors, f"해석하지 못한 수식 {len(errors)}개: {errors[:5]}"
    assert total == EXPECTED_TOTAL, f"수식 개수가 달라졌다: {total}"
    assert matched >= MATCH_FLOOR, f"표시값과 일치 {matched}개 (기준 {MATCH_FLOOR}개)"
