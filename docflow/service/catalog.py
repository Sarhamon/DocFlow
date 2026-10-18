"""서식 목록 — templates/ 의 서식을 서식·버전으로 등록하고, 사용자가 고를 수 있게 찾는다.

등록(sync)은 템플릿 파일의 해시가 바뀐 서식만 새 버전을 쌓는다. 버전마다 템플릿을 파일
저장소에 복사해 두므로, templates/ 의 파일이 바뀌거나 지워져도 그 버전으로 쓴 문서는 계속 만든다.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from docflow.config import Settings
from docflow.schema.extract import extract_file, is_form
from docflow.schema.model import FormSchema
from docflow.service.db import Form, FormVersion
from docflow.service.storage import FileStore

SORTS = ("form", "title", "recent")


class NotFound(LookupError):
    """없거나 볼 수 없는 대상."""


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    failed: list[tuple[str, str]] = field(default_factory=list)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sync_templates(session: Session, store: FileStore, settings: Settings) -> SyncReport:
    """templates/ 의 서식을 등록한다. 새 서식은 버전 1, 바뀐 서식은 다음 버전이 된다."""
    report = SyncReport()
    for path in sorted(t for t in settings.templates.rglob("*.hwpx") if is_form(t)):
        key = path.relative_to(settings.templates).as_posix()
        digest = _sha256(path)
        form = session.scalar(select(Form).where(Form.key == key))
        if form is not None and form.latest and form.latest.template_sha256 == digest:
            report.unchanged += 1
            continue
        try:
            schema = extract_file(path, settings)
        except Exception as exc:  # 한 건 실패가 등록 전체를 멈추지 않도록
            report.failed.append((key, str(exc)))
            continue
        if form is None:
            form = Form(key=key, title=schema.title)
            session.add(form)
            session.flush()
            report.added.append(key)
        else:
            report.updated.append(key)
        _apply_metadata(form, schema)
        number = (form.latest.version if form.latest else 0) + 1
        template_key = f"forms/{form.id}/v{number}.hwpx"
        store.put(template_key, path)
        form.versions.append(
            FormVersion(
                version=number,
                template_key=template_key,
                template_sha256=digest,
                schema_json=schema.model_dump_json(),
            )
        )
    session.commit()
    return report


def _apply_metadata(form: Form, schema: FormSchema) -> None:
    form.form_no = schema.form_id
    form.title = schema.title
    form.program = schema.program
    form.category_no = schema.category_no
    form.category = schema.category
    form.archived = schema.archived


# ---------------------------------------------------------------- 조회
def load_schema(version: FormVersion) -> FormSchema:
    return FormSchema.model_validate_json(version.schema_json)


def summarize(schema: FormSchema) -> dict[str, int]:
    """채울 항목 수 요약 (FM_08). 작성자가 채우는 자리만 센다."""
    slots = schema.input_slots()
    return {
        "fields": sum(f.role == "input" for f in schema.fields),
        "cells": len(slots),
        "required": sum(s.required for s in slots),
        "repeats": len(schema.repeats),
        "variants": len(schema.variants),
        "tables": len(schema.tables),
    }


def get_form(session: Session, form_id: int, *, public_only: bool = True) -> Form:
    form = session.get(Form, form_id)
    if form is None or (public_only and not form.public) or form.latest is None:
        raise NotFound(f"서식 {form_id} 이(가) 없다")
    return form


def get_version(form: Form, version: int | None = None) -> FormVersion:
    if version is None:
        return form.latest
    for v in form.versions:
        if v.version == version:
            return v
    raise NotFound(f"서식 {form.id} 에 버전 {version} 이(가) 없다")


def search_forms(
    session: Session,
    *,
    q: str | None = None,
    program: str | None = None,
    category: str | None = None,
    archived: bool | None = False,
    sort: str = "form",
    page: int = 1,
    size: int = 20,
) -> tuple[list[Form], int]:
    """서식 목록 (사업별·카테고리별 조회, 키워드 검색, 정렬·페이징). 공개 서식만."""
    if sort not in SORTS:
        raise ValueError(f"정렬은 {SORTS} 중 하나다")
    query = select(Form).where(Form.public.is_(True))
    if program:
        query = query.where(Form.program == program)
    if category:
        query = query.where(Form.category == category)
    if archived is not None:
        query = query.where(Form.archived.is_(archived))
    for word in (q or "").split():
        like = f"%{word}%"
        query = query.where(
            or_(Form.title.like(like), Form.form_no.like(like), Form.category.like(like))
        )
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    if sort == "title":
        query = query.order_by(Form.title, Form.id)
    elif sort == "recent":
        latest = (
            select(func.max(FormVersion.created_at))
            .where(FormVersion.form_id == Form.id)
            .scalar_subquery()
        )
        query = query.order_by(latest.desc(), Form.id)
    else:
        query = query.order_by(Form.program, Form.category_no, Form.form_no, Form.id)
    page, size = max(page, 1), min(max(size, 1), 100)
    items = list(session.scalars(query.offset((page - 1) * size).limit(size)))
    return items, total


def facets(session: Session) -> list[dict]:
    """사업 -> 카테고리별 서식 수 (공개·현행 서식). 목록 화면의 필터가 쓴다."""
    rows = session.execute(
        select(Form.program, Form.category_no, Form.category, func.count())
        .where(Form.public.is_(True), Form.archived.is_(False))
        .group_by(Form.program, Form.category_no, Form.category)
        .order_by(Form.program, Form.category_no)
    ).all()
    out: dict[str, dict] = {}
    for program, number, category, count in rows:
        entry = out.setdefault(program, {"program": program, "count": 0, "categories": []})
        entry["count"] += count
        entry["categories"].append({"no": number, "name": category, "count": count})
    return list(out.values())
