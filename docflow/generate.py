"""문서 생성 — 서식 스키마와 입력값으로 HWPX 문서를 만든다 (순수 파이썬).

검증(`validate.py`)을 통과한 입력값을 템플릿에 쓴다. 입력값 외에 서식에서 정리하는 것:

* 결재란(role="approval")의 템플릿 잔여값(00/00, ○○○)을 비운다. 결재선이 채울 자리다.
* 반복영역: 입력한 행 수만큼 쓰고 남는 행은 비운다. 미리 적힌 연번·금액도 지운다 —
  남겨 두면 수식 합계(=SUM(ABOVE))가 쓰지 않은 행의 5,000 까지 더한다. 행이 모자라면 늘린다.
  입력한 행에서 값을 주지 않은 칸은 연번이면 번호를, 미리 채워진 값(입금액 5,000)이
  있으면 그 값을 쓴다.
* 예시 항목 행(example_rows)을 비운다.
* 수식을 다시 계산한다. 한글은 열거나 변환할 때 다시 계산하지 않는다.
* 변형이 여러 개 든 서식은 고른 변형만 남긴다.

다 쓴 뒤에 문서에 남은 자리표시자(○○, 00 …)를 모아 알려준다. 스키마가 못 잡은 예시 문구나
값을 주지 않은 선택 칸이 산출물에 그대로 남는 것을 사용자가 확인할 수 있게 하기 위해서다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from docflow.hwpx.document import Block, HwpxDocument, Recalc
from docflow.schema.model import FormSchema, RepeatColumn, RepeatSpec
from docflow.schema.rules import PLACEHOLDER_PATTERNS, is_date_placeholder
from docflow.validate import FormInput

#: 슬롯이 아닌 칸도 좌표(t0.r2.c10)로 쓸 수 있다. 스키마가 못 잡은 예시 값을 덮어쓸 때 쓴다.
_CELL_ID_RE = re.compile(r"^t(\d+)\.r(\d+)\.c(\d+)$")
_SERIAL_LABEL_RE = re.compile(r"연번|순번|번호|No", re.IGNORECASE)
#: 산출물 점검용 자리표시자. 추출 규칙(is_placeholder)과 세 가지가 다르다.
#: 띄어 쓴 `○ ○ ○ (날인)` 과 O 로 채운 날짜 `20OO.OO.OO.` 도 잡고, 0 이 이어진 것은 앞이
#: 숫자에 이어지면 실제 수(`100%`, `2026`)로 본다. `2000.00.00` 은 `.00` 이 걸린다.
_LEFTOVER_PATTERNS = [p for p in PLACEHOLDER_PATTERNS if p.pattern != "0{2,}"] + [
    re.compile(r"(?<!\d)0{2,}"),
    re.compile(r"[○◯〇](?:\s+[○◯〇]){2,}"),
]
_LETTER_O_RE = re.compile(r"[O○]")


def is_leftover(text: str) -> bool:
    """서식의 자리표시자가 남은 글인가."""
    return any(p.search(text) for p in _LEFTOVER_PATTERNS) or (
        is_date_placeholder(text) and bool(_LETTER_O_RE.search(text))
    )


class GenerateError(ValueError):
    """입력값을 문서에 쓸 수 없다 (없는 변형·칸·반복영역 등)."""


@dataclass(frozen=True)
class Leftover:
    """생성한 문서에 남은 자리표시자 하나."""

    #: cell 표 칸 / field 누름틀 / text 표 밖 문단
    kind: Literal["cell", "field", "text"]
    #: 칸 좌표(t0.r2.c10) / 누름틀 이름 / 문단 위치(section0#12)
    id: str
    text: str


@dataclass
class GenerateResult:
    path: Path
    #: 쓴 표 칸 수 (반복영역 포함)
    cells: int = 0
    #: 작성자 값을 채운 누름틀 이름 수 (같은 이름이 여러 번 나와도 하나)
    fields: int = 0
    formulas: Recalc | None = None
    #: replacements 로 바꾼 횟수
    replaced: int = 0
    #: 고른 변형만 남기느라 지운 문단 수
    removed_blocks: int = 0
    leftovers: list[Leftover] = field(default_factory=list)


def generate(
    schema: FormSchema,
    data: FormInput,
    template: Path,
    out: Path,
    *,
    replacements: dict[str, str] | None = None,
) -> GenerateResult:
    """template 에 data 를 써서 out 에 HWPX 로 저장한다.

    replacements 는 입력칸이 아닌 본문 문구를 바꾼다 (`20○○년 ○○월 ○○일` -> 날짜 등).
    스키마가 잡지 못한 서식 문구용이라 검증하지 않는다.
    """
    variant = _check_variant(schema, data)
    doc = HwpxDocument.open(template)
    result = GenerateResult(path=Path(out))

    result.fields = _fill_fields(doc, schema, data)
    result.cells = _fill_cells(doc, schema, data, variant)
    if replacements:
        result.replaced = doc.replace_text(replacements)
    # 반복영역을 늘리거나 변형을 지우기 전에 본다. 좌표가 스키마와 같아 입력값 키로 쓸 수 있다
    result.leftovers = find_leftovers(doc, schema, data, variant, HwpxDocument.open(template))
    for rep in sorted(schema.repeats, key=lambda r: (r.table, -r.start_row)):
        # 행을 늘리면 아래 행 번호가 밀리므로 아래쪽 반복영역부터 쓴다
        if _in_variant(schema, rep.table, variant):
            result.cells += _fill_repeat(doc, rep, data.rows.get(rep.id, []))
    result.formulas = doc.recalculate_formulas()
    if variant is not None:
        # 표 번호가 바뀌므로 표 쓰기를 모두 마친 뒤에 지운다
        result.removed_blocks = doc.remove_blocks(_other_variant_blocks(doc, schema, variant))

    doc.save(out)
    return result


# ---------------------------------------------------------------- 변형
def _check_variant(schema: FormSchema, data: FormInput) -> int | None:
    if schema.variant is not None:
        if data.variant not in (None, schema.variant):
            raise GenerateError(f"이 서식은 변형 {schema.variant} 로 정해져 있다")
        return schema.variant
    if not schema.variants:
        if data.variant is not None:
            raise GenerateError("변형이 없는 서식이다")
        return None
    labels = ", ".join(f"{v.index}={v.label}" for v in schema.variants)
    if data.variant is None:
        raise GenerateError(f"변형을 골라야 한다 ({labels})")
    if not any(v.index == data.variant for v in schema.variants):
        raise GenerateError(f"없는 변형 {data.variant} ({labels})")
    return data.variant


def _in_variant(schema: FormSchema, table: int, variant: int | None) -> bool:
    owner = schema.variant_of(table)
    return variant is None or owner is None or owner == variant


def assign_variants(blocks: list[Block], schema: FormSchema) -> list[int | None]:
    """문단마다 속한 변형. None 은 어느 변형에도 속하지 않는 문단(모든 변형에 남긴다).

    표가 든 문단은 그 표의 변형이다. 표가 없는 문단은 앞뒤로 가장 가까운 표 문단을 본다.
    같은 변형 사이면 그 변형이다. 두 변형 사이면 경계를 정한다:

    1. 뒤 변형의 이름이 든 문단(`[ AID 서식 1-02호 ]`)부터 뒤 변형이다.
    2. 이름이 없으면 앞 변형의 글(`끝.` 등)과 그 뒤 빈 문단까지가 앞 변형이고,
       빈 문단 다음의 첫 글 문단(다음 변형의 제목)부터 뒤 변형이다.
    3. 빈 문단이 없으면 모두 앞 변형이다.
    """
    own: list[int | None] = []
    for b in blocks:
        found = [v for t in b.tables if (v := schema.variant_of(t)) is not None]
        own.append(found[0] if found else None)
    result = list(own)
    anchors = [i for i, v in enumerate(own) if v is not None]
    if not anchors:
        return result

    for i in range(anchors[0]):
        if not blocks[i].tables:
            result[i] = own[anchors[0]]
    for i in range(anchors[-1] + 1, len(blocks)):
        if not blocks[i].tables:
            result[i] = own[anchors[-1]]
    labels = {v.index: v.label for v in schema.variants}
    for before, after in zip(anchors, anchors[1:], strict=False):
        gap = [k for k in range(before + 1, after) if not blocks[k].tables]
        prev, nxt = own[before], own[after]
        split = len(gap)
        if prev != nxt:
            named = [n for n, k in enumerate(gap) if labels[nxt] in blocks[k].text]
            if named:
                split = named[0]
            else:
                blank = [not blocks[k].text.strip() for k in gap]
                split = next(
                    (n for n in range(1, len(gap)) if blank[n - 1] and not blank[n]), len(gap)
                )
        for n, k in enumerate(gap):
            result[k] = prev if n < split else nxt
    return result


def _other_variant_blocks(doc: HwpxDocument, schema: FormSchema, variant: int) -> list[Block]:
    blocks = doc.blocks()
    owners = assign_variants(blocks, schema)
    return [b for b, v in zip(blocks, owners, strict=True) if v is not None and v != variant]


# ---------------------------------------------------------------- 누름틀·칸
def _fill_fields(doc: HwpxDocument, schema: FormSchema, data: FormInput) -> int:
    known = {f.name: f for f in schema.fields}
    unknown = sorted(set(data.fields) - set(known))
    if unknown:
        raise GenerateError(f"서식에 없는 누름틀: {unknown}")
    filled = 0
    for spec in schema.fields:
        if spec.role == "approval":
            doc.fill({spec.name: ""})
        elif spec.name in data.fields:
            value = str(data.fields[spec.name])
            if "\n" in value:
                doc.fill_text(spec.name, value)
            else:
                doc.fill({spec.name: value})
            filled += 1
    return filled


def _fill_cells(doc: HwpxDocument, schema: FormSchema, data: FormInput, variant: int | None) -> int:
    slots = {s.id: s for s in schema.slots}
    written = 0
    for slot in schema.slots:
        if not _in_variant(schema, slot.table, variant):
            continue
        # 결재란은 비운다. 관리자가 입력 칸으로 바꾼 칸(예시 값이 적힌 칸)을 비워 두면 예시가
        # 문서에 남지 않게 지운다
        unwritten_example = (
            slot.role == "input" and slot.origin == "example" and slot.id not in data.cells
        )
        if slot.role == "approval" or unwritten_example:
            doc.set_cell(slot.table, slot.row, slot.col, "")
            written += 1
    for key, value in data.cells.items():
        slot = slots.get(key)
        if slot is not None and slot.role != "input":
            raise GenerateError(f"{key} 는 작성자가 채우는 칸이 아니다 ({slot.role})")
        m = _CELL_ID_RE.match(key)
        if m is None:
            raise GenerateError(f"칸 좌표 형식이 아니다: {key!r} (예: t0.r2.c10)")
        table, row, col = map(int, m.groups())
        if not _in_variant(schema, table, variant):
            raise GenerateError(f"{key} 는 고른 변형에 없는 칸이다")
        doc.set_cell(table, row, col, str(value))
        written += 1
    return written


# ---------------------------------------------------------------- 반복영역
def _is_serial(col: RepeatColumn) -> bool:
    return bool(
        col.prefilled
        and col.prefilled.isdigit()
        and (col.prefilled == "1" or _SERIAL_LABEL_RE.search(col.label))
    )


def _row_value(col: RepeatColumn, row: dict[int, str], index: int) -> str:
    if col.col in row:
        return str(row[col.col])
    if _is_serial(col):
        return str(index + 1)
    return col.prefilled or ""


def _fill_repeat(doc: HwpxDocument, rep: RepeatSpec, rows: list[dict[int, str]]) -> int:
    known = {c.col for c in rep.columns if c.role == "input"}
    for i, row in enumerate(rows):
        extra = sorted(set(row) - known)
        if extra:
            raise GenerateError(f"반복영역 {rep.id} {i + 1}번째 행: 없는 열 {extra}")

    first = rep.columns[0].col
    width = rep.columns[-1].col - first + 1
    lines: list[list[str | None]] = []
    for i in range(max(len(rows), rep.capacity)):
        line: list[str | None] = [None] * width
        for col in rep.columns:
            if col.role != "input":
                continue  # 수식 열은 다시 계산한다
            line[col.col - first] = _row_value(col, rows[i], i) if i < len(rows) else ""
        lines.append(line)

    written = doc.fill_rows(
        rep.table, rep.start_row, lines, first, last_row=rep.end_row, extend=True
    )
    for r in rep.example_rows:
        for cell in doc.tables()[rep.table].cells:
            if cell.row == r and not cell.has_field and cell.text:
                doc.set_cell(rep.table, cell.row, cell.col, "")
                written += 1
    return written


# ---------------------------------------------------------------- 남은 자리표시자
def find_leftovers(
    doc: HwpxDocument,
    schema: FormSchema,
    data: FormInput,
    variant: int | None,
    template: HwpxDocument,
) -> list[Leftover]:
    """쓰지 않은 자리에 남은 서식의 자리표시자. 표 칸·쓰기 전에 부른다.

    작성자가 쓴 값은 보지 않는다. 실제 값(13:00, 5,000)도 자리표시자 패턴(00)에 걸리기 때문이다.
    쓰지 않은 자리는 서식의 글자 그대로이므로 거기서 자리표시자가 보이면 서식의 잔여물이다.
    """
    #: 결재란은 비웠고, 관리자가 뺀 칸(fixed)은 서식 글자를 일부러 남긴다
    written = set(data.cells) | {s.id for s in schema.slots if s.role in ("approval", "fixed")}
    regions = {
        (r.table, row)
        for r in schema.repeats
        for row in [*range(r.start_row, r.end_row + 1), *r.example_rows]
    }
    found: list[Leftover] = []
    for t in doc.tables():
        if not _in_variant(schema, t.index, variant):
            continue
        for c in t.cells:
            key = f"t{t.index}.r{c.row}.c{c.col}"
            if key in written or (t.index, c.row) in regions or c.has_field:
                continue
            if c.text and is_leftover(c.text):
                found.append(Leftover("cell", key, c.text))

    touched = set(data.fields) | {f.name for f in schema.fields if f.role in ("approval", "fixed")}
    seen: set[str] = set()
    for f in doc.fields():
        if f.type == "FORMULA" or f.name in touched or f.name in seen:
            continue
        if f.value and is_leftover(f.value):
            seen.add(f.name)
            found.append(Leftover("field", f.name, f.value))

    # 표 밖 문단은 누름틀로만 쓴다. 서식의 글자 그대로인 문단만 본다(쓴 본문은 바뀌었다)
    original = {b.text for b in template.blocks()}
    blocks = doc.blocks()
    owners = assign_variants(blocks, schema)
    for b, owner in zip(blocks, owners, strict=True):
        if variant is not None and owner is not None and owner != variant:
            continue
        if b.text in original and is_leftover(b.text):
            found.append(Leftover("text", f"{Path(b.section).stem}#{b.index}", b.text))
    return found
