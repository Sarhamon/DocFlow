"""실제 서식(templates/) 전부에서 스키마를 뽑아 보는 로컬 회귀 테스트 (M2 완료 기준).

내부 자료가 필요해서 `pytest -m local` 로만 돈다.

- 서식 183개 전부 추출, 실패 0 (서식 목차·신구대조표 10개는 서식이 아니라 뺀다)
- 채울 지점(슬롯·반복영역)이 없는 서식 0개
- 스키마가 가리키는 좌표에 실제로 쓸 수 있다: 슬롯은 set_cell, 반복영역은 fill_rows(행 늘리기 포함)
- 반복영역을 늘려 쓴 뒤에도 행마다 있는 수식을 다시 계산할 수 있다
"""

from __future__ import annotations

import pytest

from docflow.config import get_settings
from docflow.hwpx.document import HwpxDocument
from docflow.schema.model import FormSchema, RepeatSpec
from tools.extract_schema import extract_file, is_form

pytestmark = pytest.mark.local

EXPECTED_TEMPLATES = 193
EXPECTED_FORMS = 183


@pytest.fixture(scope="module")
def schemas() -> dict[str, FormSchema]:
    settings = get_settings()
    files = sorted(settings.templates.rglob("*.hwpx"))
    if not files:
        pytest.skip("templates/ 가 없다")
    assert len(files) == EXPECTED_TEMPLATES
    return {
        str(f.relative_to(settings.templates)): extract_file(f, settings)
        for f in files
        if is_form(f)
    }


def test_서식을_모두_추출한다(schemas):
    assert len(schemas) == EXPECTED_FORMS


def test_채울_지점이_없는_서식이_없다(schemas):
    empty = [k for k, s in schemas.items() if not s.slots and not s.repeats]
    assert empty == []


def test_모든_서식이_사업과_카테고리를_가진다(schemas):
    missing = [k for k, s in schemas.items() if not s.program or s.category_no is None]
    assert missing == []


def _rows(rep: RepeatSpec, n: int) -> tuple[int, list[list[str | None]]]:
    """반복영역 열에 n 행을 쓰는 fill_rows 인자. 병합으로 가려진 열은 None 으로 건너뛴다."""
    first = rep.columns[0].col
    width = rep.columns[-1].col - first + 1
    rows: list[list[str | None]] = []
    for _ in range(n):
        row: list[str | None] = [None] * width
        for c in rep.columns:
            if c.role == "input":  # 수식 열은 쓰지 않는다
                row[c.col - first] = "1"  # 숫자라야 행마다 있는 수식이 계산된다
        rows.append(row)
    return first, rows


def test_스키마가_가리키는_칸에_쓸_수_있다(schemas, tmp_path):
    settings = get_settings()
    errors: list[str] = []
    for rel, schema in schemas.items():
        doc = HwpxDocument.open(settings.templates / rel)
        for slot in schema.slots:
            try:
                doc.set_cell(slot.table, slot.row, slot.col, "1")
            except Exception as exc:
                errors.append(f"{rel} 슬롯 {slot.id}: {exc!r}")
        # 행을 늘리면 아래 표 행 번호가 밀리므로 아래쪽 반복영역부터 쓴다
        for rep in sorted(schema.repeats, key=lambda r: (r.table, -r.start_row)):
            col, rows = _rows(rep, rep.capacity + 2)
            try:
                doc.fill_rows(
                    rep.table, rep.start_row, rows, col, last_row=rep.end_row, extend=True
                )
            except Exception as exc:
                errors.append(f"{rel} 반복영역 {rep.id}: {exc!r}")
        skipped = [f for f, _why in doc.recalculate_formulas().skipped if f.text]
        # 계산 못 한 수식이 원래부터 결과가 비어 있던 것(서식의 빈 행)뿐이어야 한다
        errors += [f"{rel} 수식 t{f.table}.r{f.row}.c{f.col} {f.formula}" for f in skipped]
        doc.save(tmp_path / "out.hwpx")
    assert not errors, f"{len(errors)}건:\n" + "\n".join(errors[:30])
