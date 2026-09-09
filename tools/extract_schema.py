# -*- coding: utf-8 -*-
"""templates/ 의 HWPX 서식에서 서식별 스키마 JSON 을 추출한다.

    python tools/extract_schema.py

산출: schemas/<서식번호>.json, schemas/index.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from docflow.hwpx.document import Cell, HwpxDocument, Table  # noqa: E402
from docflow.schema.form import (  # noqa: E402
    FieldSpec,
    FormSchema,
    RepeatSpec,
    SlotSpec,
    TableSpec,
    is_placeholder,
    parse_form_name,
)

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE_ROOT = ROOT / "templates"
OUT_ROOT = ROOT / "schemas"


def find_label(cell: Cell, cells: list[Cell]) -> str:
    """셀의 항목명을 찾는다: 같은 행 왼쪽 -> 같은 열 위쪽 순."""
    left = [
        c
        for c in cells
        if c.row == cell.row and c.col < cell.col and c.text and not is_placeholder(c.text)
    ]
    if left:
        return max(left, key=lambda c: c.col).text
    above = [
        c
        for c in cells
        if c.col == cell.col and c.row < cell.row and c.text and not is_placeholder(c.text)
    ]
    if above:
        return max(above, key=lambda c: c.row).text
    return ""


def build_slots(table: Table) -> list[SlotSpec]:
    """표에서 값을 채워야 할 지점을 뽑는다.

    - placeholder: ○○○ / 0000 / □ 등이 박혀 있는 셀 (가장 확실한 신호)
    - empty:       항목명이 붙어 있는 빈 셀
    """
    slots: list[SlotSpec] = []
    for cell in table.cells:
        if is_placeholder(cell.text):
            kind = "placeholder"
        elif not cell.text:
            kind = "empty"
        else:
            continue
        label = find_label(cell, table.cells)
        if kind == "empty" and not label:
            continue  # 항목명도 없는 빈 셀은 입력 지점으로 보기 어렵다
        slots.append(
            SlotSpec(
                id=f"t{table.index}.r{cell.row}.c{cell.col}",
                table=table.index,
                row=cell.row,
                col=cell.col,
                label=label,
                placeholder=cell.text,
                kind=kind,
            )
        )
    return slots


def find_repeats(table: Table) -> list[RepeatSpec]:
    """명단·내역처럼 빈 행이 연달아 나오는 반복 영역을 찾는다.

    판정: 채워진 셀이 열 수의 30% 이하인 행이 3줄 이상 이어지면 반복 영역.
    열 이름을 못 찾으면 채울 방법이 없으므로 헤더가 있는 영역만 남긴다.
    """
    grid = table.grid()
    if table.rows < 4 or table.cols < 2:
        return []

    filled = [sum(1 for c in row if c.strip()) for row in grid]
    sparse = max(1, int(table.cols * 0.3))

    repeats: list[RepeatSpec] = []
    start = None
    for r in range(table.rows + 1):
        is_sparse = r < table.rows and filled[r] <= sparse
        if is_sparse and start is None:
            start = r
        elif not is_sparse and start is not None:
            if r - start >= 3:
                rep = _make_repeat(table, grid, filled, start, r - 1)
                if any(rep.header):
                    repeats.append(rep)
            start = None
    return repeats


def _find_header(grid, filled, start: int) -> list[str]:
    """반복 영역 위쪽에서 열 이름 행을 찾는다.

    바로 위에는 보통 예시 행(`1 / 홍길동 / 2000000000`)이 있고 그 위가 진짜 헤더다.
    예시 행은 자리표시자를 포함하므로 그걸로 걸러낸다.
    """
    for r in range(start - 1, max(-1, start - 7), -1):
        row = grid[r]
        if filled[r] < 2:
            continue
        if any(is_placeholder(c) for c in row):
            continue  # 예시 행
        return [" ".join(c.split()) for c in row]
    return []


def _make_repeat(table: Table, grid, filled, start: int, end: int) -> RepeatSpec:
    header = _find_header(grid, filled, start)
    return RepeatSpec(
        id=f"t{table.index}.r{start}-{end}",
        table=table.index,
        start_row=start,
        end_row=end,
        header=header,
    )


def extract(template: Path) -> FormSchema:
    doc = HwpxDocument.open(template)
    form_id, program, title = parse_form_name(template.stem)

    grouped: dict[str, FieldSpec] = {}
    for f in doc.fields():
        spec = grouped.get(f.name)
        if spec is None:
            grouped[f.name] = FieldSpec(
                name=f.name, type=f.type, occurrences=1, placeholder=f.value
            )
        else:
            spec.occurrences += 1
            if not spec.placeholder:
                spec.placeholder = f.value

    tables = doc.tables()
    table_specs: list[TableSpec] = []
    slots: list[SlotSpec] = []
    repeats: list[RepeatSpec] = []
    for t in tables:
        grid = t.grid()
        header = [c for c in grid[0]] if grid else []
        table_specs.append(
            TableSpec(
                index=t.index,
                rows=t.rows,
                cols=t.cols,
                depth=t.depth,
                header=header,
                grid=grid,
            )
        )
        table_repeats = find_repeats(t)
        repeats.extend(table_repeats)
        covered = {
            (t.index, r)
            for rep in table_repeats
            for r in range(rep.start_row, rep.end_row + 1)
        }
        # 반복 영역 안의 빈 셀은 개별 슬롯으로 잡지 않는다 (영역이 대신한다)
        slots.extend(
            s
            for s in build_slots(t)
            if not (s.kind == "empty" and (s.table, s.row) in covered)
        )

    return FormSchema(
        form_id=form_id,
        title=title,
        program=program,
        category=template.parent.name,
        template=str(template.relative_to(ROOT)).replace("\\", "/"),
        fields=list(grouped.values()),
        tables=table_specs,
        slots=slots,
        repeats=repeats,
    )


def main() -> int:
    templates = sorted(TEMPLATE_ROOT.rglob("*.hwpx"))
    if not templates:
        print(f"템플릿이 없습니다: {TEMPLATE_ROOT}  (먼저 tools/convert_templates.py 실행)")
        return 1

    OUT_ROOT.mkdir(exist_ok=True)
    index: dict[str, list[dict]] = {}
    for template in templates:
        rel = template.relative_to(TEMPLATE_ROOT)
        schema = extract(template)
        name = (schema.form_id or template.stem).replace("/", "_")
        # 템플릿 경로를 미러링한다. 원본 폴더명이 유지되어야 공개 가능한 서식과
        # 내부용 서식을 .gitignore 로 구분할 수 있다.
        out = OUT_ROOT / rel.parent / f"{name}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(schema.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        index.setdefault(rel.parts[0], []).append(
            {
                "form_id": schema.form_id,
                "title": schema.title,
                "category": schema.category,
                "schema": str(out.relative_to(OUT_ROOT)).replace("\\", "/"),
                "fields": len(schema.fields),
                "tables": len(schema.tables),
                "slots": len(schema.slots),
                "repeats": len(schema.repeats),
            }
        )
        print(
            f"  {schema.form_id:<10} 필드 {len(schema.fields):>3} "
            f"표 {len(schema.tables):>3} 슬롯 {len(schema.slots):>4} "
            f"반복 {len(schema.repeats):>2}  {schema.title}"
        )

    # 목록도 서식 이름을 담으므로 원본과 같은 폴더 안에 둔다 (통째로 ignore 되도록)
    for group, entries in index.items():
        (OUT_ROOT / group / "index.json").write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    total = sum(len(v) for v in index.values())
    print(f"\n서식 {total}개 -> {OUT_ROOT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
