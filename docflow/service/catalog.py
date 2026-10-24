"""서식 목록 — templates/ 의 서식을 서식·버전으로 등록하고, 사용자가 고를 수 있게 찾는다.

등록(sync)은 템플릿 파일의 해시가 바뀐 서식만 새 버전을 쌓는다. 버전마다 템플릿을 파일
저장소에 복사해 두므로, templates/ 의 파일이 바뀌거나 지워져도 그 버전으로 쓴 문서는 계속 만든다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path, PurePath

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from docflow.config import Settings
from docflow.hwpx.document import HwpxDocument
from docflow.schema.extract import extract, extract_file, is_form
from docflow.schema.model import (
    SCHEMA_VERSION,
    FieldSpec,
    FormSchema,
    RepeatColumn,
    RepeatSpec,
    SlotSpec,
)
from docflow.schema.rules import FormLocation
from docflow.service.db import Document, Form, FormItem, FormVersion
from docflow.service.storage import FileStore

SORTS = ("form", "title", "recent")


class NotFound(LookupError):
    """없거나 볼 수 없는 대상."""


class Unavailable(RuntimeError):
    """지금 이 서버에서 할 수 없다 (한컴오피스가 없어 HWP 를 바꿀 수 없음 등)."""


@dataclass
class SyncReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    #: 묶음 파일을 서식별로 나누면서 목록에서 숨긴 예전 항목
    hidden: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def sync_templates(session: Session, store: FileStore, settings: Settings) -> SyncReport:
    """templates/ 의 서식을 등록한다. 새 서식은 버전 1, 바뀐 서식은 다음 버전이 된다.

    서식 여러 개를 묶은 파일은 서식 번호마다 따로 등록한다(키 `경로#1-02호`). 묶음을 통째로
    등록했던 서식은 목록에서 숨기기만 한다. 그 버전으로 쓴 문서는 그대로 열린다.
    스키마 형식(SCHEMA_VERSION)이 바뀌면 템플릿이 같아도 다시 뽑는다.
    """
    report = SyncReport()
    for path in sorted(t for t in settings.templates.rglob("*.hwpx") if is_form(t)):
        key = path.relative_to(settings.templates).as_posix()
        digest = _sha256(path)
        if any(_current(f, digest) for f in file_forms(session, key)):
            report.unchanged += 1
            continue
        try:
            schema = extract_file(path, settings)
        except Exception as exc:  # 한 건 실패가 등록 전체를 멈추지 않도록
            report.failed.append((key, str(exc)))
            continue
        register_file(session, store, key, schema, path, report)
    session.commit()
    return report


def register_file(
    session: Session,
    store: FileStore,
    key: str,
    schema: FormSchema,
    path: Path,
    report: SyncReport,
    *,
    public: bool = True,
) -> list[Form]:
    """템플릿 파일 하나를 등록한다. 등록·개정한 서식을 돌려준다.

    묶음 파일은 서식 번호마다 따로 등록하고(키 `경로#1-02호`), 같은 파일에서 나왔던 항목 중
    이번에 없는 것은 숨긴다. 새 서식은 public 대로 공개 여부를 정하고(관리자가 올린 서식은
    확인 전까지 비공개), 있던 서식은 공개 여부와 이름·분류를 그대로 둔다(관리자가 고쳤을 수 있다).
    """
    digest = _sha256(path)
    by_key = {f.key: f for f in file_forms(session, key)}
    parts = {f"{key}#{p.variants[p.variant].label}": p for p in schema.split()} or {key: schema}
    done = [
        _register(session, store, by_key.get(k), k, part, path, digest, report, public=public)
        for k, part in parts.items()
    ]
    for stale in set(by_key) - set(parts):
        by_key[stale].public = False
        report.hidden.append(stale)
    return done


def file_key(form: Form) -> str:
    """서식이 나온 템플릿 파일의 키. 묶음에서 나온 서식이면 `#` 앞이다."""
    return form.key.split("#", 1)[0]


def file_forms(session: Session, key: str) -> list[Form]:
    """템플릿 파일 하나에서 나온 서식들 (통째 항목과 서식 번호별 항목)."""
    return list(
        session.scalars(
            select(Form).where(
                or_(Form.key == key, Form.key.startswith(f"{key}#", autoescape=True))
            )
        )
    )


# ---------------------------------------------------------------- 관리자 등록·개정 (AD_01~08)
#: 관리자가 올린 서식의 키 앞머리. templates/ 에서 등록한 서식과 겹치지 않게 한다
UPLOADS = "uploads"


def register_upload(
    session: Session, store: FileStore, path: Path, name: str, location: FormLocation
) -> tuple[list[Form], SyncReport]:
    """관리자가 올린 HWPX 서식을 등록한다 (AD_01~04). 새 서식은 확인 전까지 비공개다.

    name 은 올린 파일 이름이다. 서식 번호·제목을 여기서 읽고(`[AID 서식 5-05호] 교통비 지급
    내역서.hwpx`), 같은 사업에 같은 이름을 다시 올리면 그 서식의 새 버전이 된다.
    """
    key = f"{UPLOADS}/{location.program or '기타'}/{Path(name).stem}.hwpx"
    schema = extract(HwpxDocument.open(path), stem=Path(name).stem, location=location, template=key)
    report = SyncReport()
    forms = register_file(session, store, key, schema, path, report, public=False)
    session.commit()
    return forms, report


def revise(
    session: Session, store: FileStore, form: Form, path: Path
) -> tuple[list[Form], SyncReport]:
    """서식에 새 템플릿을 올려 개정한다 (AD_07). 기존 문서는 작성 당시 버전을 계속 쓴다.

    묶음에서 나온 서식이면 같은 파일에서 나온 서식이 모두 함께 개정된다. 서식 번호·사업·분류는
    지금 등록된 값을 따른다. templates/ 에서 등록한 서식을 여기서 개정해도 templates/ 의 파일은
    그대로라, `tools/sync_forms.py` 를 다시 돌리면 그 파일이 다음 버전이 된다.
    """
    key = file_key(form)
    location = FormLocation(form.program, form.category_no, form.category, form.archived)
    schema = extract(
        HwpxDocument.open(path), stem=PurePath(key).stem, location=location, template=key
    )
    report = SyncReport()
    forms = register_file(session, store, key, schema, path, report, public=False)
    session.commit()
    return forms, report


def _current(form: Form, digest: str) -> bool:
    """이 템플릿을 지금 스키마 형식으로 이미 등록했는가."""
    latest = form.latest
    if latest is None or latest.template_sha256 != digest:
        return False
    return json.loads(latest.schema_json).get("schema_version") == SCHEMA_VERSION


def _register(
    session: Session,
    store: FileStore,
    form: Form | None,
    key: str,
    schema: FormSchema,
    path: Path,
    digest: str,
    report: SyncReport,
    *,
    public: bool,
) -> Form:
    if form is None:
        form = Form(key=key, title=schema.title, public=public)
        _apply_metadata(form, schema)
        session.add(form)
        session.flush()
        report.added.append(key)
    else:
        report.updated.append(key)
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
    return form


def _apply_metadata(form: Form, schema: FormSchema) -> None:
    form.form_no = schema.form_id
    form.title = schema.title
    form.program = schema.program
    form.category_no = schema.category_no
    form.category = schema.category
    form.archived = schema.archived


# ---------------------------------------------------------------- 조회
def load_schema(version: FormVersion, *, raw: bool = False) -> FormSchema:
    """서식 버전의 스키마. 관리자의 입력폼 설정을 덧씌운다 (raw 면 추출한 그대로)."""
    schema = FormSchema.model_validate_json(version.schema_json)
    return schema if raw else apply_items(schema, version.form.items)


# ---------------------------------------------------------------- 입력폼 설정 (AD_09~12, AD_14)
#: 관리자가 고를 수 있는 입력 형식
INPUT_TYPES = ("text", "date", "amount", "number", "checkbox")
#: 설정할 수 있는 항목 종류
ITEM_KINDS = ("field", "cell", "column")


def column_key(rep: RepeatSpec, col: RepeatColumn) -> str:
    return f"{rep.id}:{col.col}"


def apply_items(schema: FormSchema, items: list[FormItem]) -> FormSchema:
    """관리자 설정을 덧씌운 스키마. 서식에 없는 키(개정으로 사라진 칸)는 건너뛴다.

    결재란·수식 칸은 작성자 입력이 아니라 손대지 않는다. 반복영역 열은 입력 형식만 바꾼다
    (열 이름은 입력값의 키로도 쓰여 바꾸면 저장한 명단을 못 읽는다).
    """
    if not items:
        return schema
    by = {(i.kind, i.key): i for i in items}
    out = schema.model_copy(deep=True)
    for f in out.fields:
        if f.role == "input" and (item := by.get(("field", f.name))):
            _apply(f, item)
    for s in out.slots:
        if s.role == "input" and (item := by.get(("cell", s.id))):
            _apply(s, item)
    for rep in out.repeats:
        for c in rep.columns:
            item = by.get(("column", column_key(rep, c)))
            if c.role == "input" and item and item.input_type:
                c.input_type = item.input_type  # type: ignore[assignment]
    order = {i.key: i.position for i in items if i.kind == "field" and i.position is not None}
    if order:
        indexed = list(enumerate(out.fields))
        indexed.sort(key=lambda p: (p[1].name not in order, order.get(p[1].name, 0), p[0]))
        out.fields = [f for _, f in indexed]
    return out


def _apply(spec: FieldSpec | SlotSpec, item: FormItem) -> None:
    if item.hidden:
        spec.role = "fixed"
    if item.required is not None:
        spec.required = item.required
    if item.input_type:
        spec.input_type = item.input_type  # type: ignore[assignment]
    if item.label:
        spec.label = item.label


def save_items(session: Session, form: Form, settings: list[dict]) -> list[FormItem]:
    """서식의 입력폼 설정을 통째로 바꾼다. 아무것도 바꾸지 않은 항목은 저장하지 않는다."""
    schema = load_schema(form.latest, raw=True)
    known = {("field", f.name) for f in schema.fields if f.role == "input"}
    known |= {("cell", s.id) for s in schema.slots if s.role == "input"}
    known |= {("column", column_key(r, c)) for r in schema.repeats for c in r.columns
              if c.role == "input"}  # fmt: skip
    rows: list[FormItem] = []
    for raw in settings:
        kind, key = raw.get("kind"), raw.get("key")
        if (kind, key) not in known:
            raise ValueError(f"서식에 없는 항목이다: {kind} {key}")
        input_type = raw.get("input_type") or None
        if input_type is not None and input_type not in INPUT_TYPES:
            raise ValueError(f"입력 형식은 {INPUT_TYPES} 중 하나다")
        item = FormItem(
            kind=kind, key=key, hidden=bool(raw.get("hidden")), required=raw.get("required"),
            input_type=input_type, label=(raw.get("label") or "").strip() or None,
            position=raw.get("position"),
        )  # fmt: skip
        if item.hidden or item.required is not None or item.input_type or item.label or (
            item.position is not None
        ):
            rows.append(item)
    form.items = []
    session.flush()  # 같은 (종류, 키)를 다시 넣기 전에 지운다
    form.items = rows
    session.commit()
    return rows


def summarize(schema: FormSchema) -> dict[str, int]:
    """채울 항목 수 요약 (FM_08). 작성자가 채우는 자리만 센다. 떼어 낸 서식은 제 변형만 센다."""

    def mine(table: int) -> bool:
        return schema.variant is None or schema.variant_of(table) in (None, schema.variant)

    slots = [s for s in schema.input_slots() if mine(s.table)]
    return {
        "fields": sum(f.role == "input" for f in schema.fields),
        "cells": len(slots),
        "required": sum(s.required for s in slots),
        "repeats": sum(mine(r.table) for r in schema.repeats),
        "variants": len(schema.choices),
        "tables": sum(mine(t.index) for t in schema.tables),
    }


#: 템플릿 키 -> 표 배치. 버전마다 템플릿이 바뀌지 않으므로 한 번 읽으면 계속 쓴다
_LAYOUTS: dict[str, list[dict]] = {}


def layout(version: FormVersion, store: FileStore) -> list[dict]:
    """서식 버전의 표 배치 (병합 포함). 입력 화면이 서식과 같은 모양으로 칸을 그린다.

    스키마의 격자는 병합을 펼친 텍스트라 칸 크기를 모른다. 그래서 템플릿을 직접 읽는다.
    """
    key = version.template_key
    if key not in _LAYOUTS:
        doc = HwpxDocument.open(store.local_path(key))
        _LAYOUTS[key] = [
            {
                "index": t.index,
                "rows": t.rows,
                "cols": t.cols,
                "depth": t.depth,
                "cells": [
                    {
                        "row": c.row,
                        "col": c.col,
                        "row_span": c.row_span,
                        "col_span": c.col_span,
                        "text": c.text,
                    }
                    for c in t.cells
                ],
            }
            for t in doc.tables()
        ]
    return _LAYOUTS[key]


def get_form(session: Session, form_id: int, *, public_only: bool = True) -> Form:
    form = session.get(Form, form_id)
    if form is None or (public_only and not form.public) or form.latest is None:
        raise NotFound(f"서식 {form_id} 이(가) 없다")
    return form


def document_count(session: Session, version: FormVersion) -> int:
    """이 서식 버전으로 쓴 문서 수. 개정해도 이 문서들은 이 버전을 계속 쓴다."""
    query = select(func.count()).where(Document.form_version_id == version.id)
    return session.scalar(query) or 0


def listed_forms(session: Session) -> list[Form]:
    """관리자가 다루는 서식 전부 (공개·비공개). 서식별로 나눈 묶음의 통째 항목은 뺀다."""
    forms = list(session.scalars(select(Form).order_by(Form.id)))
    split = {file_key(f) for f in forms if "#" in f.key}
    return [f for f in forms if f.latest is not None and ("#" in f.key or f.key not in split)]


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
    public: bool | None = True,
    sort: str = "form",
    page: int = 1,
    size: int = 20,
) -> tuple[list[Form], int]:
    """서식 목록 (사업별·카테고리별 조회, 키워드 검색, 정렬·페이징).

    public=True 는 사용자 목록(공개 서식만), None 은 관리자 목록(공개·비공개 모두)이다. 관리자
    목록에서도 서식별로 나눈 묶음의 통째 항목은 뺀다. 고칠 것은 나눈 항목이다.
    """
    if sort not in SORTS:
        raise ValueError(f"정렬은 {SORTS} 중 하나다")
    query = select(Form)
    if public is not None:
        query = query.where(Form.public.is_(public))
    part = aliased(Form)
    query = query.where(~select(part.id).where(part.key.startswith(Form.key + "#")).exists())
    if program:
        query = query.where(Form.program == program)
    if category:
        query = query.where(Form.category == category)
    if archived is not None:
        query = query.where(Form.archived.is_(archived))
    for word in (q or "").split():
        like = f"%{word}%"
        # 경로(key)도 본다: 묶음에서 떼어 낸 서식을 원래 파일 이름(`일반기안문(샘플)`)으로 찾는다
        query = query.where(
            or_(Form.title.like(like), Form.form_no.like(like), Form.category.like(like),
                Form.key.like(like))  # fmt: skip
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
