"""HWPX 템플릿에서 서식 스키마(v2)를 뽑는다.

문서 구조(표·행·병합)를 보는 판단은 여기, 칸 하나의 텍스트만 보는 판정은 `rules.py` 에 둔다.

* 누름틀  -> fields (결재선 apbN_* 은 role=approval)
* 수식    -> formulas (입력 대상이 아니다)
* 반복영역 -> repeats. 같은 칸 배치의 행이 이어지고 그중 2행 이상이 비어 있으면 명단·내역으로 본다.
* 나머지 표 칸 -> slots. 자리표시자가 있는 칸(필수)과 항목명이 붙은 빈 칸(선택).
* 한 파일에 든 서식 변형 -> variants
"""

from __future__ import annotations

import re
from collections import defaultdict
from pathlib import Path, PurePath

from docflow.config import Settings
from docflow.hwpx.document import Cell, HwpxDocument, Table
from docflow.hwpx.formula import FormulaError, operands
from docflow.schema.model import (
    FieldSpec,
    FormSchema,
    FormulaSpec,
    RepeatColumn,
    RepeatSpec,
    SlotSpec,
    TableSpec,
    Variant,
)
from docflow.schema.rules import (
    FormLocation,
    field_input_type,
    form_numbers,
    infer_input_type,
    is_approval_field,
    is_approval_placeholder,
    is_approval_title,
    is_placeholder,
    is_total_text,
    parse_form_name,
    parse_location,
)


def extract(
    doc: HwpxDocument,
    *,
    stem: str | None = None,
    location: FormLocation | None = None,
    template: str = "",
) -> FormSchema:
    """문서 하나의 스키마. stem 은 파일 이름(서식번호·제목), location 은 경로에서 읽은 분류다."""
    stem = doc.path.stem if stem is None else stem
    location = location or FormLocation()
    form_id, name_program, title = parse_form_name(stem)

    tables = doc.tables()
    formulas = [
        FormulaSpec(
            table=f.table, row=f.row, col=f.col, formula=f.formula, result_format=f.result_format
        )
        for f in doc.formula_cells()
    ]
    repeats: list[RepeatSpec] = []
    slots: list[SlotSpec] = []
    layouts = [_Layout(t) for t in tables]
    for layout in layouts:
        found = find_repeats(layout)
        repeats.extend(found)
        slots.extend(find_slots(layout, found))
    _type_formula_operands(formulas, layouts, slots, repeats)

    return FormSchema(
        form_id=form_id,
        title=title,
        program=location.program or name_program,
        category_no=location.category_no,
        category=location.category,
        archived=location.archived,
        template=template,
        variants=find_variants(doc, form_id, len(tables)),
        fields=find_fields(doc),
        tables=[
            TableSpec(index=t.index, rows=t.rows, cols=t.cols, depth=t.depth, grid=t.grid())
            for t in tables
        ],
        slots=slots,
        formulas=formulas,
        repeats=repeats,
    )


def _type_formula_operands(
    formulas: list[FormulaSpec],
    layouts: list[_Layout],
    slots: list[SlotSpec],
    repeats: list[RepeatSpec],
) -> None:
    """수식이 읽는 입력칸은 금액이다. 자리표시자 없이 비어 있던 칸은 항목명만으로 유형을 몰라
    글(text)로 남는데, 거기에 글을 쓰면 합계를 계산하지 못한다(정산서의 집행 금액 칸)."""
    read: dict[int, set[tuple[int, int]]] = defaultdict(set)
    for f in formulas:
        layout = layouts[f.table]
        try:
            cells = operands(f.formula, layout.table.grid(), f.row, f.col)
        except FormulaError:
            continue
        for r, c in cells:
            if (anchor := layout.cover.get((r, c))) is not None:
                read[f.table].add((anchor.row, anchor.col))
    for s in slots:
        if s.role == "input" and s.input_type == "text" and (s.row, s.col) in read[s.table]:
            s.input_type = "amount"
    for rep in repeats:
        for col in rep.columns:
            rows = range(rep.start_row, rep.end_row + 1)
            if (
                col.role == "input"
                and col.input_type == "text"
                and any((r, col.col) in read[rep.table] for r in rows)
            ):
                col.input_type = "amount"


# ---------------------------------------------------------------- 표 배치
def _norm(text: str) -> str:
    return " ".join(text.split())


class _Layout:
    """표 하나의 칸 배치. 병합을 고려해 (행, 열) 을 덮는 칸을 찾는다."""

    def __init__(self, table: Table) -> None:
        self.table = table
        self.cover: dict[tuple[int, int], Cell] = {}
        self.by_row: dict[int, list[Cell]] = defaultdict(list)
        for c in table.cells:
            self.by_row[c.row].append(c)
            for r in range(c.row, c.row + c.row_span):
                for k in range(c.col, c.col + c.col_span):
                    self.cover[(r, k)] = c
        for cells in self.by_row.values():
            cells.sort(key=lambda c: c.col)

    def row_texts(self, row: int) -> list[str]:
        return [c.text for c in self.by_row.get(row, [])]

    def signature(self, row: int) -> tuple | None:
        """행의 칸 배치. 아래로 병합된 칸이 이 행에서 시작하면 None.

        위에서 내려온 병합 칸(명단 왼쪽의 `참석대상` 같은 묶음 이름)은 그 칸의 위치로 배치에 넣는다.
        같은 칸에 덮인 행끼리만 배치가 같아진다.
        """
        cells = self.by_row.get(row, [])
        if any(c.row_span != 1 for c in cells):
            return None
        above = {
            (c.row, c.col)
            for k in range(self.table.cols)
            if (c := self.cover.get((row, k))) is not None and c.row < row
        }
        return tuple((c.col, c.col_span) for c in cells), tuple(sorted(above))


# ---------------------------------------------------------------- 반복영역
def _is_serial(text: str) -> bool:
    return text.isdigit() and len(text) <= 3


#: 숫자로만 된 칸 (1, 5, 1,700,000) — 항목명이 될 수 없다
_NUMERIC_RE = re.compile(r"^[\d,.\-\s]+$")

#: 연번으로 반복영역을 판정할 때의 최소 행 수
MIN_SERIAL_ROWS = 3


def find_repeats(layout: _Layout) -> list[RepeatSpec]:
    """같은 칸 배치의 행이 이어지고 그중 2행 이상이 비어 있는 곳을 반복영역으로 본다.

    빈 행에는 연번(1, 2, 3 …)이나 모든 행에 같은 값(입금액 5,000)이 미리 적혀 있을 수 있다.
    빈 행 바로 위의 같은 배치 행이 자리표시자를 담고 있으면 예시 행이다(첫 데이터 행으로 넣는다).
    빈 행이 없어도 첫 열이 1, 2, 3 … 으로 이어지면 예시로 다 채워 둔 명단으로 본다
    (물품 구입 요구서).
    """
    table = layout.table
    if table.cols < 2 or table.rows < 3:
        return []
    sigs = [layout.signature(r) for r in range(table.rows)]

    repeats: list[RepeatSpec] = []
    r = 0
    while r < table.rows:
        sig = sigs[r]
        end = r
        while end + 1 < table.rows and sigs[end + 1] == sig:
            end += 1
        if sig is not None and len(sig[0]) >= 2 and end > r:
            repeats.extend(_repeats_in_run(layout, r, end))
        r = end + 1
    return repeats


def _repeats_in_run(layout: _Layout, first: int, last: int) -> list[RepeatSpec]:
    rows = range(first, last + 1)
    texts = {r: [_norm(t) for t in layout.row_texts(r)] for r in rows}
    width = len(texts[first])

    # 미리 채워진 열: 절반 이상 빈 행들에서 모두 같은 값인 열
    mostly_empty = [r for r in rows if sum(not t for t in texts[r]) * 2 >= width]
    constant: set[int] = set()
    if len(mostly_empty) >= 2:
        for i in range(width):
            values = {texts[r][i] for r in mostly_empty}
            if len(values) == 1 and "" not in values:
                constant.add(i)

    def blank(r: int) -> bool:
        cells = texts[r]
        return any(not t for t in cells) and all(
            not t or _is_serial(t) or i in constant for i, t in enumerate(cells)
        )

    def serial(r: int) -> int | None:
        return int(texts[r][0]) if _is_serial(texts[r][0]) else None

    found: list[RepeatSpec] = []
    r = first
    while r <= last:
        # 연번이 1씩 이어지는 행들 (예시 값으로 다 채워져 있어도 된다)
        end = r
        while end + 1 <= last and serial(r) is not None and serial(end + 1) == serial(end) + 1:
            end += 1
        if end - r + 1 >= MIN_SERIAL_ROWS:
            rep = _make_repeat(layout, first, r, end, examples=True)
            if rep is not None:
                found.append(rep)
                r = end + 1
                continue
        if not blank(r):
            r += 1
            continue
        end = r
        while end + 1 <= last and blank(end + 1):
            end += 1
        if end > r:
            rep = _make_repeat(layout, first, r, end)
            if rep is not None:
                found.append(rep)
        r = end + 1
    return found


def _make_repeat(
    layout: _Layout, run_first: int, blank_first: int, end: int, *, examples: bool = False
) -> RepeatSpec | None:
    """blank_first..end 가 데이터 행인 반복영역. examples 면 데이터 행이 예시 값으로 차 있다."""
    table = layout.table
    start = blank_first
    has_example = examples
    # 빈 행 바로 위의 자리표시자 행은 예시 행이다. 예시가 두 줄 이상인 서식도 있다(협약 기관 명단).
    while (
        not examples
        and start - 1 >= run_first
        and any(is_placeholder(t) for t in layout.row_texts(start - 1))
    ):
        start -= 1
        has_example = True

    headers, example_rows = _header_rows(layout, start, layout.by_row[start][0].col)
    if not headers:
        return None

    columns: list[RepeatColumn] = []
    for cell in layout.by_row[start]:
        parts: list[str] = []
        for h in headers:
            head = layout.cover.get((h, cell.col))
            text = _norm(head.text) if head else ""
            if text and text not in parts:
                parts.append(text)
        label = " ".join(parts)
        first_text = _norm(cell.text)
        second = _norm(layout.cover[(start + 1, cell.col)].text) if start < end else ""
        computed = any(layout.cover[(r, cell.col)].has_field for r in range(start, end + 1))
        columns.append(
            RepeatColumn(
                col=cell.col,
                label=label,
                input_type=infer_input_type(first_text or second, label),
                role="computed" if computed else "input",
                prefilled=None if computed else _prefilled(first_text, second),
            )
        )
    if not any(c.label for c in columns):
        return None

    total = end + 1
    total_row = (
        total
        if total < table.rows and any(is_total_text(t) for t in layout.row_texts(total))
        else None
    )
    return RepeatSpec(
        table=table.index,
        header_rows=headers,
        start_row=start,
        end_row=end,
        total_row=total_row,
        has_example=has_example or bool(example_rows),
        example_rows=example_rows,
        columns=columns,
    )


def _prefilled(first: str, second: str) -> str | None:
    """데이터 행마다 미리 적힌 값. 첫 행과 둘째 행이 같거나(입금액 5,000) 연번이면 첫 행 값이다.

    같은지부터 본다. `5,000` 같은 금액도 자리표시자 패턴(00)에 걸리기 때문이다.
    첫 행만 예시(홍길동)면 둘째 행 값을 쓴다. 행마다 다른 예시 값(품목명)은 미리 채움이 아니다.
    """
    if not second:
        return None
    if first == second or (_is_serial(first) and _is_serial(second)):
        return first
    if is_placeholder(second):  # 둘째 행도 예시(홍길서)
        return None
    if not first or is_placeholder(first):
        return second
    return None


def _header_rows(layout: _Layout, start: int, first_col: int) -> tuple[list[int], list[int]]:
    """(헤더 행, 예시 항목 행). 아래로 병합된 칸이 헤더 행까지 내려오면 윗줄도 헤더다.

    데이터 바로 위가 연번이 거꾸로 이어지는 행(5, 4, … 1)이면 모양이 다른 예시 항목이다
    (인건비 입금계좌 확인서: 항목 하나가 세 줄에 병합돼 있다). 그 행들을 건너뛰어 진짜 헤더를
    찾고, 건너뛴 행은 예시 항목 행으로 돌려준다.
    """
    def item_at(row: int) -> tuple[Cell | None, int | None]:
        cell = layout.cover.get((row, first_col))
        text = cell.text.strip() if cell is not None else ""
        return cell, (int(text) if _is_serial(text) else None)

    h = start - 1
    last_cell, last = item_at(start)
    while h >= 0:
        cell, n = item_at(h)
        # 여러 줄에 병합된 항목은 같은 칸이 덮는다. 그 외에는 연번이 줄어들어야 한다
        if n is None or not (cell is last_cell or last is None or n < last):
            break
        last_cell, last = cell, n
        h -= 1
    if h < 0:
        return [], []
    covering = [layout.cover[(h, k)] for k in range(layout.table.cols) if (h, k) in layout.cover]
    if any(is_placeholder(c.text) for c in covering) or not any(c.text for c in covering):
        return [], []
    rows = {h}
    for c in covering:  # 위에서 내려온 병합 칸 (연번·성명 등 두 줄짜리 헤더)
        rows.update(range(c.row, h))
    return sorted(rows), list(range(h + 1, start))


# ---------------------------------------------------------------- 칸
def find_slots(layout: _Layout, repeats: list[RepeatSpec]) -> list[SlotSpec]:
    """반복영역·수식·누름틀 칸을 뺀 나머지에서 채울 칸을 고른다."""
    table = layout.table
    skip_rows: set[int] = set()
    for rep in repeats:
        skip_rows.update(rep.header_rows)
        skip_rows.update(rep.example_rows)
        skip_rows.update(range(rep.start_row, rep.end_row + 1))

    slots: list[SlotSpec] = []
    for cell in table.cells:
        if cell.row in skip_rows or cell.has_field:
            continue
        text = _norm(cell.text)
        if is_placeholder(text):
            origin = "placeholder"
        elif not text:
            origin = "empty"
        else:
            continue
        left, above = _label_cells(layout, cell)
        # 숫자(순번·금액·코드)는 항목명이 아니라 값이다. 왼쪽이 숫자면 위쪽 열 이름을 쓰고,
        # 둘 다 없으면 항목명을 비운다
        left, above = (
            None if c is None or _NUMERIC_RE.match(c.text.strip()) else c for c in (left, above)
        )
        # 합계 칸은 아래로 이름을 물려주지 않는다. 합계 행 아래 빈 줄이 합계 칸으로 잡혔다
        if above is not None and is_total_text(above.text):
            above = None
        label_cell = left or above
        label = _norm(label_cell.text) if label_cell else ""
        if is_total_text(text):  # `합계 : 10,500원 × 00시간` 처럼 칸 자체가 합계다
            label = "합계"
        if origin == "empty" and not label:
            continue  # 항목명도 없는 빈 칸은 입력 지점으로 보기 어렵다
        approval = is_approval_placeholder(text) or any(
            c is not None and _in_approval_row(layout, c) for c in (left, above)
        )
        slots.append(
            SlotSpec(
                table=table.index,
                row=cell.row,
                col=cell.col,
                label=label,
                placeholder=text,
                origin=origin,
                input_type=infer_input_type(text if origin == "placeholder" else "", label),
                role="approval" if approval else "input",
                required=origin == "placeholder" and not approval,
            )
        )
    return slots


def _is_label(cell: Cell | None) -> bool:
    return cell is not None and bool(cell.text.strip()) and not is_placeholder(cell.text)


def _label_cells(layout: _Layout, cell: Cell) -> tuple[Cell | None, Cell | None]:
    """항목명 후보: 같은 행 왼쪽에서 가장 가까운 것, 같은 열 위쪽에서 가장 가까운 것."""
    left = above = None
    for k in range(cell.col - 1, -1, -1):
        c = layout.cover.get((cell.row, k))
        if c is not cell and _is_label(c):
            left = c
            break
    for r in range(cell.row - 1, -1, -1):
        c = layout.cover.get((r, cell.col))
        if c is not cell and _is_label(c):
            above = c
            break
    return left, above


def _in_approval_row(layout: _Layout, label: Cell) -> bool:
    """직위 이름이 같은 행에 둘 이상 있어야 결재란으로 본다 (항목명 '담당' 하나로는 부족하다)."""
    if not is_approval_title(label.text):
        return False
    return sum(is_approval_title(c.text) for c in layout.by_row.get(label.row, [])) >= 2


# ---------------------------------------------------------------- 누름틀·변형
def find_fields(doc: HwpxDocument) -> list[FieldSpec]:
    grouped: dict[str, FieldSpec] = {}
    for f in doc.fields():
        if f.type == "FORMULA":
            continue  # 수식은 formulas 로
        spec = grouped.get(f.name)
        if spec is None:
            grouped[f.name] = FieldSpec(name=f.name, type=f.type, placeholder=_norm(f.value))
        else:
            spec.occurrences += 1
            if not spec.placeholder:
                spec.placeholder = _norm(f.value)
    for spec in grouped.values():
        if is_approval_field(spec.name):
            spec.role = "approval"
        spec.input_type = field_input_type(spec.name, spec.placeholder)
    return list(grouped.values())


def find_variants(doc: HwpxDocument, form_id: str, table_count: int) -> list[Variant]:
    """한 파일에 같은 서식이 여러 번 들어 있으면 변형으로 나눈다.

    결재선이 아닌 첫 누름틀이 n 번 나오면 변형 n 개다. 변형 k 는 그 앞 변형의 마지막 누름틀
    다음에 나오는 첫 표부터 시작한다. 기안문은 본문 표가, 지출품의서는 머리 표가 첫 누름틀보다
    앞선다.
    """
    events = [
        (kind, value)
        for kind, value in doc.outline()
        if kind == "table" or not is_approval_field(str(value))
    ]
    names = [v for k, v in events if k == "field"]
    if not names:
        return []
    first = names[0]
    count = names.count(first)
    if count < 2:
        return []

    starts = [0]
    occurrence = 0
    for i, (kind, value) in enumerate(events):
        if kind != "field" or value != first:
            continue
        occurrence += 1
        if occurrence == 1:
            continue
        last_field = max(j for j in range(i) if events[j][0] == "field")
        table = next((v for k, v in events[last_field:i] if k == "table"), None)
        if table is None or table <= starts[-1]:
            return []  # 경계를 못 찾으면 변형 없이 한 서식으로 둔다
        starts.append(int(table))

    numbers = form_numbers(form_id)
    bounds = list(zip(starts, [s - 1 for s in starts[1:]] + [table_count - 1], strict=True))
    return [
        Variant(
            index=k,
            label=f"{numbers[k]}호" if len(numbers) == count else f"변형 {k + 1}",
            first_table=a,
            last_table=b,
        )
        for k, (a, b) in enumerate(bounds)
    ]


def template_path(path: PurePath, root: PurePath) -> str:
    """저장소 루트 기준 / 구분자 경로."""
    return PurePath(path).relative_to(root).as_posix()


#: 서식이 아니라 서식 묶음을 설명하는 문서라 스키마를 뽑지 않는다.
SKIP_WORDS = ("서식 목차", "서식목차", "서식 신구대조표")


def is_form(template: PurePath) -> bool:
    return not any(w in PurePath(template).stem for w in SKIP_WORDS)


def extract_file(template: Path, settings: Settings) -> FormSchema:
    """templates/ 아래 템플릿 하나의 스키마. 경로에서 사업·카테고리를 읽는다."""
    rel = Path(template).relative_to(settings.templates)
    return extract(
        HwpxDocument.open(template),
        location=parse_location(rel.parent),
        template=template_path(template, settings.root),
    )
