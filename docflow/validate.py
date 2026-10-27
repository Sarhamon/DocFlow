"""규칙 기반 검증 — 입력값이 서식 규칙을 지키는지 확인한다.

결재 문서라 같은 입력에 같은 판정이 나와야 하므로 재현 가능한 규칙만 여기서 다룬다.
지침 해석이 필요한 정성 검증은 LLM 몫이다 (README 3.2).

규칙은 서식 스키마(v2)에서 유도한다. 자리표시자가 있던 칸은 필수, 입력 유형이 날짜인 칸은
날짜 규칙, 합계 칸은 금액 합계 규칙의 대상이다.

| 코드 | 무엇을 보나 |
|---|---|
| REQUIRED_EMPTY | 필수 칸·누름틀이 비었는가 |
| AMOUNT_TOTAL_MISMATCH | 금액 항목의 합이 합계 칸과 같은가 |
| DATE_INVALID | 날짜 칸의 값이 달력에 있는 날짜인가 |
| DATE_ORDER | 칸 안의 기간이 거꾸로가 아닌가 |
| AMOUNT_INVALID | 금액 칸의 값이 금액으로 읽히는가 (경고) |
| ROW_DUPLICATE | 명단의 학번이 겹치지 않는가 (경고) |
| ACCOUNT_INVALID | 계좌번호가 숫자·하이픈 10~14자리인가 (경고) |
| RRN_INVALID | 주민등록번호가 생년월일-성별자리-6자리 모양인가 (경고) |
| HOLDER_MISMATCH | 명단의 예금주와 성명이 같은가 (경고) |

호출 방식은 두 가지이고 같은 규칙을 쓴다.

* 칸 단위 (입력 중 실시간 점검): check_cell / check_field / check_repeat_cell
* 문서 전체 (입력 완료 검증): validate — 칸 단위 규칙 + 여러 칸을 함께 보는 합계 규칙
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Literal

from docflow.schema.model import FieldSpec, FormSchema, RepeatColumn, RepeatSpec, SlotSpec
from docflow.schema.rules import column_concept, is_placeholder, is_total_text

REQUIRED_EMPTY = "REQUIRED_EMPTY"
AMOUNT_TOTAL_MISMATCH = "AMOUNT_TOTAL_MISMATCH"
DATE_INVALID = "DATE_INVALID"
DATE_ORDER = "DATE_ORDER"
AMOUNT_INVALID = "AMOUNT_INVALID"
ROW_DUPLICATE = "ROW_DUPLICATE"
ACCOUNT_INVALID = "ACCOUNT_INVALID"
RRN_INVALID = "RRN_INVALID"
HOLDER_MISMATCH = "HOLDER_MISMATCH"
RULES = (
    REQUIRED_EMPTY, AMOUNT_TOTAL_MISMATCH, DATE_INVALID, DATE_ORDER, AMOUNT_INVALID, ROW_DUPLICATE,
    ACCOUNT_INVALID, RRN_INVALID, HOLDER_MISMATCH,
)  # fmt: skip
#: 관리자 화면의 규칙 이름과 설명 (AD_15)
RULE_INFO: dict[str, tuple[str, str]] = {
    REQUIRED_EMPTY: ("필수 누락", "자리표시자가 있던 칸·필수 누름틀이 비었는가"),
    AMOUNT_TOTAL_MISMATCH: ("금액 합계", "작성자가 적는 합계 칸이 항목들의 합과 같은가"),
    DATE_INVALID: ("날짜 형식", "날짜 칸의 값이 달력에 있는 날짜인가"),
    DATE_ORDER: ("날짜 순서", "칸 안의 기간이 거꾸로가 아닌가"),
    AMOUNT_INVALID: ("금액 형식", "금액 칸의 값이 금액으로 읽히는가 (경고)"),
    ROW_DUPLICATE: ("학번 중복", "명단에 같은 학번이 두 번 나오는가 (경고)"),
    ACCOUNT_INVALID: ("계좌번호 형식", "계좌번호가 숫자·하이픈 10~14자리인가 (경고)"),
    RRN_INVALID: ("주민등록번호 형식", "생년월일 6자리-성별 자리-6자리 모양인가 (경고)"),
    HOLDER_MISMATCH: ("예금주·성명", "명단에서 예금주와 성명이 같은가 (경고)"),
}
#: 설정이 따로 주지 않을 때의 심각도. 형식·중복은 입력을 막지 않는 경고다 (명세 F3)
DEFAULT_SEVERITY: dict[str, Severity] = {
    code: "warning"
    for code in (AMOUNT_INVALID, ROW_DUPLICATE, ACCOUNT_INVALID, RRN_INVALID, HOLDER_MISMATCH)
}
#: 중복을 보는 명단 열 (학번). 같은 이름은 동명이인이 있어 보지 않는다
_DUPLICATE_COLUMN_RE = re.compile(r"학번")

Severity = Literal["error", "warning"]


# ---------------------------------------------------------------- 결과 모델
@dataclass(frozen=True)
class Location:
    """오류가 난 자리. 화면이 이 정보로 해당 입력칸으로 이동한다."""

    #: cell 표 칸 / field 누름틀 / repeat 반복영역의 한 칸 / document 문서 전체 (정성 판정)
    kind: Literal["cell", "field", "repeat", "document"]
    #: 슬롯 id(t0.r1.c2) / 누름틀 이름 / 반복영역 id(t0.r7-16)
    id: str
    table: int | None = None
    row: int | None = None
    col: int | None = None
    #: 반복영역에서 몇 번째 입력 행인가 (0부터)
    index: int | None = None
    label: str = ""

    @property
    def key(self) -> str:
        """설정의 ignore 목록과 입력값의 키에 쓰는 문자열."""
        if self.kind == "repeat":
            return f"{self.id}[{self.index}].c{self.col}"
        return self.id


@dataclass(frozen=True)
class Issue:
    """검증에 걸린 한 건."""

    code: str
    severity: Severity
    location: Location
    #: 무엇이 틀렸나
    message: str
    #: 어떻게 고치나
    action: str = ""

    @property
    def where(self) -> str:
        return self.location.key


# ---------------------------------------------------------------- 입력·설정
@dataclass
class FormInput:
    """사용자가 입력한 값. 키는 스키마의 id 를 그대로 쓴다."""

    #: 슬롯 id(t0.r1.c2) -> 값
    cells: dict[str, str] = field(default_factory=dict)
    #: 누름틀 이름 -> 값 (HwpxDocument.fill 과 같은 형태)
    fields: dict[str, str] = field(default_factory=dict)
    #: 반복영역 id -> 입력한 행들. 행은 열 번호 -> 값
    rows: dict[str, list[dict[int, str]]] = field(default_factory=dict)
    #: 변형이 있는 서식에서 고른 변형 번호. None 이면 전체를 본다
    variant: int | None = None


@dataclass(frozen=True)
class Exclusions:
    """오탐을 줄이려고 일부러 검사하지 않는 대상. 결재 문서라 틀린 경고가 나오면 아무도 안 본다."""

    #: 결재란 칸·결재선 누름틀(apbN_*) — 작성자가 아니라 결재선이 채운다
    approval: bool = True
    #: 수식 칸 — 우리가 계산해서 쓴다
    computed: bool = True
    #: 항목명만 보고 잡은 빈 칸 — 첨부 확인란처럼 안 채우는 게 정상인 칸이 섞여 있다
    optional_empty: bool = True
    #: 반복영역에서 입력하지 않은 행 — 명단은 수용 행을 다 채우지 않는 게 정상이다
    unused_rows: bool = True


@dataclass(frozen=True)
class RuleConfig:
    """규칙 설정. 관리자 페이지의 규칙 관리(on/off·예외)가 이 값을 만든다."""

    disabled: frozenset[str] = frozenset()
    #: 검사하지 않을 자리 (Location.key)
    ignore: frozenset[str] = frozenset()
    severity: dict[str, Severity] = field(default_factory=dict)
    exclusions: Exclusions = Exclusions()

    def merged(self, other: RuleConfig) -> RuleConfig:
        """other(서식별 설정)를 덧씌운다. 끄는 규칙·무시하는 자리는 합친다."""
        return RuleConfig(
            disabled=self.disabled | other.disabled,
            ignore=self.ignore | other.ignore,
            severity={**self.severity, **other.severity},
            exclusions=other.exclusions,
        )


@dataclass(frozen=True)
class ValidationConfig:
    default: RuleConfig = RuleConfig()
    #: 서식별 예외. 키는 스키마의 template 경로
    forms: dict[str, RuleConfig] = field(default_factory=dict)

    def for_form(self, schema: FormSchema) -> RuleConfig:
        own = self.forms.get(schema.template)
        return self.default.merged(own) if own else self.default


DEFAULT_CONFIG = ValidationConfig()


# ---------------------------------------------------------------- 값 읽기
#: "5,000", "5,000원", "₩ 5,000" 에서 숫자 부분만 뽑는다.
_AMOUNT_RE = re.compile(r"-?\d[\d,]*")
#: 합계에 더할 항목 값. 칸 전체가 금액이어야 한다 — 날짜(2026.10.12)·문장 속 숫자를 더하지 않는다.
_ITEM_AMOUNT_RE = re.compile(r"^-?\s*(?:\d{1,3}(?:,\d{3})+|\d+)\s*원?$")
#: 금액 항목이 될 수 없는 입력 유형 (연번·수량·날짜)
_NOT_AMOUNT = {"number", "date", "checkbox"}
#: 금액 칸에 받는 값: `5,000` `5,000원` `₩ 5,000` `\ 650,000` `650,000원정`
_AMOUNT_TEXT_RE = re.compile(r"^[-₩\\\s]*\d[\d,]*\s*(?:원정?)?$")
#: 금액이 없다는 뜻으로 적는 말. 금액 칸에 있어도 경고하지 않는다
_NO_AMOUNT_RE = re.compile(r"^(?:해당\s*없음|없음|-|—)$")
#: 입력값에 쓰인 날짜. 자리표시자와 달리 실제 숫자만 받는다.
_DATE_RE = re.compile(r"(\d{4})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")


def parse_amount(text: str) -> int | None:
    """금액 문자열을 정수로. 금액으로 읽히지 않으면 None."""
    m = _AMOUNT_RE.search(str(text).replace(" ", ""))
    if not m:
        return None
    return int(m.group().replace(",", ""))


def item_amount(text: str) -> int | None:
    """합계에 더할 항목 값. 칸 전체가 금액(`5,000`, `5,000원`)일 때만 읽는다."""
    text = _one_line(text)
    return parse_amount(text) if _ITEM_AMOUNT_RE.match(text) else None


def find_dates(text: str) -> list[date | None]:
    """값에 담긴 날짜를 등장 순서대로 반환한다. 달력에 없는 날(2026.02.30)은 None 이다."""
    found: list[date | None] = []
    for year, month, day in _DATE_RE.findall(str(text)):
        try:
            found.append(date(int(year), int(month), int(day)))
        except ValueError:
            found.append(None)
    return found


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


# ---------------------------------------------------------------- 검사 대상
@dataclass(frozen=True)
class _Target:
    """값 하나를 받는 자리와 그 자리에 걸린 규칙 정보."""

    location: Location
    input_type: str
    required: bool
    placeholder: str = ""

    @property
    def name(self) -> str:
        return self.location.label or self.location.key


def _in_variant(schema: FormSchema, table: int, variant: int | None) -> bool:
    if variant is None:
        return True
    owner = schema.variant_of(table)
    return owner is None or owner == variant


def field_required(spec: FieldSpec) -> bool:
    """누름틀이 필수인가. 관리자가 정했으면 그대로, 아니면 자리표시자가 있으면 필수다.

    주소·공개여부처럼 서식에 이미 값이 박혀 있는 누름틀은 채울 곳이 아니다.
    """
    if spec.role != "input":
        return False
    return spec.required if spec.required is not None else is_placeholder(spec.placeholder)


def _slot_target(slot: SlotSpec, ex: Exclusions) -> _Target | None:
    if slot.role == "fixed":
        return None  # 관리자가 입력폼에서 뺀 칸
    if (ex.approval and slot.role == "approval") or (ex.computed and slot.role == "computed"):
        return None
    required = slot.required or (not ex.optional_empty and slot.role == "input")
    return _Target(
        Location("cell", slot.id, slot.table, slot.row, slot.col, label=_one_line(slot.label)),
        slot.input_type,
        required,
        _one_line(slot.placeholder),
    )


def _field_target(spec: FieldSpec, ex: Exclusions) -> _Target | None:
    if spec.role == "fixed" or (ex.approval and spec.role == "approval"):
        return None
    return _Target(
        Location("field", spec.name, label=spec.label or spec.name),
        spec.input_type,
        field_required(spec),
        _one_line(spec.placeholder),
    )


def _repeat_target(
    rep: RepeatSpec, col: RepeatColumn, index: int, ex: Exclusions
) -> _Target | None:
    if ex.computed and col.role == "computed":
        return None
    return _Target(
        Location("repeat", rep.id, rep.table, rep.start_row + index, col.col, index, col.label),
        col.input_type,
        required=False,  # 행 안의 칸은 행마다 무엇을 쓰는지 달라 필수로 보지 않는다
    )


def _targets(
    schema: FormSchema, data: FormInput, ex: Exclusions
) -> Iterator[tuple[_Target, str]]:
    """문서 전체의 (검사 대상, 입력값). 누름틀 -> 표 칸 -> 반복영역 순서."""
    for spec in schema.fields:
        if (t := _field_target(spec, ex)) is not None:
            yield t, data.fields.get(spec.name, "")
    for slot in schema.slots:
        if _in_variant(schema, slot.table, data.variant) and (t := _slot_target(slot, ex)):
            yield t, data.cells.get(slot.id, "")
    for rep in schema.repeats:
        if not _in_variant(schema, rep.table, data.variant):
            continue
        rows = data.rows.get(rep.id, [])
        count = len(rows) if ex.unused_rows else max(len(rows), rep.capacity)
        for i in range(count):
            row = rows[i] if i < len(rows) else {}
            for col in rep.columns:
                if (t := _repeat_target(rep, col, i, ex)) is not None:
                    yield t, row.get(col.col, "")


# ---------------------------------------------------------------- 칸 하나 규칙
def _required(t: _Target, value: str) -> list[tuple[str, str, str]]:
    if not t.required or str(value).strip():
        return []
    message = f"'{t.name}' 칸이 비어 있습니다."
    if t.placeholder:
        message += f" (서식 예시: {t.placeholder})"
    return [(REQUIRED_EMPTY, message, "값을 입력하세요.")]


def _date(t: _Target, value: str) -> list[tuple[str, str, str]]:
    """날짜 칸이 달력에 있는 날짜인지, 기간이 거꾸로가 아닌지.

    기간은 `2000.00.00 ~ 2000.00.00` 처럼 칸 하나에 들어 있다. 그런데
    `2024.5.2.(목) 13:00~17:00` 의 물결은 시각을 나누는 것이라, 물결로 쪼개지 않고
    값에서 날짜만 뽑아 순서를 본다.
    """
    raw = _one_line(value)
    if t.input_type != "date" or not raw:
        return []  # 빈 칸은 '필수 누락' 규칙이 볼 몫이다
    found = find_dates(raw)
    fix = "2026.10.12 처럼 연.월.일 순서의 실제 날짜로 입력하세요."
    if not found:
        return [(DATE_INVALID, f"'{t.name}' 을(를) 날짜로 읽을 수 없습니다: {raw}", fix)]
    if None in found:
        return [(DATE_INVALID, f"'{t.name}' 에 달력에 없는 날짜가 있습니다: {raw}", fix)]
    dates = [d for d in found if d is not None]
    for start, end in zip(dates, dates[1:], strict=False):  # 인접한 두 날짜씩
        if start > end:
            return [
                (
                    DATE_ORDER,
                    f"'{t.name}' 의 기간이 거꾸로입니다: "
                    f"{start:%Y.%m.%d} 이 {end:%Y.%m.%d} 보다 뒤입니다.",
                    "시작일이 종료일보다 앞서도록 고치세요.",
                )
            ]
    return []


def _amount(t: _Target, value: str) -> list[tuple[str, str, str]]:
    """금액 칸에 글자가 들어갔는가 (IN_15). 그대로 두면 합계를 낼 수 없다."""
    raw = _one_line(value)
    if t.input_type != "amount" or not raw:
        return []
    if _AMOUNT_TEXT_RE.match(raw) or _NO_AMOUNT_RE.match(raw):
        return []
    return [
        (
            AMOUNT_INVALID,
            f"'{t.name}' 을(를) 금액으로 읽을 수 없습니다: {raw}",
            "5,000 처럼 숫자로 입력하세요 (쉼표·'원'은 써도 됩니다).",
        )
    ]


#: 계좌번호: 숫자와 하이픈(띄어 쓴 것 포함)만. 숫자는 9~16자리로 넉넉히 본다. 은행별 자릿수는
#: 보지 않는다 (은행마다 계좌 종류가 여러 개라 표를 두면 오탐이 난다). 짧게 끊긴 번호·글자를 잡는다
_ACCOUNT_RE = re.compile(r"^[\d\s-]+$")
_ACCOUNT_DIGITS = range(9, 17)
#: 주민등록번호: 생년월일 6자리 - 성별 자리(1~8) 6자리. 뒷자리를 가린 것(`1******`)도 받는다.
#: 검증 숫자는 보지 않는다 (2020년 10월부터 새 번호는 그 규칙을 따르지 않는다)
_RRN_RE = re.compile(r"^(\d{2})(\d{2})(\d{2})\s*-?\s*[1-8][\d*]{6}$")


def _identity(t: _Target, value: str) -> list[tuple[str, str, str]]:
    """계좌번호·주민등록번호 칸의 모양. 송금 사고를 막으려고 본다 (경고)."""
    raw = _one_line(value)
    kind = _identity_kind(t)
    if not raw or kind is None:
        return []
    if kind == "account":
        digits = re.sub(r"\D", "", raw)
        if _ACCOUNT_RE.match(raw) and len(digits) in _ACCOUNT_DIGITS:
            return []
        return [(ACCOUNT_INVALID, f"'{t.name}' 이(가) 계좌번호 모양이 아닙니다: {raw}",
                 "숫자와 하이픈만으로 적었는지 통장 사본과 대조하세요.")]  # fmt: skip
    m = _RRN_RE.match(raw)
    if m and _real_birthday(*m.groups()):
        return []
    return [(RRN_INVALID, f"'{t.name}' 이(가) 주민등록번호 모양이 아닙니다.",
             "생년월일 6자리-뒤 7자리(예: 900101-1234567)로 적었는지 확인하세요.")]  # fmt: skip


def _identity_kind(t: _Target) -> str | None:
    """계좌번호(account)·주민등록번호(rrn) 칸인가. 외국인등록번호·국적도 받는 칸은 보지 않는다."""
    kind = column_concept(t.name)
    if t.input_type != "text" or kind not in ("account", "rrn"):
        return None
    if kind == "rrn" and "외국인" in t.name:
        return None
    return kind


def _real_birthday(yy: str, mm: str, dd: str) -> bool:
    """달력에 있는 날인가. 세기를 몰라 윤년은 2000년대로 본다 (2월 29일을 받는다)."""
    try:
        date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return False
    return True


_CELL_RULES = (_required, _date, _amount, _identity)


def _severity(config: RuleConfig, code: str) -> Severity:
    return config.severity.get(code, DEFAULT_SEVERITY.get(code, "error"))


def _issues(t: _Target, value: str, config: RuleConfig) -> list[Issue]:
    if t.location.key in config.ignore:
        return []
    return [
        Issue(code, _severity(config, code), t.location, message, action)
        for rule in _CELL_RULES
        for code, message, action in rule(t, value)
        if code not in config.disabled
    ]


# ---------------------------------------------------------------- 칸 단위 호출
def check_cell(
    schema: FormSchema, slot_id: str, value: str, config: ValidationConfig = DEFAULT_CONFIG
) -> list[Issue]:
    """표 칸 하나를 입력하는 즉시 점검한다. 없는 슬롯이면 KeyError."""
    rules = config.for_form(schema)
    slot = next((s for s in schema.slots if s.id == slot_id), None)
    if slot is None:
        raise KeyError(f"슬롯이 없다: {slot_id}")
    t = _slot_target(slot, rules.exclusions)
    return _issues(t, value, rules) if t else []


def check_field(
    schema: FormSchema, name: str, value: str, config: ValidationConfig = DEFAULT_CONFIG
) -> list[Issue]:
    """누름틀 하나를 점검한다. 없는 누름틀이면 KeyError."""
    rules = config.for_form(schema)
    spec = next((f for f in schema.fields if f.name == name), None)
    if spec is None:
        raise KeyError(f"누름틀이 없다: {name}")
    t = _field_target(spec, rules.exclusions)
    return _issues(t, value, rules) if t else []


def check_repeat_cell(
    schema: FormSchema,
    repeat_id: str,
    index: int,
    col: int,
    value: str,
    config: ValidationConfig = DEFAULT_CONFIG,
) -> list[Issue]:
    """반복영역의 index 번째 행, col 열 칸 하나를 점검한다. 없는 영역·열이면 KeyError."""
    rules = config.for_form(schema)
    rep = next((r for r in schema.repeats if r.id == repeat_id), None)
    column = next((c for c in rep.columns if c.col == col), None) if rep else None
    if rep is None or column is None:
        raise KeyError(f"반복영역 칸이 없다: {repeat_id} c{col}")
    t = _repeat_target(rep, column, index, rules.exclusions)
    return _issues(t, value, rules) if t else []


# ---------------------------------------------------------------- 합계 규칙
def _total_issue(
    location: Location, declared: int, items: list[int], config: RuleConfig
) -> list[Issue]:
    actual = sum(items)
    if actual == declared or AMOUNT_TOTAL_MISMATCH in config.disabled:
        return []
    if location.key in config.ignore:
        return []
    return [
        Issue(
            AMOUNT_TOTAL_MISMATCH,
            _severity(config, AMOUNT_TOTAL_MISMATCH),
            location,
            f"{len(items)}개 항목의 합은 {actual:,}원인데 합계에 {declared:,}원이 적혀 있습니다.",
            f"합계를 {actual:,}원으로 고치거나 항목 금액을 확인하세요.",
        )
    ]


def check_amount_totals(
    schema: FormSchema, data: FormInput, config: RuleConfig | None = None
) -> list[Issue]:
    """금액 항목의 합이 작성자가 적은 합계 칸과 맞는지 확인한다.

    합계 칸은 항목명이 합계(합계·소계·총계)인 입력 칸이다. 수식 합계는 우리가 계산해서
    쓰므로 틀릴 수 없어 보지 않는다.

    * 반복영역의 합계 행이면 입력한 행들의 같은 열을 더한다.
    * 그 밖(항목이 행마다 고정된 정산표 등)이면 같은 열에서 직전 합계 다음 칸부터 더한다.
      소계와 합계가 같이 있는 표도 이 방식이면 구간이 맞는다.

    더하는 항목은 입력 유형이 연번·수량·날짜가 아니고 값 전체가 금액인 칸뿐이다.
    `합계 : 10,500원 × 00시간` 이 연번 열에 있거나 합계 위에 날짜 칸이 있는 서식이 있다.

    안 채운 칸은 '필수 누락' 규칙이 볼 몫이라, 합계가 비었거나 더할 항목이 없으면 넘어간다.
    """
    config = config or RuleConfig()
    total_rows = {(r.table, r.total_row): r for r in schema.repeats if r.total_row is not None}
    inputs = [
        s
        for s in schema.slots
        if s.role == "input" and _in_variant(schema, s.table, data.variant)
    ]
    by_col: dict[tuple[int, int], list[SlotSpec]] = {}
    for s in inputs:
        by_col.setdefault((s.table, s.col), []).append(s)

    issues: list[Issue] = []
    for (table, col), column in sorted(by_col.items()):
        lower = -1
        for s in sorted(column, key=lambda x: x.row):
            if not is_total_text(s.label):
                continue
            above, lower = [x for x in column if lower < x.row < s.row], s.row
            declared = parse_amount(data.cells.get(s.id, ""))
            if declared is None:
                continue
            rep = total_rows.get((table, s.row))
            if rep is not None:
                column = next((c for c in rep.columns if c.col == col), None)
                ok = column is not None and column.input_type not in _NOT_AMOUNT
                values = [row.get(col, "") for row in data.rows.get(rep.id, [])] if ok else []
            else:
                values = [
                    data.cells.get(x.id, "") for x in above if x.input_type not in _NOT_AMOUNT
                ]
            items = [a for v in values if (a := item_amount(v)) is not None]
            if items:
                where = Location("cell", s.id, s.table, s.row, s.col, label=_one_line(s.label))
                issues += _total_issue(where, declared, items, config)
    return issues


# ---------------------------------------------------------------- 문서 전체
def validate(
    schema: FormSchema, data: FormInput, config: ValidationConfig = DEFAULT_CONFIG
) -> list[Issue]:
    """서식 스키마와 입력값을 받아 위반 사항을 모아 반환한다 (입력 완료 검증)."""
    rules = config.for_form(schema)
    issues: list[Issue] = []
    for target, value in _targets(schema, data, rules.exclusions):
        issues += _issues(target, value, rules)
    return (
        issues
        + check_amount_totals(schema, data, rules)
        + check_duplicates(schema, data, rules)
        + check_holders(schema, data, rules)
    )


def _duplicate_columns(rep: RepeatSpec) -> list[RepeatColumn]:
    return [c for c in rep.columns if c.role == "input" and _DUPLICATE_COLUMN_RE.search(c.label)]


def check_duplicates(
    schema: FormSchema, data: FormInput, config: RuleConfig | None = None
) -> list[Issue]:
    """명단 안에서 같은 학번이 두 번 나오는가 (IN_16). 두 번째 행부터 경고한다."""
    config = config or RuleConfig()
    if ROW_DUPLICATE in config.disabled:
        return []
    issues: list[Issue] = []
    for rep in schema.repeats:
        if not _in_variant(schema, rep.table, data.variant):
            continue
        rows = data.rows.get(rep.id, [])
        for col in _duplicate_columns(rep):
            seen: dict[str, int] = {}
            for i, row in enumerate(rows):
                value = "".join(str(row.get(col.col, "")).split())
                if not value:
                    continue
                if value not in seen:
                    seen[value] = i
                    continue
                where = Location("repeat", rep.id, rep.table, rep.start_row + i, col.col, i,
                                 col.label)  # fmt: skip
                if where.key in config.ignore:
                    continue
                issues.append(
                    Issue(
                        ROW_DUPLICATE,
                        _severity(config, ROW_DUPLICATE),
                        where,
                        f"'{col.label}' {value} 이(가) {seen[value] + 1}번째 행과 겹칩니다.",
                        "같은 사람을 두 번 적었는지 확인하세요.",
                    )
                )
    return issues


def _holder_columns(rep: RepeatSpec) -> tuple[RepeatColumn, RepeatColumn] | None:
    """(성명 열, 예금주 열). 둘이 따로 있을 때만. `성명 (예금주)` 한 열이면 볼 것이 없다."""
    inputs = [c for c in rep.columns if c.role == "input"]
    holder = next((c for c in inputs if "예금주" in c.label and "성명" not in c.label), None)
    name = next((c for c in inputs if column_concept(c.label) == "name" and c is not holder
                 and "예금주" not in c.label), None)  # fmt: skip
    return (name, holder) if name and holder else None


def check_holders(
    schema: FormSchema, data: FormInput, config: RuleConfig | None = None
) -> list[Issue]:
    """명단에서 예금주가 성명과 다른가. 본인 명의 계좌가 아니면 지급이 늦어진다 (경고)."""
    config = config or RuleConfig()
    if HOLDER_MISMATCH in config.disabled:
        return []
    issues: list[Issue] = []
    for rep in schema.repeats:
        cols = _holder_columns(rep)
        if cols is None or not _in_variant(schema, rep.table, data.variant):
            continue
        name_col, holder_col = cols
        for i, row in enumerate(data.rows.get(rep.id, [])):
            name = "".join(str(row.get(name_col.col, "")).split())
            holder = "".join(str(row.get(holder_col.col, "")).split())
            if not name or not holder or name == holder:
                continue
            where = Location("repeat", rep.id, rep.table, rep.start_row + i, holder_col.col, i,
                             holder_col.label)  # fmt: skip
            if where.key in config.ignore:
                continue
            issues.append(
                Issue(
                    HOLDER_MISMATCH,
                    _severity(config, HOLDER_MISMATCH),
                    where,
                    f"{i + 1}번째 행의 예금주 '{holder}' 이(가) 성명 '{name}' 과(와) 다릅니다.",
                    "본인 명의 계좌인지 확인하세요. 다른 사람 계좌면 지급이 늦어질 수 있습니다.",
                )
            )
    return issues


def applicable_rules(schema: FormSchema, config: ValidationConfig = DEFAULT_CONFIG) -> set[str]:
    """이 서식에 적용되는 규칙 (관리자 페이지의 '규칙·적용 서식 조회', 회귀 기준)."""
    rules = config.for_form(schema)
    ex = rules.exclusions
    full = FormInput(rows={r.id: [{}] * r.capacity for r in schema.repeats})
    found: set[str] = set()
    for t, _ in _targets(schema, full, replace(ex, unused_rows=False)):
        if t.required:
            found.add(REQUIRED_EMPTY)
        if t.input_type == "date":
            found |= {DATE_INVALID, DATE_ORDER}
        if t.input_type == "amount":
            found.add(AMOUNT_INVALID)
        if kind := _identity_kind(t):
            found.add(ACCOUNT_INVALID if kind == "account" else RRN_INVALID)
    if any(s.role == "input" and is_total_text(s.label) for s in schema.slots):
        found.add(AMOUNT_TOTAL_MISMATCH)
    if any(_duplicate_columns(r) for r in schema.repeats):
        found.add(ROW_DUPLICATE)
    if any(_holder_columns(r) for r in schema.repeats):
        found.add(HOLDER_MISMATCH)
    return found - rules.disabled
