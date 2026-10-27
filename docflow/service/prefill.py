"""자연어 일괄 입력 (M10, F2-c) — 사용자가 적은 글로 LLM 이 서식의 칸을 한 번에 채운다.

1. 고른 서식(변형)의 채울 칸 목록을 만든다. **명단·계좌·주민등록번호는 넘기지 않는다** — 사람 명단은
   입금계좌 확인서로 채우고(`service.roster`), 계좌·주민등록번호 칸은 직접 적는다.
2. 칸 목록으로 답의 JSON 스키마를 만든다. 답의 모양이 입력 JSON(`pipeline.parse_input`)과 같아서
   그대로 입력 화면에 넣을 수 있다.
3. 답에서 없는 칸·빈 값을 버린다. 저장하지 않는다 — 화면이 미리 보여 주고 사용자가 넣는다.
4. **인용 검사**: 채운 금액·날짜가 글에 없으면 `LLM_UNCITED` 경고. LLM 이 지어낸 숫자를 잡는다.

규칙 검증은 바뀌지 않는다. LLM 의 답은 사용자가 친 값과 같은 입력값일 뿐이다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from docflow.llm import LlmClient, LlmRequest
from docflow.pipeline import parse_input
from docflow.schema.labels import field_label
from docflow.schema.model import FormSchema, SlotSpec
from docflow.schema.rules import column_concept, is_approval_title, is_placeholder
from docflow.validate import Issue, Location, find_dates, parse_amount

LLM_UNCITED = "LLM_UNCITED"
#: 글의 최대 길이 (7.8B 의 문맥과 응답 시간을 생각한 값)
MAX_PROMPT = 4000
#: 반복영역 하나에 받는 최대 행 수
MAX_ROWS = 200
#: 이 뜻의 칸·열은 LLM 에 넘기지 않는다 (개인정보)
PRIVATE = {"rrn", "account", "bank"}
#: 이 뜻의 열이 있는 반복영역은 사람 명단이라 통째로 뺀다
ROSTER = {"name", "rrn", "account", "student_id"}

SYSTEM = """\
너는 대학 행정 서식의 칸을 채우는 도우미다. 사용자의 글에서 각 칸에 해당하는 내용을 찾아 옮긴다.
규칙:
1. 글에 적힌 글자·숫자를 그대로 옮긴다. 날짜의 월·일, 금액, 시각은 글과 한 글자도 다르면 안 된다.
2. 글에 없는 내용은 추측하지 말고 "" 로 둔다. 비슷한 단어로 지어내지 않는다
   (예: 건물 이름을 학과 이름으로 쓰지 않는다). 글에 없는 계산식도 만들지 않는다.
3. 금액은 숫자와 쉼표로 적는다 (예: 50,000원). '50만원' 은 500,000원 이다.
4. 날짜는 칸의 예시 모양을 따르되 숫자는 글에서 가져온다. 연도가 없으면 오늘의 연도를 쓴다.
5. 칸의 '예:' 는 서식에 적혀 있던 모양일 뿐 값이 아니다. 예시의 글자·숫자를 옮기지 않는다.
6. 명단(여러 행) 칸은 글에 나온 항목마다 한 행씩 적는다. 항목이 없으면 빈 배열로 둔다.
7. [서식 모양] 의 표에서 {cells.키} 자리가 채울 칸이다. 칸의 뜻은 같은 행의 왼쪽 제목과
   같은 열의 위쪽 제목으로 판단한다. 표에 적힌 다른 글자는 서식의 글자라 옮기지 않는다.
"""

Kind = Literal["field", "cell", "repeat"]


# ---------------------------------------------------------------- 채울 칸
@dataclass(frozen=True)
class Target:
    """LLM 이 채우는 자리 하나. 반복영역은 열 하나다."""

    kind: Kind
    #: 누름틀 이름 / 슬롯 id / 열 번호
    key: str
    label: str
    input_type: str
    example: str = ""
    table: int | None = None
    row: int | None = None
    col: int | None = None
    #: 반복영역 id (kind=repeat)
    repeat: str | None = None

    def describe(self) -> str:
        text = f"{self.label} [{self.input_type}]"
        return f"{text} 예: {self.example}" if self.example else text

    def location(self, index: int | None = None) -> Location:
        if self.kind == "repeat":
            return Location("repeat", self.repeat or "", self.table, (self.row or 0) + (index or 0),
                            self.col, index, self.label)  # fmt: skip
        return Location(self.kind, self.key, self.table, self.row, self.col, label=self.label)


@dataclass
class Plan:
    fields: list[Target] = field(default_factory=list)
    cells: list[Target] = field(default_factory=list)
    #: 반복영역 id -> 열들
    rows: dict[str, list[Target]] = field(default_factory=dict)
    #: 개인정보라 뺀 자리 (화면에 알린다)
    skipped: list[str] = field(default_factory=list)
    #: 같은 이름의 칸 사본: 처음 칸 id -> 나머지. 한 번만 묻고 답을 똑같이 넣는다
    #: (한 파일에 학과용·부서용처럼 같은 서식이 두 벌 든 경우)
    copies: dict[str, list[Target]] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not (self.fields or self.cells or self.rows)


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def _example(text: str) -> str:
    """서식에 적혀 있던 값. 자리표시자(○○○ 회의)도 칸의 뜻을 알려 주므로 넘긴다."""
    return _one_line(text)[:40]


#: 서식에 박힌 값으로 보는 최대 길이. 이보다 길거나 여러 줄이면 본문 예시다
_PRESET_LIMIT = 60


def _preset(placeholder: str) -> bool:
    """서식에 진짜 값이 박힌 칸 (주소·홈페이지·공개 구분). 그대로 두는 칸이라 LLM 에 묻지 않는다.

    물으면 7.8B 가 예시를 그대로 옮겨 적고, 본문 초안에도 끌고 들어간다(실측).
    여러 줄이거나 긴 예시(본문의 `1. 관련: …`)는 쓰는 칸이라 뺀다.
    """
    text = placeholder.strip()
    one_line = "\n" not in text and len(text) <= _PRESET_LIMIT
    return bool(text) and one_line and not is_placeholder(text)


def _bare(text: str) -> str:
    return re.sub(r"[\W_]", "", text)


def _useless(t: Target, value: str, *, example: bool = True) -> bool:
    """LLM 이 글이 아니라 서식에서 베낀 값: 칸 이름, 서식 예시, 자리표시자(○, 0000)."""
    bare = _bare(value)
    digits = re.sub(r"\D", "", value)
    return (
        bare == _bare(t.label)
        or (example and bool(t.example) and bare == _bare(t.example))
        or "○" in value
        or (bool(digits) and set(digits) == {"0"})
    )


#: 칸 이름의 최대 길이 (행 제목 / 열 제목)
_LABEL_LIMIT = 60


def context_label(schema: FormSchema, slot: SlotSpec, inputs: set[tuple[int, int, int]]) -> str:
    """칸의 이름: 행 제목(왼쪽)과 열 제목(위쪽)을 함께 쓴다 — `① 강사료 / 현집행금액`.

    추출은 이름을 한쪽에서만 찾아서, 정산서의 예산·집행·잔액 칸이 모두 `① 강사료` 가 되고
    이름 없는 칸은 좌표만 남는다(서식 전체 표 칸의 26%). 모델은 이름만 보고 칸을 고르므로
    이대로면 엉뚱한 칸에 쓴다(실측). 입력 칸과 자리표시자는 제목으로 보지 않는다.
    """
    grid = schema.tables[slot.table].grid if slot.table < len(schema.tables) else []

    def header(r: int, c: int) -> str:
        if (slot.table, r, c) in inputs or r >= len(grid) or c >= len(grid[r]):
            return ""
        text = _one_line(grid[r][c])
        return text if text and not is_placeholder(text) else ""

    row = _one_line(slot.label) or next(
        (h for c in range(slot.col - 1, -1, -1) if (h := header(slot.row, c))), ""
    )
    # 열 제목: 위로 올라가며 입력 칸은 건너뛰고 처음 만나는 제목. 빈 칸이나 자리표시자를 만나면
    # 멈춘다 — 더 위의 글자는 다른 표 블록의 머리다(회의비용 칸이 명단 머리 `성명` 을 집었다)
    col = ""
    for r in range(slot.row - 1, -1, -1):
        if (slot.table, r, slot.col) in inputs:
            continue
        col = header(r, slot.col)
        break
    parts = [x[:_LABEL_LIMIT] for x in dict.fromkeys((row, col)) if x]
    return " / ".join(parts) or slot.id


def plan(schema: FormSchema, variant: int | None = None) -> Plan:
    """고른 변형에서 LLM 이 채울 자리. 입력 칸만, 개인정보와 서식에 박힌 값은 뺀다."""
    if schema.variant is not None:
        variant = schema.variant

    def mine(table: int) -> bool:
        owner = schema.variant_of(table)
        return variant is None or owner is None or owner == variant

    out = Plan()
    for f in schema.fields:
        if f.role != "input" or _preset(f.placeholder):
            continue
        label = f.label or field_label(f.name)
        if column_concept(label) in PRIVATE:
            out.skipped.append(label)
            continue
        out.fields.append(Target("field", f.name, label, f.input_type, _example(f.placeholder)))
    inputs = {(s.table, s.row, s.col) for s in schema.input_slots()}
    by_table: dict[int, list[Target]] = {}
    for s in schema.input_slots():
        if not mine(s.table) or _preset(s.placeholder):
            continue
        own = _one_line(s.label)
        if is_approval_title("".join(own.split())):
            continue  # 결재란 아래 서명 칸이 입력 칸으로 잡힌 경우. 결재선이 채운다
        if column_concept(own) in PRIVATE:
            out.skipped.append(own)
            continue
        label = context_label(schema, s, inputs)
        example = _example(s.placeholder)
        t = Target("cell", s.id, label, s.input_type, example, s.table, s.row, s.col)
        by_table.setdefault(s.table, []).append(t)
    # 칸 구성(자리·이름·형식)이 통째로 같은 표는 같은 서식의 사본(학과용·부서용)이다. 한 번만 묻고
    # 답을 사본에 똑같이 넣는다. 칸 하나만 이름이 같다고 묶으면 강사료 표와 전문가활용비 표처럼
    # 다른 표의 칸이 묶인다(행 값 `보조` 가 이름이 된 경우, 실측)
    originals: dict[tuple, list[Target]] = {}
    for targets in by_table.values():
        sig = tuple((t.row, t.col, "".join(t.label.split()), t.input_type) for t in targets)
        first = originals.setdefault(sig, targets)
        if first is targets:
            out.cells += targets
        else:
            for orig, copy in zip(first, targets, strict=True):
                out.copies.setdefault(orig.key, []).append(copy)
    for rep in schema.repeats:
        if not mine(rep.table):
            continue
        cols = [c for c in rep.columns if c.role == "input"]
        concepts = {column_concept(c.label) for c in cols}
        if concepts & ROSTER:
            out.skipped.append(f"명단 ({', '.join(_one_line(c.label) for c in cols)})")
            continue
        targets = [
            Target("repeat", str(c.col), _one_line(c.label) or f"{c.col}열", c.input_type,
                   _example(c.prefilled or ""), rep.table, rep.start_row, c.col, rep.id)
            for c in cols
            if column_concept(c.label) not in PRIVATE
        ]  # fmt: skip
        if targets:
            out.rows[rep.id] = targets
    return out


# ---------------------------------------------------------------- 요청
def output_schema(p: Plan) -> dict[str, Any]:
    """답의 JSON 스키마. 모양은 입력 JSON 과 같다: {fields, cells, rows}.

    모든 칸을 필수로 둔다(모르면 ""). 선택으로 두면 모델이 빈 객체 `{}` 로 답해 버린다.
    """

    def obj(props: dict[str, Any]) -> dict[str, Any]:
        return {"type": "object", "properties": props, "required": list(props),
                "additionalProperties": False}  # fmt: skip

    def text(targets: list[Target]) -> dict[str, Any]:
        return obj({t.key: {"type": "string", "description": t.describe()} for t in targets})

    props: dict[str, Any] = {}
    if p.fields:
        props["fields"] = text(p.fields)
    if p.cells:
        props["cells"] = text(p.cells)
    if p.rows:
        props["rows"] = obj({
            rid: {"type": "array", "items": text(cols), "maxItems": MAX_ROWS}
            for rid, cols in p.rows.items()
        })  # fmt: skip
    return obj(props)


#: 표 모양에 넣는 최대 글자 수. 문맥 8192 토큰 안에 글·칸 목록·답이 함께 들어가야 한다
TABLE_BUDGET = 5000
#: 표 모양의 칸 하나에 보이는 최대 글자 수
_CELL_LIMIT = 16


def table_view(schema: FormSchema, p: Plan, budget: int = TABLE_BUDGET) -> str:
    """채울 칸이 든 표를 마크다운 표로. 채울 자리는 `{cells.키}` 로 바꾼다.

    칸 이름만으로는 어느 칸인지 모호한 경우가 많다(목록 모양 표에서 행의 값 `보조` 가 이름이 되는
    등). 표를 보여 주면 모델이 머리글을 직접 보고 칸을 고른다.
    반복영역(명단)은 한 줄로 줄인다. 넘치면 넣을 수 있는 표까지만 넣는다.
    """
    keys = {(t.table, t.row, t.col): t.key for t in p.cells}
    repeats = {(r.table, row): r for r in schema.repeats
               for row in range(r.start_row, r.end_row + 1)}  # fmt: skip
    blocks: list[str] = []
    used = 0
    # 사본 표는 원본 표와 같으니 한 번만 보인다
    for table in sorted({t.table for t in p.cells if t.table is not None}):
        grid = schema.tables[table].grid if table < len(schema.tables) else []
        lines = []
        for r, row in enumerate(grid):
            rep = repeats.get((table, r))
            if rep is not None:
                if r == rep.start_row and rep.id in p.rows:
                    lines.append(f"| (여러 행: rows.{rep.id}) |")
                continue
            cells: list[str] = []
            for c, text in enumerate(row):
                key = keys.get((table, r, c))
                cell = f"{{cells.{key}}}" if key else _one_line(text)[:_CELL_LIMIT]
                if cells and cell and cell == cells[-1]:
                    continue  # 가로로 병합된 칸이 펼쳐져 같은 글자가 이어진다
                cells.append(cell)
            while cells and not cells[-1]:
                cells.pop()
            if any(cells):
                lines.append("| " + " | ".join(cells) + " |")
        block = f"표 {table + 1}:\n" + "\n".join(lines)
        if used + len(block) > budget:
            break
        blocks.append(block)
        used += len(block)
    return "\n\n".join(blocks)


def build_request(schema: FormSchema, p: Plan, text: str, today: date) -> LlmRequest:
    """글을 맨 앞에 둔다. 칸 목록 뒤에 두면 7.8B 가 글의 날짜·금액을 덜 정확히 옮긴다(실측)."""
    lines = ["[사용자의 글]", text.strip(), "", f"[서식] {schema.title}",
             f"[오늘] {today:%Y. %m. %d.}"]  # fmt: skip
    if view := table_view(schema, p):
        lines += ["", "[서식 모양]", view]
    lines += ["", "[채울 칸]"]
    lines += [f"- fields.{t.key}: {t.describe()}" for t in p.fields]
    lines += [f"- cells.{t.key}: {t.describe()}" for t in p.cells]
    for rid, cols in p.rows.items():
        columns = " / ".join(f"{t.key}={t.describe()}" for t in cols)
        lines.append(f"- rows.{rid} (여러 행): {columns}")
    return LlmRequest(SYSTEM, "\n".join(lines), output_schema(p))


# ---------------------------------------------------------------- 답 읽기
@dataclass(frozen=True)
class Filled:
    target: Target
    value: str
    #: 반복영역의 몇 번째 행인가
    index: int | None = None

    @property
    def location(self) -> Location:
        return self.target.location(self.index)


#: 날짜 뒤의 요일: `2026년 10월 30일(월)` `2026. 10. 30.(월요일)`
_WEEKDAY_RE = re.compile(
    r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})\s*일?\.?\s*\(\s*([월화수목금토일])(요일)?\s*\)"
)
_WEEKDAYS = "월화수목금토일"


def fix_weekday(value: str) -> str:
    """요일은 코드가 계산한다. 7.8B 는 날짜를 맞게 옮기고도 요일을 자주 틀린다(실측)."""

    def fix(m: re.Match[str]) -> str:
        try:
            day = _WEEKDAYS[date(int(m[1]), int(m[2]), int(m[3])).weekday()]
        except ValueError:
            return m[0]  # 달력에 없는 날은 DATE_INVALID 가 잡는다
        at = m.start(4) - m.start()
        return m[0][:at] + day + m[0][at + 1 :]

    return _WEEKDAY_RE.sub(fix, value)


def _text(value: Any) -> str:
    if value is None or isinstance(value, (dict, list)):
        return ""
    return fix_weekday(str(value).strip())


def read_answer(p: Plan, data: dict[str, Any]) -> tuple[dict[str, Any], list[Filled]]:
    """답에서 계획에 있는 칸의 값만 꺼낸다 -> (입력 JSON, 채운 자리들). 빈 값·빈 행은 버린다."""
    raw: dict[str, Any] = {"fields": {}, "cells": {}, "rows": {}}
    filled: list[Filled] = []
    for part, targets in (("fields", p.fields), ("cells", p.cells)):
        got = data.get(part)
        got = got if isinstance(got, dict) else {}
        for t in targets:
            if (value := _text(got.get(t.key))) and not _useless(t, value):
                for same in [t, *p.copies.get(t.key, [])]:
                    raw[part][same.key] = value
                    filled.append(Filled(same, value))
    rows = data.get("rows")
    rows = rows if isinstance(rows, dict) else {}
    for rid, cols in p.rows.items():
        lines = rows.get(rid)
        lines = lines if isinstance(lines, list) else []
        out = []
        for line in lines[:MAX_ROWS]:
            line = line if isinstance(line, dict) else {}
            # 행의 미리 적힌 값(기본 금액)은 글에 있어도 되므로 예시와 같은 값은 버리지 않는다
            row = {t.key: v for t in cols if (v := _text(line.get(t.key)))
                   and not _useless(t, v, example=False)}  # fmt: skip
            if row:
                filled += [Filled(t, row[t.key], len(out)) for t in cols if t.key in row]
                out.append(row)
        if out:
            raw["rows"][rid] = out
    return raw, filled


# ---------------------------------------------------------------- 인용 검사
_DIGITS_RE = re.compile(r"\d[\d,]*")
#: 한글 단위 금액의 한 조각: `50만` `1.5억` `3천` `천`
_UNIT_RE = re.compile(r"(\d[\d,]*(?:\.\d+)?)?\s*(억|만|천|백)")
#: 한글 단위 금액 전체: `1억 2천만` `50만` `3천5백`
_KOREAN_AMOUNT_RE = re.compile(r"(?:(?:\d[\d,]*(?:\.\d+)?)?\s*(?:억|만|천|백)\s*)+(?:\d[\d,]*)?")
#: 글 안의 `500,000원`
_WON_RE = re.compile(r"(\d[\d,]*)\s*원")
#: 인원·횟수: `5명` `3건` `2회`
COUNT_RE = re.compile(r"(\d[\d,]*)\s*(?:명|건|회|개|시간|일간)")
#: 글자 낱말 (숫자 없는 두 글자 이상)
_WORD_RE = re.compile(r"[가-힣A-Za-z]{2,}")
#: 이보다 긴 글 칸은 낱말 검사를 하지 않는다 (서술형은 본문 초안의 몫)
_WORD_CHECK_LIMIT = 30
#: 연도 없는 날짜: `10월 12일` `10/12` `10.12`
_MONTH_DAY_RE = re.compile(r"(\d{1,2})\s*(?:월|[./])\s*(\d{1,2})")
_SMALL = {"천": 1000, "백": 100}
_BIG = {"억": 100_000_000, "만": 10_000}


def _number(text: str) -> float:
    return float(text.replace(",", ""))


def korean_amount(text: str) -> int | None:
    """`1억 2천만` -> 120000000. 단위가 없으면 None."""
    total, section, found = 0.0, 0.0, False
    end = 0
    for m in _UNIT_RE.finditer(text):
        found = True
        num, unit = m.group(1), m.group(2)
        if unit in _SMALL:
            section += (_number(num) if num else 1) * _SMALL[unit]
        else:
            section += _number(num) if num else 0
            total += (section or 1) * _BIG[unit]
            section = 0
        end = m.end()
    if not found:
        return None
    tail = _DIGITS_RE.match(text[end:].strip())
    if tail:
        section += _number(tail.group())
    return round(total + section)


def _amounts(text: str) -> set[int]:
    """글에 나온 금액·숫자. 단위 없는 숫자도 모두 넣는다 (`500000` 과 `500,000` 은 같다)."""
    found = {int(m.group().replace(",", "")) for m in _DIGITS_RE.finditer(text)
             if m.group().replace(",", "")}  # fmt: skip
    for m in _KOREAN_AMOUNT_RE.finditer(text):
        if (n := korean_amount(m.group())) is not None:
            found.add(n)
    return found


@dataclass
class Source:
    """사용자 글에서 뽑은 숫자·날짜. 채운 값을 이것과 견준다."""

    amounts: set[int]
    dates: set[date]
    #: (월, 일)
    month_days: set[tuple[int, int]]

    @classmethod
    def of(cls, text: str) -> Source:
        dates = {d for d in find_dates(text) if d is not None}
        month_days = {(int(m), int(d)) for m, d in _MONTH_DAY_RE.findall(text)}
        month_days |= {(d.month, d.day) for d in dates}
        return cls(_amounts(text), dates, month_days)

    def has_date(self, d: date, today: date) -> bool:
        if d in self.dates:
            return True
        year_ok = d.year == today.year or d.year in self.amounts
        return (d.month, d.day) in self.month_days and year_ok


def money_in(text: str) -> set[int]:
    """글 속의 금액: `500,000원`, `50만원`. 단위 없는 숫자(목록 번호 등)는 보지 않는다."""
    found = {int(m.replace(",", "")) for m in _WON_RE.findall(text) if m.replace(",", "")}
    for m in _KOREAN_AMOUNT_RE.finditer(text):
        if (n := korean_amount(m.group())) is not None and re.search(r"\d", m.group()):
            found.add(n)
    return found


def _value_amounts(t: Target, value: str) -> set[int]:
    """값에 담긴, 글에 있어야 할 금액·숫자. 날짜 숫자는 날짜로 따로 본다."""
    found: set[int] = set()
    if t.input_type in ("amount", "number") and not find_dates(value):
        n = parse_amount(value)
        if n is not None:
            found.add(n)
    return found | money_in(value)


def counts_in(text: str) -> set[int]:
    return {int(n.replace(",", "")) for n in COUNT_RE.findall(text) if n.replace(",", "")}


def _strange_words(text: str, t: Target, value: str) -> list[str]:
    """짧은 글 칸에서 글에 없는 낱말 (`공학관` 을 보고 지어낸 `공학과`)."""
    if t.input_type != "text" or re.search(r"\d", value) or len(value) > _WORD_CHECK_LIMIT:
        return []
    flat = "".join(text.split())
    if "".join(value.split()) in flat:
        return []
    return [f"'{w}'" for w in dict.fromkeys(_WORD_RE.findall(value)) if w not in flat]


def uncited(text: str, filled: list[Filled], today: date) -> list[Issue]:
    """채운 금액·인원·날짜·낱말 가운데 글에 없는 것. 모두 경고다."""
    src = Source.of(text)
    issues = []
    for f in filled:
        amounts = sorted(_value_amounts(f.target, f.value) | counts_in(f.value))
        missing = _strange_words(text, f.target, f.value)
        missing += [f"{n:,}" for n in amounts if n not in src.amounts]
        missing += [f"{d:%Y. %m. %d.}" for d in find_dates(f.value)
                    if d is not None and not src.has_date(d, today)]  # fmt: skip
        if missing:
            issues.append(Issue(
                LLM_UNCITED, "warning", f.location,
                f"LLM 이 채운 {', '.join(missing)} 이(가) 입력한 글에 없습니다",
                "글에 적은 내용과 맞는지 확인하고 고치세요",
            ))  # fmt: skip
    return issues


# ---------------------------------------------------------------- 한 번에
@dataclass
class Prefill:
    #: 입력 JSON 모양 {fields, cells, rows}. 저장하지 않은 값이다
    input: dict[str, Any]
    filled: list[Filled]
    issues: list[Issue]
    skipped: list[str]
    model: str = ""
    elapsed: float = 0.0


def prefill(
    schema: FormSchema,
    text: str,
    client: LlmClient,
    *,
    variant: Any = None,
    today: date | None = None,
) -> Prefill:
    """글로 서식의 칸을 채운다. LLM 을 못 쓰면 `LlmError`."""
    text = text.strip()
    if not text:
        raise ValueError("채울 내용을 적으세요")
    if len(text) > MAX_PROMPT:
        raise ValueError(f"글은 {MAX_PROMPT:,}자까지 적습니다 (지금 {len(text):,}자)")
    today = today or date.today()
    index = parse_input(schema, {"variant": variant} if variant is not None else {}).data.variant
    p = plan(schema, index)
    if p.empty:
        raise ValueError(
            "이 서식에는 LLM 으로 채울 칸이 없습니다 (명단은 입금계좌 확인서로 채웁니다)"
        )
    result = client.complete(build_request(schema, p, text, today))
    raw, filled = read_answer(p, result.data)
    issues = uncited(text, filled, today)
    return Prefill(raw, filled, issues, p.skipped, result.model, result.elapsed)
