"""문서 간 검증 (M11, VA_11·VA_12) — 계획서와 정산서처럼 두 문서의 금액을 견준다.

관리자가 서식 쌍과 견줄 항목을 규칙으로 정하고(`CrossRule`), 사용자가 문서를 이어 두면
(`DocumentLink`) 검증할 때 두 문서의 금액을 비교한다. 정산은 계획과 달라질 수 있어 결과는
경고이고, 만들기를 막지 않는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from docflow.pipeline import InputError, parse_input
from docflow.schema.model import FormSchema
from docflow.service.catalog import NotFound, column_key, get_form, load_schema
from docflow.service.db import CrossRule, Document, DocumentLink, FormVersion
from docflow.validate import FormInput, Issue, Location, parse_amount

CROSS_AMOUNT_MISMATCH = "CROSS_AMOUNT_MISMATCH"
OPS = {"eq": "같아야 한다", "le": "대상이 기준보다 크지 않아야 한다"}


# ---------------------------------------------------------------- 규칙 (관리자)
def rule_keys(rule: CrossRule) -> tuple[list[str], list[str]]:
    return json.loads(rule.source_keys), json.loads(rule.target_keys)


def input_keys(schema: FormSchema) -> dict[str, str]:
    """대조에 쓸 수 있는 항목 키 -> 이름 (작성자가 채우는 누름틀·표 칸·명단 열)."""
    keys = {f.name: f.label or f.name for f in schema.fields if f.role == "input"}
    keys |= {s.id: s.label or s.id for s in schema.slots if s.role == "input"}
    keys |= {
        column_key(r, c): f"{c.label or c.col} (명단 합)"
        for r in schema.repeats
        for c in r.columns
        if c.role == "input"
    }
    return keys


def save_rule(session: Session, rule: CrossRule | None, values: dict) -> CrossRule:
    """규칙을 만들거나 고친다. 서식과 항목 키가 실제로 있는지 본다."""
    name = (values.get("name") or "").strip()
    if not name:
        raise ValueError("규칙 이름이 비었다")
    if values.get("op", "eq") not in OPS:
        raise ValueError(f"비교는 {list(OPS)} 중 하나다")
    sides = {}
    for side in ("source", "target"):
        form = get_form(session, values[f"{side}_form_id"], public_only=False)
        keys = list(dict.fromkeys(values.get(f"{side}_keys") or []))
        if not keys:
            raise ValueError("견줄 항목을 하나 이상 고른다")
        known = input_keys(load_schema(form.latest))
        if missing := [k for k in keys if k not in known]:
            raise ValueError(f"'{form.title}' 에 없는 항목이다: {missing}")
        sides[side] = (form.id, keys)
    rule = rule or CrossRule()
    rule.name = name
    rule.op = values.get("op", "eq")
    rule.enabled = bool(values.get("enabled", True))
    rule.source_form_id, source = sides["source"]
    rule.target_form_id, target = sides["target"]
    rule.source_keys = json.dumps(source, ensure_ascii=False)
    rule.target_keys = json.dumps(target, ensure_ascii=False)
    session.add(rule)
    session.commit()
    return rule


def delete_rule(session: Session, rule: CrossRule) -> None:
    for link in session.scalars(select(DocumentLink).where(DocumentLink.rule_id == rule.id)):
        session.delete(link)
    session.flush()  # 연결을 먼저 지운다 (규칙을 가리키는 외래 키)
    session.delete(rule)
    session.commit()


# ---------------------------------------------------------------- 연결 (사용자)
@dataclass
class Side:
    """이 문서가 규칙에서 어느 쪽인가."""

    rule: CrossRule
    #: source 기준(계획서) / target 대상(정산서)
    role: str

    @property
    def own_keys(self) -> list[str]:
        source, target = rule_keys(self.rule)
        return source if self.role == "source" else target

    @property
    def other_keys(self) -> list[str]:
        source, target = rule_keys(self.rule)
        return target if self.role == "source" else source

    @property
    def other_form_id(self) -> int:
        return self.rule.target_form_id if self.role == "source" else self.rule.source_form_id


def sides_of(session: Session, doc: Document) -> list[Side]:
    """이 문서의 서식에 걸린 대조 규칙 (켜진 것만)."""
    form_id = doc.form_version.form_id
    rules = session.scalars(
        select(CrossRule)
        .where(CrossRule.enabled.is_(True),
               or_(CrossRule.source_form_id == form_id, CrossRule.target_form_id == form_id))
        .order_by(CrossRule.id)
    )  # fmt: skip
    out = []
    for rule in rules:
        if rule.source_form_id == form_id:
            out.append(Side(rule, "source"))
        if rule.target_form_id == form_id:
            out.append(Side(rule, "target"))
    return out


def linked(session: Session, doc: Document, rule: CrossRule) -> Document | None:
    """이 규칙에 이어 둔 상대 문서. 어느 쪽에서 이었든 찾는다."""
    link = session.scalar(
        select(DocumentLink).where(
            DocumentLink.rule_id == rule.id,
            or_(DocumentLink.document_id == doc.id, DocumentLink.linked_id == doc.id),
        )
    )
    if link is None:
        return None
    other = link.linked_id if link.document_id == doc.id else link.document_id
    return session.get(Document, other)


def candidates(session: Session, doc: Document, side: Side) -> list[Document]:
    """이을 수 있는 문서: 같은 작성자가 쓴 상대 서식 문서."""
    query = (
        select(Document)
        .join(FormVersion, Document.form_version_id == FormVersion.id)
        .where(FormVersion.form_id == side.other_form_id, Document.id != doc.id,
               Document.owner_id == doc.owner_id)
        .order_by(Document.updated_at.desc())
    )  # fmt: skip
    return list(session.scalars(query))


def link(session: Session, doc: Document, rule_id: int, other_id: int | None) -> None:
    """문서를 잇거나(other_id) 끊는다(None). 상대는 같은 작성자의 상대 서식 문서여야 한다."""
    side = next((s for s in sides_of(session, doc) if s.rule.id == rule_id), None)
    if side is None:
        raise NotFound(f"이 문서에 걸린 대조 규칙 {rule_id} 이(가) 없다")
    for row in session.scalars(
        select(DocumentLink).where(
            DocumentLink.rule_id == rule_id,
            or_(DocumentLink.document_id == doc.id, DocumentLink.linked_id == doc.id),
        )
    ):
        session.delete(row)
    session.flush()
    if other_id is not None:
        if other_id not in {d.id for d in candidates(session, doc, side)}:
            raise ValueError("이을 수 없는 문서다 (내가 쓴 상대 서식 문서만 잇는다)")
        session.add(DocumentLink(document_id=doc.id, rule_id=rule_id, linked_id=other_id))
    session.commit()


def unlink_all(session: Session, doc: Document) -> None:
    """문서를 지울 때 이어 둔 것을 함께 지운다."""
    for row in session.scalars(
        select(DocumentLink).where(
            or_(DocumentLink.document_id == doc.id, DocumentLink.linked_id == doc.id)
        )
    ):
        session.delete(row)
    session.flush()  # 문서를 지우기 전에 (문서를 가리키는 외래 키)


# ---------------------------------------------------------------- 검증
def amount(schema: FormSchema, data: FormInput, keys: list[str]) -> int | None:
    """항목들의 금액 합. 읽히는 금액이 하나도 없으면 None (아직 안 채움)."""
    columns = {column_key(r, c): (r.id, c.col) for r in schema.repeats for c in r.columns}
    values: list[int] = []
    for key in keys:
        if key in columns:
            rep_id, col = columns[key]
            texts = [row.get(col, "") for row in data.rows.get(rep_id, [])]
        else:
            texts = [data.cells.get(key) or data.fields.get(key) or ""]
        values += [v for t in texts if (v := parse_amount(t)) is not None]
    return sum(values) if values else None


def _location(schema: FormSchema, key: str, label: str) -> Location:
    for s in schema.slots:
        if s.id == key:
            return Location("cell", s.id, s.table, s.row, s.col, label=label)
    for r in schema.repeats:
        for c in r.columns:
            if column_key(r, c) == key:
                return Location("repeat", r.id, r.table, r.start_row, c.col, 0, label)
    return Location("field", key, label=label)


def check(session: Session, doc: Document, schema: FormSchema, data: FormInput) -> list[Issue]:
    """이어 둔 문서와 금액이 맞는가. 한쪽이라도 금액을 아직 안 채웠으면 보지 않는다."""
    issues: list[Issue] = []
    for side in sides_of(session, doc):
        other = linked(session, doc, side.rule)
        if other is None:
            continue
        other_schema = load_schema(other.form_version)
        try:
            other_data = parse_input(other_schema, json.loads(other.input_json or "{}")).data
        except InputError:
            continue
        mine = amount(schema, data, side.own_keys)
        theirs = amount(other_schema, other_data, side.other_keys)
        if mine is None or theirs is None:
            continue
        source, target = (mine, theirs) if side.role == "source" else (theirs, mine)
        ok = target == source if side.rule.op == "eq" else target <= source
        if ok:
            continue
        rule = side.rule
        names = input_keys(schema)
        first = side.own_keys[0]
        relation = "같아야" if rule.op == "eq" else "넘지 않아야"
        issues.append(
            Issue(
                CROSS_AMOUNT_MISMATCH,
                "warning",
                _location(schema, first, names.get(first, first)),
                f"[{rule.name}] 이 문서 {mine:,}원, 이어 둔 '{other.title}' {theirs:,}원 — "
                f"{rule.target_form.title} 금액이 {rule.source_form.title} 금액과 {relation} 한다.",
                "두 문서의 금액을 확인하세요. 계획이 바뀌었으면 사유를 남기세요.",
            )
        )
    return issues
