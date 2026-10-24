"""검증 규칙 관리 (M9, AD_15~17) — 관리자 설정을 검증 설정(`ValidationConfig`)으로 바꾼다.

규칙은 기본으로 모두 켜져 있다. 관리자가 전체로 끄거나(AD_16), 특정 서식에서만 끈다(AD_17).
실시간 점검·완료 검증·문서 만들기가 모두 이 설정을 쓴다.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from docflow.service.db import Form, RuleSetting
from docflow.validate import RULES, RuleConfig, ValidationConfig


def _check(code: str) -> None:
    if code not in RULES:
        raise ValueError(f"없는 규칙이다: {code} (가능: {', '.join(RULES)})")


def disabled(session: Session, form_id: int | None = None) -> frozenset[str]:
    """꺼진 규칙. form_id 를 주면 그 서식의 예외까지 합친다."""
    rows = session.scalars(
        select(RuleSetting).where(
            RuleSetting.enabled.is_(False),
            (RuleSetting.form_id.is_(None) | (RuleSetting.form_id == form_id))
            if form_id is not None
            else RuleSetting.form_id.is_(None),
        )
    )
    return frozenset(r.code for r in rows)


def config_for(session: Session, form_id: int) -> ValidationConfig:
    """서식 하나의 검증 설정."""
    return ValidationConfig(default=RuleConfig(disabled=disabled(session, form_id)))


def set_enabled(session: Session, code: str, enabled: bool) -> None:
    """규칙을 전체로 켜고 끈다 (AD_16)."""
    _check(code)
    row = session.scalar(
        select(RuleSetting).where(RuleSetting.code == code, RuleSetting.form_id.is_(None))
    )
    if enabled and row is not None:
        session.delete(row)
    elif not enabled and row is None:
        session.add(RuleSetting(code=code, enabled=False))
    session.commit()


def exceptions(session: Session, code: str) -> list[Form]:
    """이 규칙을 끈 서식들 (AD_17)."""
    _check(code)
    query = (
        select(Form)
        .join(RuleSetting, RuleSetting.form_id == Form.id)
        .where(RuleSetting.code == code, RuleSetting.enabled.is_(False))
        .order_by(Form.program, Form.form_no, Form.id)
    )
    return list(session.scalars(query))


def set_exceptions(session: Session, code: str, form_ids: list[int]) -> list[Form]:
    """이 규칙을 끌 서식을 통째로 정한다 (AD_17)."""
    _check(code)
    forms = [session.get(Form, i) for i in dict.fromkeys(form_ids)]
    if missing := [i for i, f in zip(form_ids, forms, strict=False) if f is None]:
        raise ValueError(f"없는 서식이다: {missing}")
    for row in session.scalars(
        select(RuleSetting).where(RuleSetting.code == code, RuleSetting.form_id.is_not(None))
    ):
        session.delete(row)
    session.flush()
    for form in forms:
        session.add(RuleSetting(code=code, form_id=form.id, enabled=False))
    session.commit()
    return exceptions(session, code)
