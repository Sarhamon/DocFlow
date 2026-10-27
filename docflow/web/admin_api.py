"""관리자 API — 서식 관리 (M9, AD_01~08). 계정 관리 API 는 `app.py` 에 있다.

서식 등록은 올린 파일을 HWPX 로 바꾸고(HWP 면 한컴 변환), 스키마를 뽑아 **비공개**로 등록한다.
관리자가 미리보기로 확인한 뒤 공개하면 사용자 목록에 나타난다.
"""

from __future__ import annotations

import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from fastapi import Depends, FastAPI, File, Query, UploadFile
from fastapi import Form as Field
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from docflow.convert import Converter
from docflow.schema.labels import field_label
from docflow.schema.rules import FormLocation
from docflow.service import audit, catalog, rules, system
from docflow.service.db import Form, User
from docflow.service.documents import Serialized
from docflow.service.storage import FileStore
from docflow.validate import DEFAULT_SEVERITY, RULE_INFO, RULES, applicable_rules, field_required
from docflow.web.files import hwpx_upload

API = "/api/admin"


class FormUpdate(BaseModel):
    """서식 수정 (AD_05·AD_06). 보내지 않은 값은 그대로 둔다."""

    title: str | None = None
    program: str | None = None
    category_no: int | None = None
    category: str | None = None
    archived: bool | None = None
    public: bool | None = None


class CategoryRename(BaseModel):
    program: str
    category: str
    new: str
    category_no: int | None = None


class RuleToggle(BaseModel):
    enabled: bool


class RuleExceptions(BaseModel):
    form_ids: list[int]


class ItemSetting(BaseModel):
    """입력폼 항목 하나의 설정. 비운 값은 추출한 대로 둔다."""

    kind: Literal["field", "cell", "column"]
    key: str
    hidden: bool = False
    required: bool | None = None
    input_type: str | None = None
    label: str | None = None
    position: int | None = None


def items_view(form: Form) -> list[dict[str, Any]]:
    """입력폼 항목 목록: 추출한 값(extracted)과 관리자 설정(setting).

    누름틀(문서 순서) → 표 칸(표·행·열 순서) → 반복영역 열. 묶음에서 나온 서식은 제 변형만.
    """
    schema = catalog.load_schema(form.latest, raw=True)
    saved = {(i.kind, i.key): i for i in form.items}

    def mine(table: int) -> bool:
        return schema.variant is None or schema.variant_of(table) in (None, schema.variant)

    def row(kind: str, key: str, label: str, input_type: str, required: bool, **extra) -> dict:
        item = saved.get((kind, key))
        return {
            "kind": kind, "key": key,
            "extracted": {"label": label, "input_type": input_type, "required": required, **extra},
            "setting": {
                "hidden": item.hidden if item else False,
                "required": item.required if item else None,
                "input_type": item.input_type if item else None,
                "label": item.label if item else None,
                "position": item.position if item else None,
            },
        }  # fmt: skip

    out = [
        row("field", f.name, field_label(f.name), f.input_type, field_required(f),
            placeholder=f.placeholder)  # fmt: skip
        for f in schema.fields
        if f.role == "input"
    ]
    out += [
        row("cell", s.id, s.label, s.input_type, s.required, placeholder=s.placeholder,
            table=s.table)  # fmt: skip
        for s in schema.slots
        if s.role == "input" and mine(s.table)
    ]
    # 관리자가 입력 칸으로 바꾼 칸 (서식에서는 고정 글자였다)
    known = {s.id for s in schema.slots}
    out += [
        row("cell", x.id, "", x.input_type, False, placeholder=x.placeholder, table=x.table,
            added=True)  # fmt: skip
        for i in form.items
        if i.kind == "cell" and i.key not in known
        and (x := catalog.added_slot(schema, i)) is not None and mine(x.table)
    ]
    out += [
        row("column", catalog.column_key(r, c), c.label, c.input_type, False, table=r.table,
            repeat=r.id)  # fmt: skip
        for r in schema.repeats
        if mine(r.table)
        for c in r.columns
        if c.role == "input"
    ]
    return out


def admin_form(form: Form) -> dict[str, Any]:
    latest = form.latest
    return {
        "id": form.id,
        "key": form.key,
        "file": catalog.file_key(form),
        "form_no": form.form_no,
        "title": form.title,
        "program": form.program,
        "category_no": form.category_no,
        "category": form.category,
        "archived": form.archived,
        "public": form.public,
        "version": latest.version if latest else None,
        "updated_at": latest.created_at.isoformat() if latest else None,
    }


def report_dict(report: catalog.SyncReport) -> dict[str, Any]:
    return {"added": report.added, "updated": report.updated, "hidden": report.hidden}


def mount(
    app: FastAPI,
    *,
    session: Callable[..., Session],
    admin_user: Callable[..., User],
    store: FileStore,
    converter: Converter | None,
) -> None:
    convert = Serialized(converter) if converter is not None else None
    admins = [Depends(admin_user)]

    def to_hwpx(upload: UploadFile, work: Path) -> tuple[Path, str]:
        return hwpx_upload(upload, work, convert)

    def outcome(forms: list[Form], report: catalog.SyncReport) -> dict[str, Any]:
        return {"forms": [admin_form(f) for f in forms], "report": report_dict(report)}

    @app.get(f"{API}/forms", dependencies=admins)
    def list_forms(
        q: str | None = None,
        program: str | None = None,
        category: str | None = None,
        visibility: Literal["all", "public", "private"] = "all",
        archived: Literal["exclude", "only", "include"] = "include",
        page_no: int = Query(1, alias="page", ge=1),
        size: int = Query(50, ge=1, le=100),
        s: Session = Depends(session),
    ) -> dict[str, Any]:
        """서식 목록 (비공개 포함). 새로 올려 아직 비공개인 서식을 먼저 찾을 수 있게 한다."""
        items, total = catalog.search_forms(
            s, q=q, program=program, category=category,
            archived={"exclude": False, "only": True, "include": None}[archived],
            public={"all": None, "public": True, "private": False}[visibility],
            page=page_no, size=size,
        )  # fmt: skip
        return {"items": [admin_form(f) for f in items], "total": total, "page": page_no,
                "size": size}  # fmt: skip

    @app.get(f"{API}/forms/categories", dependencies=admins)
    def categories(s: Session = Depends(session)) -> list[dict[str, Any]]:
        """등록에서 고를 사업·카테고리 (비공개·지난 서식 포함)."""
        rows = s.execute(
            select(Form.program, Form.category_no, Form.category).distinct()
            .order_by(Form.program, Form.category_no, Form.category)
        ).all()  # fmt: skip
        return [{"program": p, "category_no": n, "category": c} for p, n, c in rows]

    @app.get(f"{API}/forms/{{form_id}}", dependencies=admins)
    def form_detail(form_id: int, s: Session = Depends(session)) -> dict[str, Any]:
        form = catalog.get_form(s, form_id, public_only=False)
        schema = catalog.load_schema(form.latest)
        siblings = [f for f in catalog.file_forms(s, catalog.file_key(form)) if f.id != form.id]
        return {
            **admin_form(form),
            "summary": catalog.summarize(schema),
            "versions": [
                {"version": v.version, "created_at": v.created_at.isoformat(),
                 "documents": catalog.document_count(s, v)}  # fmt: skip
                for v in form.versions
            ],
            "siblings": [admin_form(f) for f in siblings if "#" in f.key],
        }

    @app.patch(f"{API}/forms/{{form_id}}")
    def update_form(
        form_id: int,
        body: FormUpdate,
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ):
        form = catalog.get_form(s, form_id, public_only=False)
        changes = body.model_dump(exclude_unset=True)
        if "title" in changes and not (changes["title"] or "").strip():
            raise ValueError("서식 이름이 비었다")
        for name, value in changes.items():
            setattr(form, name, value.strip() if isinstance(value, str) else value)
        s.commit()
        audit.record(s, actor, "form.update", audit.form_name(form), changes)
        return admin_form(form)

    @app.post(f"{API}/forms", status_code=201)
    def upload_form(
        file: UploadFile = File(...),
        program: str = Field(...),
        category_no: int | None = Field(None),
        category: str = Field(""),
        archived: bool = Field(False),
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ) -> dict[str, Any]:
        """서식 등록 (AD_01~04): 올리기 → HWPX 변환 → 스키마 추출 → 비공개로 등록."""
        if not program.strip():
            raise ValueError("사업을 고른다")
        location = FormLocation(program.strip(), category_no, category.strip(), archived)
        with tempfile.TemporaryDirectory(prefix="docflow-") as tmp:
            path, name = to_hwpx(file, Path(tmp))
            forms, report = catalog.register_upload(s, store, path, name, location)
        audit.record(s, actor, "form.upload", name, report_dict(report))
        return outcome(forms, report)

    @app.get(f"{API}/forms/{{form_id}}/items", dependencies=admins)
    def form_items(form_id: int, s: Session = Depends(session)) -> dict[str, Any]:
        """입력폼 항목 (AD_09~12, AD_14): 추출한 값과 관리자 설정을 나란히 준다."""
        form = catalog.get_form(s, form_id, public_only=False)
        return items_reply(form)

    def items_reply(form: Form) -> dict[str, Any]:
        """입력폼 항목과, 표에서 칸을 고르는 데 쓰는 표 배치·고를 수 없는 자리."""
        schema = catalog.load_schema(form.latest, raw=True)
        return {
            "items": items_view(form),
            "input_types": list(catalog.INPUT_TYPES),
            "layout": catalog.layout(form.latest, store),
            "blocked": [f"t{t}.r{r}.c{c}" for t, r, c in sorted(catalog.blocked_cells(schema))],
            "variant": schema.variant,
            "variants": [v.model_dump() for v in schema.variants],
        }

    @app.put(f"{API}/forms/{{form_id}}/items")
    def save_form_items(
        form_id: int,
        body: list[ItemSetting],
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ) -> dict[str, Any]:
        """입력폼 설정을 통째로 바꾼다. 개정해도 유지되고, 작성 중인 문서에도 바로 적용된다."""
        form = catalog.get_form(s, form_id, public_only=False)
        anchors = {(t["index"], c["row"], c["col"]) for t in catalog.layout(form.latest, store)
                   for c in t["cells"]}  # fmt: skip
        saved = catalog.save_items(s, form, [b.model_dump() for b in body], anchors)
        audit.record(s, actor, "form.items", audit.form_name(form), f"설정한 항목 {len(saved)}개")
        return items_reply(form)

    # ------------------------------------------------------------ 카테고리 (AD_13)
    @app.get(f"{API}/categories", dependencies=admins)
    def category_list(s: Session = Depends(session)) -> list[dict[str, Any]]:
        """사업별 카테고리와 서식 수 (비공개·지난 서식 포함, 나눈 묶음의 통째 항목 제외)."""
        rows = s.execute(
            select(Form.program, Form.category_no, Form.category, func.count())
            .where(Form.id.in_([f.id for f in catalog.listed_forms(s)]))
            .group_by(Form.program, Form.category_no, Form.category)
            .order_by(Form.program, Form.category_no, Form.category)
        ).all()  # fmt: skip
        return [{"program": p, "category_no": n, "category": c, "forms": k} for p, n, c, k in rows]

    @app.put(f"{API}/categories")
    def rename_category(
        body: CategoryRename, s: Session = Depends(session), actor: User = Depends(admin_user)
    ) -> dict[str, Any]:
        """사업 안에서 카테고리 이름을 한꺼번에 바꾼다 (사업마다 다른 표기를 맞출 때)."""
        new = body.new.strip()
        if not new:
            raise ValueError("카테고리 이름이 비었다")
        forms = list(s.scalars(
            select(Form).where(Form.program == body.program, Form.category == body.category)
        ))  # fmt: skip
        if not forms:
            raise catalog.NotFound(f"{body.program} 에 '{body.category}' 카테고리가 없다")
        for form in forms:
            form.category = new
            if body.category_no is not None:
                form.category_no = body.category_no
        s.commit()
        change = {"new": new, "category_no": body.category_no, "forms": len(forms)}
        audit.record(s, actor, "category.rename", f"{body.program} {body.category}", change)
        return {"program": body.program, "category": new, "forms": len(forms)}

    # ------------------------------------------------------------ 검증 규칙 (AD_15~17)
    @app.get(f"{API}/rules", dependencies=admins)
    def rule_list(s: Session = Depends(session)) -> list[dict[str, Any]]:
        """규칙과 적용 서식 수, 전체 on/off, 예외 서식 (AD_15)."""
        forms = catalog.listed_forms(s)
        applies = {code: 0 for code in RULES}
        for form in forms:
            for code in applicable_rules(catalog.load_schema(form.latest)):
                applies[code] += 1
        off = rules.disabled(s)
        return [
            {
                "code": code,
                "name": RULE_INFO[code][0],
                "description": RULE_INFO[code][1],
                "severity": DEFAULT_SEVERITY.get(code, "error"),
                "enabled": code not in off,
                "applies": applies[code],
                "forms": len(forms),
                "exceptions": [admin_form(f) for f in rules.exceptions(s, code)],
            }
            for code in RULES
        ]

    @app.put(f"{API}/rules/{{code}}")
    def rule_toggle(
        code: str,
        body: RuleToggle,
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ) -> dict:
        rules.set_enabled(s, code, body.enabled)
        audit.record(s, actor, "rule.toggle", code, "켬" if body.enabled else "끔")
        return {"code": code, "enabled": body.enabled}

    @app.put(f"{API}/rules/{{code}}/exceptions")
    def rule_exceptions(
        code: str,
        body: RuleExceptions,
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ) -> list[dict[str, Any]]:
        """이 규칙을 끌 서식을 통째로 정한다 (AD_17)."""
        forms = rules.set_exceptions(s, code, body.form_ids)
        audit.record(s, actor, "rule.exceptions", code, [audit.form_name(f) for f in forms])
        return [admin_form(f) for f in forms]

    # ------------------------------------------------------------ 시스템 (AD_27·AD_28)
    @app.get(f"{API}/system", dependencies=admins)
    def system_status(s: Session = Depends(session)) -> dict[str, Any]:
        """변환기와 LLM 서버 상태. LLM 은 연결만 확인한다 (쓰는 것은 M10)."""
        llm = system.get_settings(s)
        return {
            "converter": {
                "available": converter is not None,
                "name": type(converter).__name__ if converter is not None else None,
            },
            "llm": {**llm, "status": system.llm_status(llm["llm_url"], llm["llm_model"])},
        }

    @app.put(f"{API}/system")
    def system_save(
        body: dict[str, str], s: Session = Depends(session), actor: User = Depends(admin_user)
    ) -> dict[str, Any]:
        saved = system.save_settings(s, body)
        audit.record(s, actor, "system.save", ", ".join(body), body)
        return saved

    @app.post(f"{API}/forms/{{form_id}}/versions", status_code=201)
    def revise_form(
        form_id: int,
        file: UploadFile = File(...),
        s: Session = Depends(session),
        actor: User = Depends(admin_user),
    ) -> dict[str, Any]:
        """서식 개정 (AD_07): 새 템플릿을 다음 버전으로. 기존 문서는 작성 당시 버전을 쓴다."""
        form = catalog.get_form(s, form_id, public_only=False)
        with tempfile.TemporaryDirectory(prefix="docflow-") as tmp:
            path, name = to_hwpx(file, Path(tmp))
            forms, report = catalog.revise(s, store, form, path)
        audit.record(s, actor, "form.revise", audit.form_name(form),
                     {"file": name, **report_dict(report)})  # fmt: skip
        return outcome(forms, report)
