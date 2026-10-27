"""관리자 API — 운영 현황 (M11, AD_23~26): 문서 현황, 오류 통계, 로그인 이력, 감사 로그.

관리자는 남의 문서를 **목록으로만** 본다. 입력값과 산출물은 사유를 적고 열 때만 주고,
열람·내려받기는 감사 로그에 남긴다 (명세 8장 결정).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from fastapi import Depends, FastAPI, Query, Response
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from docflow.service import audit, catalog, crosscheck, crypto, operations, review
from docflow.service.db import ChecklistItem, CrossRule, Document, Form, User
from docflow.service.documents import DocumentService
from docflow.service.storage import FileStore
from docflow.web.files import download_response

API = "/api/admin"
#: 열람 사유는 이보다 짧으면 받지 않는다
MIN_REASON = 5


class OpenRequest(BaseModel):
    reason: str = Field(max_length=300)


def _reason(text: str) -> str:
    text = " ".join(text.split())
    if len(text) < MIN_REASON:
        raise ValueError(f"열람 사유를 {MIN_REASON}자 이상 적는다")
    return text


def _utc(at: datetime | None) -> datetime | None:
    """화면이 보낸 시각(시간대 있음)을 DB 기준(UTC, 시간대 없음)으로."""
    if at is None or at.tzinfo is None:
        return at
    return at.astimezone(UTC).replace(tzinfo=None)


def doc_row(row: operations.DocumentRow) -> dict[str, Any]:
    doc, owner = row.document, row.owner
    form = doc.form_version.form
    return {
        "id": doc.id,
        "title": doc.title,
        "status": doc.status,
        "revision": doc.revision,
        "owner": {"login_id": owner.login_id, "name": owner.name} if owner else None,
        "form": {"id": form.id, "program": form.program, "form_no": form.form_no,
                 "title": form.title, "version": doc.form_version.version},  # fmt: skip
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "errors": row.errors,
        "warnings": row.warnings,
    }


def doc_target(doc: Document, owner: User | None) -> str:
    return f"문서 {doc.id} '{doc.title}' (작성자 {owner.login_id if owner else '-'})"


def mount(
    app: FastAPI,
    *,
    session: Callable[..., Session],
    admin_user: Callable[..., User],
    store: FileStore,
) -> None:
    admins = [Depends(admin_user)]

    # ------------------------------------------------------------ 문서 현황 (AD_23)
    @app.get(f"{API}/documents", dependencies=admins)
    def document_list(
        q: str | None = None,
        status: str | None = None,
        owner: str | None = None,
        form_id: int | None = None,
        page_no: int = Query(1, alias="page", ge=1),
        size: int = Query(50, ge=1, le=200),
        s: Session = Depends(session),
    ) -> dict[str, Any]:
        """전체 문서 목록. 입력값은 싣지 않는다."""
        rows, total = operations.documents(
            s, q=q, status=status, owner=owner, form_id=form_id, page=page_no, size=size
        )
        return {"items": [doc_row(r) for r in rows], "total": total, "page": page_no,
                "size": size, "counts": operations.document_counts(s)}  # fmt: skip

    @app.post(f"{API}/documents/{{doc_id}}/open")
    def open_document(
        doc_id: int,
        body: OpenRequest,
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ) -> dict[str, Any]:
        """사유를 적고 문서를 연다: 입력값·이력·산출물 목록. 열람을 감사 로그에 남긴다."""
        reason = _reason(body.reason)
        doc = DocumentService(s, store).get(doc_id)
        owner = s.get(User, doc.owner_id) if doc.owner_id else None
        audit.record(s, actor, "document.view", doc_target(doc, owner), reason)
        files: dict[int, list[str]] = {}
        for a in doc.artifacts:
            files.setdefault(a.revision, []).append(a.format)
        return {
            **doc_row(operations.DocumentRow(doc, owner, None, None)),
            # 주민등록번호는 가린다. 원문이 필요하면 산출물을 사유와 함께 내려받는다
            "input": json.loads(crypto.mask_rrn(doc.input_json or "{}")),
            "files": [{"revision": r, "formats": sorted(f)} for r, f in sorted(files.items())],
            "history": [
                {"kind": e.kind, "at": e.at.isoformat(), "note": e.note,
                 "issues": json.loads(e.issues_json) if e.issues_json else None}
                for e in doc.events
            ],  # fmt: skip
        }

    @app.get(f"{API}/documents/{{doc_id}}/files/{{fmt}}")
    def download_document(
        doc_id: int,
        fmt: str,
        reason: str,
        revision: int | None = None,
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ) -> Response:
        """남의 문서 산출물 내려받기. 사유를 받고 감사 로그에 남긴다."""
        reason = _reason(reason)
        svc = DocumentService(s, store)
        data, name = svc.artifact(doc_id, fmt, revision=revision)
        doc = svc.get(doc_id)
        owner = s.get(User, doc.owner_id) if doc.owner_id else None
        audit.record(s, actor, "document.download", doc_target(doc, owner),
                     f"{name} — {reason}")  # fmt: skip
        return download_response(data, name)

    # ------------------------------------------------------------ 오류 통계 (AD_24)
    @app.get(f"{API}/stats", dependencies=admins)
    def stats(days: int = Query(30, ge=1, le=365), s: Session = Depends(session)):
        return operations.error_stats(s, days=days)

    # ------------------------------------------------------------ 로그인 이력 (AD_25)
    @app.get(f"{API}/logins", dependencies=admins)
    def login_list(
        login_id: str | None = None,
        success: bool | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        page_no: int = Query(1, alias="page", ge=1),
        size: int = Query(50, ge=1, le=200),
        s: Session = Depends(session),
    ) -> dict[str, Any]:
        rows, total = operations.logins(
            s, login_id=login_id, success=success, since=_utc(since), until=_utc(until),
            page=page_no, size=size,
        )  # fmt: skip
        items = [
            {"at": r.at.isoformat(), "login_id": r.login_id, "success": r.success,
             "fail_reason": r.fail_reason, "ip": r.ip, "user_agent": r.user_agent}
            for r in rows
        ]  # fmt: skip
        return {"items": items, "total": total, "page": page_no, "size": size}

    # ------------------------------------------------------------ 감사 로그 (AD_26)
    @app.get(f"{API}/audit", dependencies=admins)
    def audit_list(
        action: str | None = None,
        actor: str | None = None,
        q: str | None = None,
        since: datetime | None = None,
        until: datetime | None = None,
        page_no: int = Query(1, alias="page", ge=1),
        size: int = Query(50, ge=1, le=200),
        s: Session = Depends(session),
    ) -> dict[str, Any]:
        rows, total = audit.search(
            s, action=action, actor=actor, q=q, since=_utc(since), until=_utc(until),
            page=page_no, size=size,
        )  # fmt: skip
        return {"items": [audit.log_dict(r) for r in rows], "total": total, "page": page_no,
                "size": size, "actions": audit.ACTIONS}  # fmt: skip


# ---------------------------------------------------------------- 문서 간 대조 규칙 (VA_12)
class CrossRuleBody(BaseModel):
    name: str = Field(max_length=200)
    source_form_id: int
    source_keys: list[str]
    target_form_id: int
    target_keys: list[str]
    op: str = "eq"
    enabled: bool = True


def cross_dict(rule: CrossRule) -> dict[str, Any]:
    source, target = crosscheck.rule_keys(rule)

    def side(form: Form, keys: list[str]) -> dict[str, Any]:
        names = crosscheck.input_keys(catalog.load_schema(form.latest))
        info = {"id": form.id, "program": form.program, "form_no": form.form_no,
                "title": form.title}  # fmt: skip
        labels = [{"key": k, "label": names.get(k, f"{k} (서식에서 사라짐)")} for k in keys]
        return {"form": info, "keys": labels}

    return {
        "id": rule.id,
        "name": rule.name,
        "op": rule.op,
        "op_name": crosscheck.OPS[rule.op],
        "enabled": rule.enabled,
        "source": side(rule.source_form, source),
        "target": side(rule.target_form, target),
    }


def mount_cross(
    app: FastAPI, *, session: Callable[..., Session], admin_user: Callable[..., User]
) -> None:
    admins = [Depends(admin_user)]

    @app.get(f"{API}/cross-rules", dependencies=admins)
    def cross_list(s: Session = Depends(session)) -> list[dict[str, Any]]:
        return [cross_dict(r) for r in s.scalars(select(CrossRule).order_by(CrossRule.id))]

    @app.get(f"{API}/cross-rules/keys", dependencies=admins)
    def cross_keys(form_id: int, s: Session = Depends(session)) -> list[dict[str, str]]:
        """규칙에 고를 수 있는 서식 항목 (누름틀·표 칸·명단 열)."""
        form = catalog.get_form(s, form_id, public_only=False)
        names = crosscheck.input_keys(catalog.load_schema(form.latest))
        return [{"key": k, "label": v} for k, v in names.items()]

    def save(rule: CrossRule | None, body: CrossRuleBody, s: Session, actor: User) -> dict:
        rule = crosscheck.save_rule(s, rule, body.model_dump())
        out = cross_dict(rule)
        detail = {
            side: f"{out[side]['form']['title']} {[k['label'] for k in out[side]['keys']]}"
            for side in ("source", "target")
        }
        audit.record(s, actor, "cross.save", rule.name,
                     {**detail, "op": rule.op, "enabled": rule.enabled})  # fmt: skip
        return out

    @app.post(f"{API}/cross-rules", status_code=201)
    def cross_create(
        body: CrossRuleBody, s: Session = Depends(session), actor: User = Depends(admin_user)
    ) -> dict[str, Any]:
        return save(None, body, s, actor)

    @app.put(f"{API}/cross-rules/{{rule_id}}")
    def cross_update(
        rule_id: int,
        body: CrossRuleBody,
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ) -> dict[str, Any]:
        return save(_rule(s, rule_id), body, s, actor)

    @app.delete(f"{API}/cross-rules/{{rule_id}}", status_code=204)
    def cross_delete(
        rule_id: int, s: Session = Depends(session), actor: User = Depends(admin_user)
    ) -> None:
        rule = _rule(s, rule_id)
        name = rule.name
        crosscheck.delete_rule(s, rule)
        audit.record(s, actor, "cross.save", name, "삭제")


def _rule(s: Session, rule_id: int) -> CrossRule:
    rule = s.get(CrossRule, rule_id)
    if rule is None:
        raise catalog.NotFound(f"대조 규칙 {rule_id} 이(가) 없다")
    return rule


# ---------------------------------------------------------------- 정성 판정 체크리스트 (ER_05)
class ChecklistBody(BaseModel):
    text: str = Field(max_length=review.MAX_TEXT)
    #: 없으면 모든 서식에 묻는다
    form_id: int | None = None
    enabled: bool = True


def checklist_dict(item: ChecklistItem) -> dict[str, Any]:
    form = item.form
    return {
        "id": item.id,
        "text": item.text,
        "enabled": item.enabled,
        "form": {"id": form.id, "program": form.program, "form_no": form.form_no,
                 "title": form.title} if form else None,  # fmt: skip
    }


def mount_checklist(
    app: FastAPI, *, session: Callable[..., Session], admin_user: Callable[..., User]
) -> None:
    admins = [Depends(admin_user)]

    @app.get(f"{API}/checklist", dependencies=admins)
    def checklist_list(s: Session = Depends(session)) -> list[dict[str, Any]]:
        return [checklist_dict(i) for i in review.all_items(s)]

    def save(item: ChecklistItem | None, body: ChecklistBody, s: Session, actor: User) -> dict:
        item = review.save_item(s, item, **body.model_dump())
        out = checklist_dict(item)
        scope = audit.form_name(item.form) if item.form else "모든 서식"
        audit.record(s, actor, "checklist.save", item.text[:300],
                     {"scope": scope, "enabled": item.enabled})  # fmt: skip
        return out

    @app.post(f"{API}/checklist", status_code=201)
    def checklist_create(
        body: ChecklistBody, s: Session = Depends(session), actor: User = Depends(admin_user)
    ) -> dict[str, Any]:
        return save(None, body, s, actor)

    @app.put(f"{API}/checklist/{{item_id}}")
    def checklist_update(
        item_id: int,
        body: ChecklistBody,
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ) -> dict[str, Any]:
        return save(_item(s, item_id), body, s, actor)

    @app.delete(f"{API}/checklist/{{item_id}}", status_code=204)
    def checklist_delete(
        item_id: int, s: Session = Depends(session), actor: User = Depends(admin_user)
    ) -> None:
        item = _item(s, item_id)
        text = item.text
        review.delete_item(s, item)
        audit.record(s, actor, "checklist.save", text[:300], "삭제")


def _item(s: Session, item_id: int) -> ChecklistItem:
    item = s.get(ChecklistItem, item_id)
    if item is None:
        raise catalog.NotFound(f"체크리스트 항목 {item_id} 이(가) 없다")
    return item
