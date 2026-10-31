"""붙임 문서 — 기안문에 함께 올릴 문서(사업 계획서 등)를 이어 둔다.

기안문 본문의 `붙임 [2026 AID] … 사업 계획서 1부.` 는 따로 만드는 계획서를 가리킨다. 계획서는 다른
서식이라 기안문 파일에 없다. 작성자가 자기 문서 가운데 붙일 것을 골라 순서대로 잇는다.
본문의 붙임 줄은 이은 문서로 다시 쓴다(`fill_body`).
"""

from __future__ import annotations

import re

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from docflow.schema.model import FormSchema
from docflow.service.catalog import NotFound
from docflow.service.db import Document, DocumentAttachment


def enabled(schema: FormSchema) -> bool:
    """붙임을 받는 서식인가: 본문 누름틀이 있는 기안문·지출품의서."""
    return any(f.name == "content_body" for f in schema.fields)


def attached(session: Session, doc: Document) -> list[Document]:
    """붙인 문서 (붙인 순서대로)."""
    query = (
        select(Document)
        .join(DocumentAttachment, DocumentAttachment.attachment_id == Document.id)
        .where(DocumentAttachment.document_id == doc.id)
        .order_by(DocumentAttachment.position, DocumentAttachment.id)
    )
    return list(session.scalars(query))


def candidates(session: Session, doc: Document) -> list[Document]:
    """붙일 수 있는 문서: 같은 작성자의 다른 문서 가운데 아직 붙이지 않은 것 (최근 것부터)."""
    taken = {d.id for d in attached(session, doc)} | {doc.id}
    query = (
        select(Document)
        .where(Document.owner_id == doc.owner_id)
        .order_by(Document.updated_at.desc())
    )
    return [d for d in session.scalars(query) if d.id not in taken]


def attach(session: Session, doc: Document, other_id: int) -> None:
    """문서를 붙임 맨 뒤에 붙인다. 내 다른 문서만 붙인다."""
    if other_id not in {d.id for d in candidates(session, doc)}:
        raise ValueError("붙일 수 없는 문서다 (내가 쓴 다른 문서만, 한 번씩 붙인다)")
    last = session.scalar(
        select(func.max(DocumentAttachment.position)).where(
            DocumentAttachment.document_id == doc.id
        )
    )
    session.add(DocumentAttachment(document_id=doc.id, attachment_id=other_id,
                                   position=(last or 0) + 1))  # fmt: skip
    session.commit()


def detach(session: Session, doc: Document, other_id: int) -> None:
    row = session.scalar(
        select(DocumentAttachment).where(
            DocumentAttachment.document_id == doc.id, DocumentAttachment.attachment_id == other_id
        )
    )
    if row is None:
        raise NotFound(f"붙인 문서 {other_id} 이(가) 없다")
    session.delete(row)
    session.commit()


def copy_all(session: Session, src: Document, dst: Document) -> None:
    """복제한 문서에 같은 붙임을 같은 순서로 잇는다 (커밋은 부르는 쪽이 한다)."""
    for row in session.scalars(
        select(DocumentAttachment).where(DocumentAttachment.document_id == src.id)
    ):
        session.add(DocumentAttachment(document_id=dst.id, attachment_id=row.attachment_id,
                                       position=row.position))  # fmt: skip


def unlink_all(session: Session, doc: Document) -> None:
    """문서를 지울 때 붙인 것과, 다른 문서에 붙어 있던 것을 함께 지운다."""
    for row in session.scalars(
        select(DocumentAttachment).where(
            or_(DocumentAttachment.document_id == doc.id,
                DocumentAttachment.attachment_id == doc.id)
        )
    ):  # fmt: skip
        session.delete(row)


def titles(session: Session, doc: Document) -> list[str]:
    """붙인 문서의 이름 (붙인 순서대로)."""
    return [d.title for d in attached(session, doc)]


# ---------------------------------------------------------------- 본문의 붙임 줄
#: 공문의 붙임 표기: `붙임` 뒤 두 칸, 여럿이면 번호를 붙이고 둘째 줄부터 번호를 맞춘다
_INDENT = " " * 6
#: 본문 끝의 `끝.` (붙임 줄이 없을 때 그 앞에 붙임을 넣는다)
_END_RE = re.compile(r"\s*끝\.\s*$")


def body_lines(names: list[str]) -> str:
    """붙임 줄: `붙임  사업 계획서 1부.  끝.`

    여럿이면 `붙임  1. … 1부.` 다음 줄부터 번호를 맞춰 `      2. … 1부.  끝.`
    """
    if len(names) == 1:
        return f"붙임  {names[0]} 1부.  끝."
    lines = [f"{i}. {name} 1부." for i, name in enumerate(names, 1)]
    return "\n".join(["붙임  " + lines[0], *(_INDENT + x for x in lines[1:])]) + "  끝."


def fill_body(body: str, names: list[str]) -> str:
    """본문의 붙임 줄(`붙임` 으로 시작하는 줄부터 끝까지)을 붙인 문서로 바꾼다.

    붙임 줄이 없으면 본문 끝의 `끝.` 을 떼고 그 뒤에 붙인다. 중간의 `다. 내 용 : 붙임 … 참조` 처럼
    `붙임` 으로 시작하지 않는 줄은 그대로 둔다. 붙인 문서가 없으면 본문을 고치지 않는다
    (손으로 적은 붙임일 수 있다).
    """
    if not names:
        return body
    lines = body.rstrip().split("\n") if body.strip() else []
    start = next((i for i, line in enumerate(lines) if line.lstrip().startswith("붙임")), None)
    if start is not None:
        lines = lines[:start]
    elif lines:
        lines[-1] = _END_RE.sub("", lines[-1])
    while lines and not lines[-1].strip():
        lines.pop()
    head = "\n".join(lines)
    return f"{head}\n\n{body_lines(names)}" if head else body_lines(names)
