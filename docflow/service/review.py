"""정성 판정 (M10, ER_05·ER_06) — 규정 해석이 필요한 물음을 LLM 이 체크리스트로 판정한다.

체크리스트는 관리자가 정한다(모든 서식 공통 또는 서식별). 사용자가 편집 화면에서
[정성 판정]을 누르면 LLM 이 물음마다 적합·부적합·판단 불가와 이유, 근거 문장을 낸다.
부적합·판단 불가는 `QUALITATIVE` 경고로 규칙 검증과 같은 오류 목록에 들어간다.

* 넘기는 입력값은 본문 초안과 같다(`draft.facts`): 명단은 인원·합계만, 계좌·주민등록번호는 빼고.
* **근거 문장은 문서에서 찾아 본다.** 입력값에 그대로 있으면 그 칸을 가리키고(누르면 칸으로 간다),
  없으면 "찾지 못했다"고 알린다 — LLM 이 지어낸 근거를 그대로 믿지 않게 한다.
* 경고일 뿐 문서 만들기를 막지 않는다. 같은 입력에 같은 판정이 나와야 하는 규칙 검증과 달리 LLM 의
  판정이라서다. 저장하지 않고 검증 이력에도 남기지 않는다.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from docflow.llm import LlmClient, LlmRequest
from docflow.pipeline import parse_input
from docflow.schema.model import FormSchema
from docflow.service.db import ChecklistItem, Form
from docflow.service.draft import facts
from docflow.service.prefill import plan
from docflow.validate import FormInput, Issue, Location

QUALITATIVE = "QUALITATIVE"
#: 물음 하나의 최대 길이
MAX_TEXT = 300
#: 한 번에 묻는 최대 물음 수 (7.8B 의 문맥과 응답 시간)
MAX_ITEMS = 20
VERDICTS = {"pass": "적합", "fail": "부적합", "unknown": "판단 불가"}
#: 근거가 문서의 어느 칸에도 없을 때 가리키는 자리
DOCUMENT = Location("document", "document", label="문서 전체")

SYSTEM = """\
너는 대학 산학협력단의 집행 서류를 검토하는 담당자다.
체크리스트의 물음마다 문서 내용만 보고 판정한다.
- reason: 먼저 판정 이유를 한두 문장으로 쓴다.
- evidence: 판정의 근거가 된 문서의 글을 고치지 말고 그대로 짧게 옮긴다. 근거가 없으면 "".
- verdict: 이유에 맞게 판정한다. 물음을 충족하면 "pass", 어기면 "fail",
  문서에 판단할 내용이 없으면 "unknown".
- 문서에 없는 사실을 지어내지 않는다. 확신이 없으면 "unknown" 이다.
"""


# ---------------------------------------------------------------- 체크리스트 관리 (관리자)
def items_for(session: Session, form_id: int) -> list[ChecklistItem]:
    """이 서식에 묻는 물음: 켜진 공통 물음 + 이 서식의 물음."""
    query = (
        select(ChecklistItem)
        .where(ChecklistItem.enabled.is_(True),
               ChecklistItem.form_id.is_(None) | (ChecklistItem.form_id == form_id))
        .order_by(ChecklistItem.form_id.is_not(None), ChecklistItem.id)
    )  # fmt: skip
    return list(session.scalars(query))[:MAX_ITEMS]


def all_items(session: Session) -> list[ChecklistItem]:
    query = select(ChecklistItem).order_by(ChecklistItem.form_id.is_not(None), ChecklistItem.id)
    return list(session.scalars(query))


def save_item(
    session: Session, item: ChecklistItem | None, *, text: str, form_id: int | None, enabled: bool
) -> ChecklistItem:
    text = " ".join(text.split())
    if not text:
        raise ValueError("물음을 적는다")
    if len(text) > MAX_TEXT:
        raise ValueError(f"물음은 {MAX_TEXT}자까지 적는다")
    if form_id is not None and session.get(Form, form_id) is None:
        raise ValueError(f"없는 서식이다: {form_id}")
    if item is None:
        item = ChecklistItem()
        session.add(item)
    item.text, item.form_id, item.enabled = text, form_id, enabled
    session.commit()
    return item


def delete_item(session: Session, item: ChecklistItem) -> None:
    session.delete(item)
    session.commit()


# ---------------------------------------------------------------- 판정
@dataclass(frozen=True)
class Question:
    id: int
    text: str


@dataclass(frozen=True)
class Verdict:
    question: Question
    #: pass / fail / unknown
    verdict: str
    reason: str
    evidence: str
    #: 근거가 있는 칸. 문서에서 찾지 못하면 DOCUMENT
    location: Location

    @property
    def verified(self) -> bool:
        return self.location != DOCUMENT


@dataclass
class Review:
    verdicts: list[Verdict]
    issues: list[Issue]
    model: str = ""
    elapsed: float = 0.0


def _flat(text: str) -> str:
    return " ".join(str(text).split())


def _tight(text: str) -> str:
    """띄어쓰기를 모두 뺀다. 서식은 `장 소` 처럼 글자 사이를 띄우고, 모델은 붙여 옮긴다."""
    return "".join(str(text).split())


def _haystack(schema: FormSchema, data: FormInput) -> list[tuple[Location, str]]:
    """근거를 찾아볼 칸과 값. LLM 에 넘긴 칸만 본다 (개인정보 칸에서는 찾지 않는다)."""
    p = plan(schema, data.variant)
    out = [(t.location(), data.fields.get(t.key, "")) for t in p.fields]
    out += [(t.location(), data.cells.get(t.key, "")) for t in p.cells]
    for rid, cols in p.rows.items():
        for i, row in enumerate(data.rows.get(rid, [])):
            out += [(t.location(i), row.get(int(t.key), "")) for t in cols]
    return [(loc, _flat(v)) for loc, v in out if v.strip()]


#: 근거를 나눠 찾을 때 이보다 짧은 조각은 쓰지 않는다 (`일시` 같은 낱말은 어디에나 있다)
_MIN_PIECE = 6


def locate(evidence: str, hay: list[tuple[Location, str]]) -> Location:
    """근거 문장이 그대로 있는 칸. 따옴표·띄어쓰기 차이는 무시한다.

    여러 줄에 걸친 근거(`일시 : …\\n장소 : …`)는 원문 사이에 번호(`b.`)가 끼어 통째로는 없으므로
    줄·쉼표마다 나눠 찾는다.
    """
    quote = " \"'“”‘’"
    whole = _flat(evidence).strip(quote)
    pieces = [whole] + [p for part in re.split(r"\n|, |; |…", evidence)
                        if len(p := _flat(part).strip(quote)) >= _MIN_PIECE]  # fmt: skip
    for needle in pieces:
        if len(needle) < 2:
            continue
        for loc, value in hay:
            if _tight(needle) in _tight(value):
                return loc
    return DOCUMENT


def output_schema(questions: list[Question]) -> dict[str, Any]:
    one = {
        "type": "object",
        "properties": {
            # 이유·근거를 먼저 쓰고 판정을 마지막에. 판정을 먼저 쓰면 7.8B 가 이유와 어긋난
            # 판정을 낸다(실측: 1인당 8,421원이라 1만원 이내라고 쓰고 '부적합')
            "reason": {"type": "string"},
            "evidence": {"type": "string"},
            "verdict": {"type": "string", "enum": list(VERDICTS)},
        },
        "required": ["reason", "evidence", "verdict"],
        "additionalProperties": False,
    }
    props = {f"q{q.id}": {**one, "description": q.text} for q in questions}
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


def build_request(
    schema: FormSchema, data: FormInput, questions: list[Question], today: date
) -> LlmRequest:
    lines = [f"서식: {schema.title}", f"오늘: {today:%Y. %m. %d.}", "", "문서 내용:"]
    lines += [f"- {f.label}: {f.value}" for f in facts(schema, data)]
    lines += ["", "체크리스트:"] + [f"- q{q.id}: {q.text}" for q in questions]
    return LlmRequest(SYSTEM, "\n".join(lines), output_schema(questions))


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _issue(v: Verdict) -> Issue:
    if v.verified:
        action = f"근거: “{_flat(v.evidence)}” — 칸을 확인하고 필요하면 고치세요"
    elif v.evidence:
        action = f"LLM 이 댄 근거 “{_flat(v.evidence)}”를 문서에서 찾지 못했습니다. 직접 확인하세요"
    else:
        action = "문서에 판단할 내용이 없습니다. 직접 확인하세요"
    reason = f" — {v.reason}" if v.reason else ""
    return Issue(QUALITATIVE, "warning", v.location,
                 f"[{VERDICTS[v.verdict]}] {v.question.text}{reason}", action)  # fmt: skip


def review(
    schema: FormSchema,
    raw: dict[str, Any],
    questions: list[Question],
    client: LlmClient,
    *,
    today: date | None = None,
) -> Review:
    """체크리스트로 문서를 판정한다. LLM 을 못 쓰면 `LlmError`."""
    if not questions:
        raise ValueError("이 서식에는 정성 판정 체크리스트가 없습니다 (관리자가 정합니다)")
    data = parse_input(schema, raw).data
    if not facts(schema, data):
        raise ValueError("채운 칸이 없습니다. 칸을 채운 뒤 판정하세요")
    result = client.complete(build_request(schema, data, questions, today or date.today()))
    hay = _haystack(schema, data)
    verdicts = []
    for q in questions:
        answer = result.data.get(f"q{q.id}")
        answer = answer if isinstance(answer, dict) else {}
        verdict = answer.get("verdict") if answer.get("verdict") in VERDICTS else "unknown"
        evidence = _text(answer.get("evidence"))
        verdicts.append(Verdict(q, verdict, _text(answer.get("reason")), evidence,
                                locate(evidence, hay)))  # fmt: skip
    issues = [_issue(v) for v in verdicts if v.verdict != "pass"]
    return Review(verdicts, issues, result.model, result.elapsed)
