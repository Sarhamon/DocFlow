"""본문 초안 (M10, F2-b) — 이미 채운 입력값으로 LLM 이 서술형 칸의 글을 쓴다.

서술형 칸은 기안문 본문, 계획서의 목적·추진 배경 같은 칸이다.

1. 대상 칸 (DR_01): 기안문 `content_body` 와 이름이 서술형(목적·배경·필요성·개요 …)인 글 칸.
2. 입력값 (DR_02): 그 칸 말고 채운 칸들. 명단은 사람 대신 **인원과 금액 합계**만 넘긴다 — 이름·계좌·
   주민등록번호는 LLM 에 가지 않는다. 합계는 코드가 더한다(LLM 에 계산을 맡기지 않는다).
3. 인용 검사 (DR_05): 초안의 금액·날짜·인원(명·건·회)이 입력값에 없으면 `DRAFT_UNCITED` 경고.
4. 채택한 칸은 입력값의 `_drafted` 에 근거와 함께 적는다(칸 -> 고쳐 쓰기 전의 글·요청).
   실시간 점검·검증이 같은 인용 검사를 다시 돌려서, 사용자가 초안이나 입력값을 고쳐 숫자가
   어긋나면 다시 경고한다 (ED_05). LLM 은 부르지 않는다. 기안문은 금액·인원을 본문에만 적어서
   고쳐 쓰기 전의 글도 인용 근거로 인정한다.

저장하지 않는다. 화면이 초안을 보여 주고 사용자가 채택·수정·재생성한다 (DR_06~08).

자연어 일괄 입력은 서술형 칸을 묻지 않고, 채운 값과 사용자의 글로 이 초안을 이어서 쓴다
(`prefill_with_drafts`). 글이 요청이자 인용 근거다.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

from docflow.llm import LlmClient, LlmError, LlmRequest
from docflow.pipeline import parse_input
from docflow.schema.model import FormSchema
from docflow.schema.rules import column_concept
from docflow.service.prefill import (
    COUNT_RE,
    PRIVATE,
    ROSTER,
    Filled,
    Prefill,
    Source,
    counts_in,
    money_in,
    narrative,
    plan,
    prefill,
    story_keys,
)
from docflow.validate import FormInput, Issue, Location, find_dates, item_amount

log = logging.getLogger(__name__)

DRAFT_UNCITED = "DRAFT_UNCITED"
#: 입력값에서 채택한 초안 칸을 적어 두는 키. `_` 로 시작해 서식 입력으로는 읽히지 않는다
DRAFTED = "_drafted"
#: 추가 요청의 최대 길이
MAX_INSTRUCTION = 1000
#: 문서 아래쪽 연락처 누름틀. 본문 내용이 아니라 넘기지 않는다
#: (실측: 넘기자 본문 끝에 `문의 : 031-441-0000 | 웹사이트 : …` 줄을 지어 붙였다)
_CONTACT_FIELDS = frozenset(
    {"zipcode", "address", "homeurl", "telephone", "fax", "email", "publication"}
)
#: 연락처 표 칸의 이름
_CONTACT_LABEL_RE = re.compile(
    r"전화|연락처|팩스|전송|FAX|전자우편|이메일|E-?mail|주소|우편번호|홈페이지", re.I
)
#: 붙임(첨부 문서)을 가리키는 말
_ATTACHMENT_RE = re.compile(r"붙\s*임|첨\s*부")
#: 서식 예시(형식 참고)를 넘기는 최대 길이
_EXAMPLE_LIMIT = 600

SYSTEM = """\
너는 대학 산학협력단의 공문을 쓰는 담당자다. 주어진 입력값으로 서식의 한 칸에 들어갈 글을 쓴다.
- 공문 문체로 쓴다. 문장은 '~함', '~임', '~하고자 함' 처럼 끝낸다.
- 숫자·금액·날짜·인원·장소는 입력값에 적힌 그대로 옮긴다. 새로 계산하거나 지어내지 않는다.
- 입력값에 없는 사실(문서번호, 날짜, 금액, 장소, 사람 이름)은 쓰지 않는다.
  모르는 항목(예: 예산이 없으면 예산 줄)은 줄째 뺀다.
  '0,000,000원', '○○' 같은 빈칸 표시도 쓰지 않는다.
- 서식 예시가 있으면 그 형식(번호 붙이기 등)을 따른다. 예시의 내용은 옮기지 않는다.
- 붙임(첨부 문서)은 입력값이나 요청에 붙임·첨부가 있을 때만 쓴다.
  없으면 붙임 줄과 '붙임 참조'를 뺀다.
- 이 칸에 들어갈 글만 쓴다. 서식 제목, 문서번호, 연락처, '끝.' 은 쓰지 않는다.
- 꾸밈말 없이 간결하게 쓴다.
"""


# ---------------------------------------------------------------- 대상 칸 (DR_01)
@dataclass(frozen=True)
class DraftTarget:
    kind: Literal["field", "cell"]
    #: 누름틀 이름 / 슬롯 id (검증 결과의 where 와 같다)
    key: str
    label: str
    #: 서식에 적혀 있던 글 (형식 참고)
    example: str
    location: Location


def targets(schema: FormSchema, variant: int | None = None) -> list[DraftTarget]:
    """초안을 만들 수 있는 칸. 글 칸 가운데 본문·목적·배경 같은 이름이고 개인정보가 아닌 것."""
    examples = {f.name: f.placeholder for f in schema.fields}
    examples.update({s.id: s.placeholder for s in schema.slots})
    p = plan(schema, variant)
    return [
        DraftTarget(t.kind, t.key, t.label, examples.get(t.key, "").strip()[:_EXAMPLE_LIMIT],
                    t.location())  # fmt: skip
        for t in p.fields + p.cells
        if narrative(t)
    ]


# ---------------------------------------------------------------- 입력값 (DR_02)
@dataclass(frozen=True)
class Fact:
    key: str
    label: str
    value: str


def _one_line(text: str) -> str:
    return " ".join(str(text).split())


def facts(schema: FormSchema, data: FormInput, exclude: str = "") -> list[Fact]:
    """LLM 에 넘기는 입력값. exclude 칸(초안을 쓸 칸)과 연락처 칸은 뺀다."""
    p = plan(schema, data.variant)
    fields = [t for t in p.fields if t.key not in _CONTACT_FIELDS]
    cells = [t for t in p.cells if not _CONTACT_LABEL_RE.search(t.label)]
    out = [
        Fact(t.key, t.label, _one_line(v))
        for t, v in [(t, data.fields.get(t.key, "")) for t in fields]
        + [(t, data.cells.get(t.key, "")) for t in cells]
        if t.key != exclude and v.strip()
    ]
    for rep in schema.repeats:
        owner = schema.variant_of(rep.table)
        if data.variant is not None and owner is not None and owner != data.variant:
            continue
        cols = [c for c in rep.columns if c.role == "input"]
        concepts = {c.col: column_concept(c.label) for c in cols}
        roster = bool(set(concepts.values()) & ROSTER)
        lines = data.rows.get(rep.id, [])
        name = f"표 {rep.table + 1} {'명단' if roster else '내역'}"
        if roster:
            people = [r for r in lines if any(r.get(c, "").strip() for c, k in concepts.items()
                                              if k in ROSTER)]  # fmt: skip
            if people:
                out.append(Fact(f"{rep.id}.count", f"{name} 인원", f"{len(people)}명"))
            lines = people
        else:
            lines = [r for r in lines if any(v.strip() for v in r.values())]
            for i, row in enumerate(lines):
                parts = [
                    f"{_one_line(c.label)}: {_one_line(row[c.col])}"
                    for c in cols
                    if concepts[c.col] not in PRIVATE and row.get(c.col, "").strip()
                ]
                if parts:
                    out.append(Fact(f"{rep.id}[{i}]", f"{name} {i + 1}행", " / ".join(parts)))
        for c in cols:
            if c.input_type != "amount":
                continue
            amounts = [n for r in lines if (n := item_amount(r.get(c.col, ""))) is not None]
            if amounts:
                label = f"{name} {_one_line(c.label)} 합계"
                out.append(Fact(f"{rep.id}.c{c.col}.sum", label, f"{sum(amounts):,}원"))
    return out


# ---------------------------------------------------------------- 인용 검사 (DR_05, ED_05)
def mismatches(text: str, source: list[Fact], today: date) -> list[str]:
    """글의 금액·인원·날짜 가운데 입력값에 없는 것 (보이는 모양으로)."""
    joined = "\n".join(f.value for f in source)
    src = Source.of(joined)
    # 0원은 늘 경고한다: `0,000,000원` 은 모델이 남긴 빈칸 표시다(실측)
    missing = [f"{n:,}원" for n in sorted(money_in(text)) if n == 0 or n not in src.amounts]
    missing += [m.group().strip() for m in COUNT_RE.finditer(text)
                if int(m.group(1).replace(",", "")) not in src.amounts]  # fmt: skip
    missing += [f"{d:%Y. %m. %d.}" for d in find_dates(text)
                if d is not None and not src.has_date(d, today)]  # fmt: skip
    # 붙임: 적지 않은 첨부 문서를 지어냈다(실측: `붙임 : 개선 과제 운영 계획서 1부.`)
    if _ATTACHMENT_RE.search(text) and not _ATTACHMENT_RE.search(joined):
        missing.append("붙임")
    return list(dict.fromkeys(missing))


def cited(text: str, source: list[Fact]) -> list[Fact]:
    """초안이 인용한 입력값: 값이 그대로 나오거나, 금액·인원·날짜가 겹친다."""
    flat = _one_line(text)
    money, counts = money_in(text), counts_in(text)
    dates = {d for d in find_dates(text) if d is not None}
    out = []
    for f in source:
        mine = Source.of(f.value)  # 입력값 쪽은 단위 없는 숫자도 금액으로 본다 (`금액(원): 50,000`)
        if ((len(f.value) >= 2 and f.value in flat) or mine.amounts & (money | counts)
                or mine.dates & dates):  # fmt: skip
            out.append(f)
    return out


def _issue(t: DraftTarget, missing: list[str]) -> Issue:
    return Issue(
        DRAFT_UNCITED, "warning", t.location,
        f"{t.label}의 {', '.join(missing)} 이(가) 입력값에 없습니다",
        "입력한 칸의 값과 맞추거나 글을 고치세요",
    )  # fmt: skip


def _basis_fact(basis: str) -> list[Fact]:
    """고쳐 쓰기 전의 글과 요청. 기안문은 금액·인원을 본문에만 적어서, 이것도 인용 근거다."""
    return [Fact("_basis", "고쳐 쓰기 전의 글", basis)] if basis.strip() else []


def attachment_facts(names: list[str]) -> list[Fact]:
    """붙인 문서 (`service.attachments`).

    초안이 붙임 줄을 맞게 쓰고, 인용 검사가 붙임을 근거로 본다.
    """
    if not names:
        return []
    listed = ", ".join(f"{i}. {name} 1부" for i, name in enumerate(names, 1))
    return [Fact("_attachments", "붙임 문서", f"붙임 {listed}")]


def drafted_of(raw: dict[str, Any]) -> dict[str, str]:
    """입력값의 `_drafted`: 칸 -> 근거. 예전 모양(칸 목록)도 읽는다."""
    drafted = raw.get(DRAFTED)
    if isinstance(drafted, list):
        return {str(k): "" for k in drafted}
    if isinstance(drafted, dict):
        return {str(k): v if isinstance(v, str) else "" for k, v in drafted.items()}
    return {}


def check(
    schema: FormSchema,
    raw: dict[str, Any],
    data: FormInput,
    today: date | None = None,
    *,
    extra: list[Fact] | None = None,
) -> list[Issue]:
    """채택한 초안 칸(`_drafted`)이 입력값과 어긋나는가 (ED_05). LLM 을 부르지 않는다.

    extra 는 입력값 밖의 근거(붙인 문서)다.
    """
    drafted = drafted_of(raw)
    if not drafted:
        return []
    today = today or date.today()
    issues = []
    for t in targets(schema, data.variant):
        if t.key not in drafted:
            continue
        text = (data.fields if t.kind == "field" else data.cells).get(t.key, "")
        source = facts(schema, data, t.key) + (extra or []) + _basis_fact(drafted[t.key])
        if text.strip() and (missing := mismatches(text, source, today)):
            issues.append(_issue(t, missing))
    return issues


# ---------------------------------------------------------------- 만들기 (DR_03·04)
@dataclass
class Draft:
    target: DraftTarget
    text: str
    cited: list[Fact]
    issues: list[Issue]
    #: 인용 근거로 인정한 고쳐 쓰기 전의 글·요청. 채택하면 `_drafted` 에 같이 적는다
    basis: str = ""
    model: str = ""
    elapsed: float = 0.0


OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {"text": {"type": "string", "description": "칸에 들어갈 글"}},
    "required": ["text"],
    "additionalProperties": False,
}


def build_request(
    schema: FormSchema, t: DraftTarget, source: list[Fact], current: str, instruction: str,
    today: date, *, retry: bool = False,
) -> LlmRequest:  # fmt: skip
    lines = [f"서식: {schema.title}", f"오늘: {today:%Y. %m. %d.}", f"쓸 칸: {t.label}"]
    lines += ["", "입력값:"]
    lines += [f"- {f.label}: {f.value}" for f in source]
    if t.example:
        lines += ["", "서식 예시 (형식만 참고):", t.example]
    if current.strip():
        lines += ["", "지금 적힌 글 (고쳐 쓴다):", current.strip()]
    if instruction.strip():
        lines += ["", "요청:", instruction.strip()]
    # 재생성은 다른 글이 나와야 하므로 온도를 올린다 (DR_08)
    return LlmRequest(SYSTEM, "\n".join(lines), OUTPUT_SCHEMA, temperature=0.7 if retry else 0.0)


def draft(
    schema: FormSchema,
    raw: dict[str, Any],
    key: str,
    client: LlmClient,
    *,
    instruction: str = "",
    retry: bool = False,
    today: date | None = None,
    extra: list[Fact] | None = None,
) -> Draft:
    """raw(저장하지 않은 입력값 포함)로 key 칸의 초안을 쓴다. LLM 을 못 쓰면 `LlmError`.

    extra 는 입력값 밖의 근거(붙인 문서)다. LLM 에 입력값과 함께 넘긴다.
    """
    if len(instruction) > MAX_INSTRUCTION:
        raise ValueError(f"요청은 {MAX_INSTRUCTION:,}자까지 적습니다")
    data = parse_input(schema, raw).data
    t = next((x for x in targets(schema, data.variant) if x.key == key), None)
    if t is None:
        raise ValueError(f"{key} 은(는) 초안을 만드는 칸이 아닙니다")
    return _write(schema, data, t, client, instruction, retry, today or date.today(), extra)


def _write(
    schema: FormSchema, data: FormInput, t: DraftTarget, client: LlmClient,
    instruction: str, retry: bool, today: date, extra: list[Fact] | None = None,
) -> Draft:  # fmt: skip
    key = t.key
    source = facts(schema, data, key) + (extra or [])
    current = (data.fields if t.kind == "field" else data.cells).get(key, "")
    if not source and not current.strip() and not instruction.strip():
        raise ValueError("채운 칸이 없습니다. 사업명·기간·금액 같은 칸을 먼저 채우세요")
    request = build_request(schema, t, source, current, instruction, today, retry=retry)
    result = client.complete(request)
    text = result.data.get("text")
    if not isinstance(text, str) or not text.strip():
        raise LlmError("LLM 이 빈 초안을 냈다")
    text = text.strip()
    basis = "\n".join(x.strip() for x in (current, instruction) if x.strip())
    missing = mismatches(text, source + _basis_fact(basis), today)
    return Draft(t, text, cited(text, source), [_issue(t, missing)] if missing else [], basis,
                 result.model, result.elapsed)  # fmt: skip


# ---------------------------------------------------------------- 글로 채우기 + 초안 (F2-c)
def prefill_with_drafts(
    schema: FormSchema,
    text: str,
    client: LlmClient,
    *,
    variant: Any = None,
    today: date | None = None,
    extra: list[Fact] | None = None,
) -> Prefill:
    """자연어 일괄 입력에 이어 서술형 칸마다 본문 초안을 쓴다. 사용자의 글이 요청이자 인용 근거다.

    초안 하나가 실패하면 그 칸만 비워 둔다(편집 화면의 [초안 쓰기]로 다시 쓴다).
    """
    today = today or date.today()
    out = prefill(schema, text, client, variant=variant, today=today)
    raw: dict[str, Any] = {part: dict(out.input.get(part) or {}) for part in ("fields", "cells")}
    raw["rows"] = out.input.get("rows") or {}
    if variant is not None:
        raw["variant"] = variant
    data = parse_input(schema, raw).data
    p = plan(schema, data.variant)
    by_key = {x.key: x for x in p.fields + p.cells}
    stories = story_keys(p)
    for t in targets(schema, data.variant):
        if t.key not in stories:
            continue  # 행마다 적는 사유 칸은 일괄 입력이 옮겨 적었다
        try:
            d = _write(schema, data, t, client, text.strip(), False, today, extra)
        except LlmError as exc:
            log.warning("초안을 쓰지 못했다 (%s): %s", t.key, exc)
            continue
        out.input["fields" if t.kind == "field" else "cells"][t.key] = d.text
        out.filled.append(Filled(by_key[t.key], d.text))
        out.issues += d.issues
        out.drafted[t.key] = d.basis
        out.elapsed += d.elapsed
        out.model = out.model or d.model
    return out
