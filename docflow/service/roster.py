"""명단 가져오기 (M10) — 학생이 낸 입금계좌 확인서(HWP/HWPX)에서 명단을 읽어 서식의 명단에 넣는다.

LLM 을 거치지 않는다. 주민등록번호·계좌번호는 한 자리만 틀려도 안 되는 값이고 개인정보라서,
파일의 글자를 그대로 옮긴다 (명세 F2-d).

칸 좌표가 아니라 **머리줄 이름으로 열의 뜻**(성명·은행·계좌·주민등록번호·학번 …)을 찾는다.
머리줄이 두 줄이어도(`교통비 입금계좌` 아래 `은행명 / 계좌번호`) 되고, 학생이 행을 더하거나
지워도 읽힌다. 받는 쪽 명단도 열 이름으로 같은 뜻을 찾아 넣는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from docflow.hwpx.document import HwpxDocument, Table
from docflow.schema.model import RepeatSpec
from docflow.schema.rules import column_concept as concept
from docflow.schema.rules import is_placeholder

#: 화면에 보일 이름
CONCEPT_NAMES = {
    "rrn": "주민등록번호", "student_id": "학번", "bank": "은행", "amount": "금액",
    "account": "계좌번호", "name": "성명", "dept": "학과", "grade": "학년",
    "address": "주소", "phone": "연락처",
}  # fmt: skip
#: 이만큼의 뜻이 머리줄에 있어야 명단 표로 본다
_MIN_CONCEPTS = 2
#: 예시로 적혀 있는 이름 (지운 줄로 본다)
_EXAMPLE_NAMES = {"홍길동", "000", "○○○", "ㅇㅇㅇ"}
_TOTAL_RE = re.compile(r"^\s*(합\s*계|총\s*계|계)\s*$")


@dataclass
class SourceRoster:
    """파일 안의 명단 표 하나."""

    table: int
    #: 뜻 -> 원래 열 이름
    labels: dict[str, str]
    #: 사람마다 뜻 -> 값
    records: list[dict[str, str]] = field(default_factory=list)


def read_rosters(doc: HwpxDocument) -> list[SourceRoster]:
    """문서의 명단 표들. 머리줄에 성명·계좌 같은 뜻이 둘 이상 있는 표만 본다."""
    return [r for t in doc.tables() if (r := _read_table(t)) is not None]


def _covering(table: Table) -> dict[tuple[int, int], int]:
    """(행, 열) -> 그 자리를 덮는 셀 번호 (병합 포함)."""
    cover: dict[tuple[int, int], int] = {}
    for i, c in enumerate(table.cells):
        for r in range(c.row, c.row + c.row_span):
            for k in range(c.col, c.col + c.col_span):
                cover[(r, k)] = i
    return cover


def _row_concepts(table: Table, row: int) -> dict[str, int]:
    """이 행에서 시작하는 셀 가운데 뜻이 있는 것: 뜻 -> 셀 번호."""
    found: dict[str, int] = {}
    for i, c in enumerate(table.cells):
        if c.row == row and (name := concept(c.text)) and name not in found:
            found[name] = i
    return found


def _read_table(table: Table) -> SourceRoster | None:
    cover = _covering(table)
    header = next(
        (r for r in range(table.rows) if len(_row_concepts(table, r)) >= _MIN_CONCEPTS), None
    )
    if header is None:
        return None
    # 머리줄이 두 줄인가: 아래 줄 첫 칸이 비었거나 숫자가 아니고, 아래 줄에도 뜻이 있다
    last = header
    below = header + 1
    if below < table.rows and _row_concepts(table, below):
        first = table.cells[cover[(below, 0)]] if (below, 0) in cover else None
        if first is None or first.row < below or not first.text.strip().isdigit():
            last = below

    # 열마다: 가장 아래 머리줄 셀의 뜻, 없으면 위 줄을 합친 이름의 뜻
    columns: dict[int, str] = {}
    labels: dict[str, str] = {}
    for col in range(table.cols):
        lines = []
        for r in range(header, last + 1):
            i = cover.get((r, col))
            if i is not None and (text := " ".join(table.cells[i].text.split())) not in lines:
                lines.append(text)
        lines = [x for x in lines if x]
        name = next((n for x in reversed(lines) if (n := concept(x))), None)
        anchor = cover.get((last, col))
        if name and anchor is not None and table.cells[anchor].col == col and name not in labels:
            columns[col] = name
            labels[name] = " ".join(lines)

    roster = SourceRoster(table.index, labels)
    for r in range(last + 1, table.rows):
        texts = {
            col: " ".join(table.cells[i].text.split())
            for col in range(table.cols)
            if (i := cover.get((r, col))) is not None and table.cells[i].row == r
        }
        if any(_TOTAL_RE.match(t) for t in texts.values()):
            break
        record = {name: texts.get(col, "") for col, name in columns.items()}
        if not any(v for k, v in record.items() if k not in ("amount",)):
            continue  # 빈 줄 (미리 적힌 금액만 있는 줄 포함)
        who = record.get("name", "")
        if who in _EXAMPLE_NAMES or (who and is_placeholder(who)):
            continue  # 지우지 않은 예시 줄
        roster.records.append(record)
    return roster


# ---------------------------------------------------------------- 받는 쪽 명단에 넣기
@dataclass
class Mapping:
    """받는 쪽 열 하나와 거기 넣을 뜻."""

    col: int
    label: str
    concept: str | None


def target_mapping(rep: RepeatSpec) -> list[Mapping]:
    """받는 명단의 입력 열마다 뜻. 같은 뜻이 두 번이면 앞 열에만 넣는다."""
    seen: set[str] = set()
    out = []
    for c in rep.columns:
        if c.role != "input":
            continue
        name = concept(c.label)
        if name in seen:
            name = None
        if name:
            seen.add(name)
        out.append(Mapping(c.col, c.label, name))
    return out


def to_rows(rep: RepeatSpec, records: list[dict[str, str]]) -> list[dict[str, str]]:
    """사람마다 받는 명단의 한 행 (열 번호 -> 값). 같은 사람(성명·계좌가 같음)은 한 번만."""
    mapping = [m for m in target_mapping(rep) if m.concept]
    rows: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for rec in records:
        key = (rec.get("name", ""), "".join(re.findall(r"\d", rec.get("account", ""))))
        if key != ("", "") and key in seen:
            continue
        seen.add(key)
        row = {str(m.col): rec[m.concept] for m in mapping if rec.get(m.concept)}
        if row:
            rows.append(row)
    return rows
