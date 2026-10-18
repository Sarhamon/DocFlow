"""실제 서식(templates/) 전부에 쓰기 → 저장 → 재읽기 라운드트립을 도는 로컬 회귀 테스트.

M1(HWPX 코어)의 완료 기준이다. 내부 자료가 필요해서 `pytest -m local` 로만 돈다.

- 누름틀: CLICK_HERE 누름틀 전부에 이름별 값을 채운다 (빈 누름틀 포함).
- 표 셀: 수식 칸을 뺀 모든 앵커 셀에 여러 줄·XML 특수문자가 든 값을 쓴다.
  누름틀이 든 칸(FieldInCell)은 거부되는 게 정상이라 건너뛴다.
- 행 복제: 표마다 마지막 행을 복제하고 새 행에 쓴다.
  누름틀이 든 행, 이 행에서 아래로 병합된 행의 거부(FieldInCell / RowSpanConflict)는 정상이다.
"""

from __future__ import annotations

import pytest

from docflow.config import get_settings
from docflow.hwpx.document import FieldInCell, HwpxDocument, RowSpanConflict

pytestmark = pytest.mark.local

#: 2026-10 측정: 서식 193개, 셀 57,431 / 행 복제 2,049 / 누름틀 2,301 라운드트립 일치
EXPECTED_FILES = 193


def _roundtrip(path, tmp_path) -> list[str]:
    doc = HwpxDocument.open(path)
    names = sorted({f.name for f in doc.fields() if f.type == "CLICK_HERE"})
    fields = {n: f"값{i}" for i, n in enumerate(names)}
    doc.fill(fields)

    formula = {(f.table, f.row, f.col) for f in doc.formula_cells()}
    cells: dict[tuple[int, int, int], str] = {}
    for t in doc.tables():
        for c in t.cells:
            key = (t.index, c.row, c.col)
            if key in formula:
                continue
            value = f"표{t.index} {c.row}행 {c.col}열\n둘째 줄 & <x>"
            try:
                doc.set_cell(*key, value)
            except FieldInCell:
                continue
            cells[key] = value

    for t in doc.tables():
        try:
            new = doc.insert_row(t.index, t.rows - 1)
        except (FieldInCell, RowSpanConflict):
            continue
        # 복제한 행 아래는 밀렸으니 그 표의 기대값은 새 행 앞까지만 남긴다
        cells = {k: v for k, v in cells.items() if k[0] != t.index or k[1] < new}
        # 새 행의 첫 칸은 왼쪽 묶음 칸에 가려졌거나 수식일 수 있다
        target = next(
            (c for c in doc.tables()[t.index].cells if c.row == new and not c.has_field), None
        )
        if target is not None:
            doc.set_cell(t.index, new, target.col, "새 행")
            cells[(t.index, new, target.col)] = "새 행"

    again = HwpxDocument.open(doc.save(tmp_path / path.name))
    errors = [
        f"{path.name}: 누름틀 {f.name}={f.value!r}"
        for f in again.fields()
        if f.name in fields and f.value != fields[f.name]
    ]
    errors += [
        f"{path.name}: 표{k[0]} ({k[1]},{k[2]})={got!r}"
        for k, v in cells.items()
        if (got := again.get_cell(*k)) != v
    ]
    return errors


def test_모든_서식이_쓰기_저장_재읽기를_통과한다(tmp_path):
    files = sorted(get_settings().templates.rglob("*.hwpx"))
    if not files:
        pytest.skip("templates/ 가 없다")
    assert len(files) == EXPECTED_FILES

    errors = [e for path in files for e in _roundtrip(path, tmp_path)]
    assert not errors, f"{len(errors)}건 불일치:\n" + "\n".join(errors[:20])
