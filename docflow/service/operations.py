"""운영 현황 (M11, AD_23~25) — 전체 문서 현황, 검증 오류 통계, 로그인 이력.

관리자는 문서의 **목록과 통계**만 본다. 입력값·산출물은 사유를 적고 열 때만 주고, 그 열람은
감사 로그에 남긴다(`web/admin_ops.py`). 그래서 여기 목록에는 입력값을 싣지 않는다.
"""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from docflow.service.db import Document, DocumentEvent, Form, FormVersion, LoginHistory, User, now
from docflow.validate import RULE_INFO

#: 검증 결과가 남는 이력 종류
CHECKED = ("validated", "generated")


@dataclass
class DocumentRow:
    document: Document
    owner: User | None
    #: 마지막 검증의 오류·경고 수. 검증한 적이 없으면 None
    errors: int | None
    warnings: int | None


def documents(
    session: Session,
    *,
    q: str | None = None,
    status: str | None = None,
    owner: str | None = None,
    form_id: int | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[DocumentRow], int]:
    """전체 문서 목록 (AD_23). q 는 문서 이름·서식 이름·작성자 아이디·이름에서 찾는다."""
    query = (
        select(Document, User)
        .join(FormVersion, Document.form_version_id == FormVersion.id)
        .join(Form, FormVersion.form_id == Form.id)
        .outerjoin(User, Document.owner_id == User.id)
    )
    if status:
        query = query.where(Document.status == status)
    if owner:
        query = query.where(User.login_id == owner)
    if form_id is not None:
        query = query.where(Form.id == form_id)
    for word in (q or "").split():
        like = f"%{word}%"
        query = query.where(
            or_(Document.title.like(like), Form.title.like(like), Form.form_no.like(like),
                User.login_id.like(like), User.name.like(like))  # fmt: skip
        )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    page, size = max(page, 1), min(max(size, 1), 200)
    rows = session.execute(
        query.order_by(Document.updated_at.desc(), Document.id.desc())
        .offset((page - 1) * size).limit(size)
    ).all()  # fmt: skip
    last = _last_checks(session, [d.id for d, _ in rows])
    out = []
    for doc, user in rows:
        issues = last.get(doc.id)
        errors = None if issues is None else sum(i["severity"] == "error" for i in issues)
        warnings = None if issues is None else len(issues) - errors
        out.append(DocumentRow(doc, user, errors, warnings))
    return out, total


def _last_checks(session: Session, doc_ids: list[int]) -> dict[int, list[dict]]:
    """문서마다 마지막 검증 결과 (Issue 목록)."""
    if not doc_ids:
        return {}
    latest = (
        select(DocumentEvent.document_id, func.max(DocumentEvent.id).label("id"))
        .where(DocumentEvent.document_id.in_(doc_ids), DocumentEvent.issues_json.is_not(None))
        .group_by(DocumentEvent.document_id)
        .subquery()
    )
    rows = session.execute(
        select(DocumentEvent.document_id, DocumentEvent.issues_json).join(
            latest, DocumentEvent.id == latest.c.id
        )
    )
    return {doc_id: json.loads(text) for doc_id, text in rows}


def error_stats(session: Session, *, days: int = 30, at: datetime | None = None) -> dict[str, Any]:
    """검증 오류 통계 (AD_24): 기간 안의 검증·만들기 결과를 규칙별·서식별·날짜별로.

    같은 문서를 여러 번 검증하면 그때마다 센다(검증 횟수 기준). 문서 수는 따로 센다.
    """
    since = (at or now()) - timedelta(days=days)
    rows = session.execute(
        select(DocumentEvent.document_id, DocumentEvent.at, DocumentEvent.issues_json, Form)
        .join(FormVersion, DocumentEvent.form_version_id == FormVersion.id)
        .join(Form, FormVersion.form_id == Form.id)
        .where(DocumentEvent.kind.in_(CHECKED), DocumentEvent.issues_json.is_not(None),
               DocumentEvent.at >= since)  # fmt: skip
        .order_by(DocumentEvent.at)
    ).all()
    by_rule: dict[str, Counter] = defaultdict(Counter)
    rule_docs: dict[str, set[int]] = defaultdict(set)
    by_form: dict[int, Counter] = defaultdict(Counter)
    forms: dict[int, Form] = {}
    daily: dict[str, Counter] = defaultdict(Counter)
    for doc_id, at, text, form in rows:
        issues = json.loads(text)
        errors = [i for i in issues if i["severity"] == "error"]
        day = at.date().isoformat()
        daily[day]["checks"] += 1
        daily[day]["failed"] += bool(errors)
        forms[form.id] = form
        by_form[form.id]["checks"] += 1
        by_form[form.id]["failed"] += bool(errors)
        by_form[form.id]["errors"] += len(errors)
        for issue in issues:
            by_rule[issue["code"]][issue["severity"]] += 1
            rule_docs[issue["code"]].add(doc_id)
    top = sorted(by_form.items(), key=lambda kv: (-kv[1]["errors"], -kv[1]["checks"]))[:10]
    return {
        "days": days,
        "since": since.isoformat(),
        "checks": len(rows),
        "failed": sum(c["failed"] for c in daily.values()),
        "by_rule": [
            {"code": code, "name": RULE_INFO.get(code, (code, ""))[0],
             "errors": c["error"], "warnings": c["warning"], "documents": len(rule_docs[code])}
            for code, c in sorted(by_rule.items(), key=lambda kv: -sum(kv[1].values()))
        ],  # fmt: skip
        "by_form": [
            {"id": fid, "program": forms[fid].program, "form_no": forms[fid].form_no,
             "title": forms[fid].title, **c}
            for fid, c in top
        ],  # fmt: skip
        "daily": [{"date": d, **daily[d]} for d in sorted(daily)],
        "documents": document_counts(session),
    }


def document_counts(session: Session) -> dict[str, int]:
    rows = session.execute(select(Document.status, func.count()).group_by(Document.status))
    counts = dict(rows.all())
    return {"total": sum(counts.values()), **{k: counts.get(k, 0) for k in ("draft", "completed")}}


def logins(
    session: Session,
    *,
    login_id: str | None = None,
    success: bool | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[LoginHistory], int]:
    """로그인 이력 (AD_25). 실패·잠김·없는 아이디 시도까지."""
    query = select(LoginHistory)
    if login_id:
        query = query.where(LoginHistory.login_id == login_id)
    if success is not None:
        query = query.where(LoginHistory.success.is_(success))
    if since:
        query = query.where(LoginHistory.at >= since)
    if until:
        query = query.where(LoginHistory.at < until)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    page, size = max(page, 1), min(max(size, 1), 200)
    rows = session.scalars(
        query.order_by(LoginHistory.at.desc(), LoginHistory.id.desc())
        .offset((page - 1) * size).limit(size)
    )  # fmt: skip
    return list(rows), total
