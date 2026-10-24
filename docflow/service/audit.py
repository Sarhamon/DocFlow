"""감사 로그 (M11, AD_26) — 관리자가 무엇을 바꾸고 누구의 문서를 왜 열었는지.

API 가 변경을 마친 뒤 기록한다. 비밀번호·초기 비밀번호는 적지 않는다.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docflow.service.db import AuditLog, Form, User

#: 행위 코드 -> 화면에 보일 이름
ACTIONS = {
    "user.create": "계정 발급",
    "user.update": "계정 정보 수정",
    "user.reset_password": "비밀번호 초기화",
    "user.unlock": "잠금 해제",
    "user.status": "계정 상태 변경",
    "form.upload": "서식 등록",
    "form.revise": "서식 개정",
    "form.update": "서식 정보 수정",
    "form.items": "입력폼 수정",
    "category.rename": "카테고리 이름 변경",
    "rule.toggle": "규칙 켜기·끄기",
    "rule.exceptions": "규칙 예외 서식",
    "cross.save": "문서 간 대조 규칙",
    "system.save": "시스템 설정",
    "document.view": "문서 열람",
    "document.download": "문서 내려받기",
}


def form_name(form: Form) -> str:
    return " ".join(x for x in (form.program, form.form_no, form.title) if x)


def record(
    session: Session, actor: User, action: str, target: str = "", detail: Any = ""
) -> AuditLog:
    """한 건 남기고 커밋한다. detail 이 글자가 아니면 JSON 으로 적는다."""
    if action not in ACTIONS:
        raise ValueError(f"없는 감사 행위다: {action}")
    text = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)
    row = AuditLog(actor_id=actor.id, actor_login=actor.login_id, action=action,
                   target=target[:300], detail=text)  # fmt: skip
    session.add(row)
    session.commit()
    return row


def search(
    session: Session,
    *,
    action: str | None = None,
    actor: str | None = None,
    q: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    page: int = 1,
    size: int = 50,
) -> tuple[list[AuditLog], int]:
    query = select(AuditLog)
    if action:
        query = query.where(AuditLog.action == action)
    if actor:
        query = query.where(AuditLog.actor_login == actor)
    if q:
        like = f"%{q}%"
        query = query.where(AuditLog.target.like(like) | AuditLog.detail.like(like))
    if since:
        query = query.where(AuditLog.at >= since)
    if until:
        query = query.where(AuditLog.at < until)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    page, size = max(page, 1), min(max(size, 1), 200)
    rows = session.scalars(
        query.order_by(AuditLog.at.desc(), AuditLog.id.desc()).offset((page - 1) * size).limit(size)
    )
    return list(rows), total


def log_dict(row: AuditLog) -> dict[str, Any]:
    return {
        "id": row.id,
        "at": row.at.isoformat(),
        "actor": row.actor_login,
        "action": row.action,
        "action_name": ACTIONS.get(row.action, row.action),
        "target": row.target,
        "detail": row.detail,
    }
