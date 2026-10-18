"""DocFlow 웹 API (M6). 화면(M8)과 로그인(M7)은 이 API 위에 올린다.

    uvicorn --factory docflow.web.app:create_app          # 개발 서버
    python tools/sync_forms.py                            # templates/ 의 서식을 먼저 등록

경로는 모두 /api 아래다. 오류는 {"detail": ...} 로 돌려주고, 검증 오류(422)는 오류 목록을 함께 준다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from docflow.config import Settings, get_settings
from docflow.convert import Converter
from docflow.generate import GenerateError
from docflow.pipeline import InputError, skeleton
from docflow.service import catalog
from docflow.service.catalog import NotFound
from docflow.service.db import Document, DocumentEvent, Form, connect
from docflow.service.documents import (
    Conflict,
    DocumentService,
    GenerateOutcome,
    ValidationFailed,
    issue_dict,
)
from docflow.service.storage import FileStore, LocalFileStore

API = "/api"


# ---------------------------------------------------------------- 요청 본문
class NewDocument(BaseModel):
    form_id: int
    title: str | None = None


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class GenerateRequest(BaseModel):
    #: 변환할 형식. 비우면 HWPX 만 만든다
    formats: list[str] = Field(default_factory=lambda: ["HWP", "PDF"])
    #: 검증 오류가 있어도 만든다
    force: bool = False


# ---------------------------------------------------------------- 응답 모양
def form_summary(form: Form) -> dict[str, Any]:
    latest = form.latest
    return {
        "id": form.id,
        "form_no": form.form_no,
        "title": form.title,
        "program": form.program,
        "category_no": form.category_no,
        "category": form.category,
        "archived": form.archived,
        "version": latest.version if latest else None,
    }


def document_summary(doc: Document) -> dict[str, Any]:
    version = doc.form_version
    return {
        "id": doc.id,
        "title": doc.title,
        "status": doc.status,
        "revision": doc.revision,
        "form": {**form_summary(version.form), "version": version.version},
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "completed_at": doc.completed_at.isoformat() if doc.completed_at else None,
        "files": sorted({a.format for a in doc.artifacts if a.revision == doc.revision}),
    }


def event_dict(e: DocumentEvent) -> dict[str, Any]:
    return {
        "id": e.id,
        "kind": e.kind,
        "at": e.at.isoformat(),
        "form_version_id": e.form_version_id,
        "note": e.note,
        "input": json.loads(e.input_json) if e.input_json else None,
        "issues": json.loads(e.issues_json) if e.issues_json else None,
        "rules": json.loads(e.rules_json) if e.rules_json else None,
    }


def outcome_dict(out: GenerateOutcome) -> dict[str, Any]:
    return {
        "document": document_summary(out.document),
        "revision": out.revision,
        "issues": [issue_dict(i) for i in out.issues],
        "leftovers": [{"kind": x.kind, "id": x.id, "text": x.text} for x in out.leftovers],
        "conversion_errors": out.conversion_errors,
        "skipped_formulas": [{"cell": c, "reason": r} for c, r in out.skipped_formulas],
    }


def page(items: list[dict], total: int, number: int, size: int) -> dict[str, Any]:
    return {"items": items, "total": total, "page": number, "size": size}


# ---------------------------------------------------------------- 앱
def create_app(
    settings: Settings | None = None,
    *,
    converter: Converter | None | str = "auto",
    store: FileStore | None = None,
    database_url: str | None = None,
) -> FastAPI:
    """앱을 만든다. converter 가 "auto" 면 한컴오피스가 있을 때만 한컴 변환기를 쓴다."""
    settings = settings or get_settings()
    settings.data.mkdir(parents=True, exist_ok=True)
    sessions: sessionmaker = connect(database_url or settings.database_url)
    store = store or LocalFileStore(settings.data / "files")
    if converter == "auto":
        from docflow.adapters.hancom import HancomConverter, available

        converter = HancomConverter() if available() else None

    app = FastAPI(title="DocFlow", version="0.6")
    app.state.sessions = sessions
    app.state.store = store
    app.state.converter = converter
    app.state.settings = settings
    _errors(app)

    def session() -> Iterator[Session]:
        with sessions() as s:
            yield s

    def service(s: Session = Depends(session)) -> DocumentService:
        return DocumentService(s, store, converter)

    def owner() -> int | None:
        """요청한 계정. 로그인(M7) 전에는 None 이라 계정 구분이 없다."""
        return None

    # ------------------------------------------------------------ 서식
    @app.get(f"{API}/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "converter": converter is not None}

    @app.get(f"{API}/forms")
    def list_forms(
        q: str | None = None,
        program: str | None = None,
        category: str | None = None,
        archived: Literal["exclude", "only", "include"] = "exclude",
        sort: str = "form",
        page_no: int = Query(1, alias="page", ge=1),
        size: int = Query(20, ge=1, le=100),
        s: Session = Depends(session),
    ) -> dict[str, Any]:
        items, total = catalog.search_forms(
            s, q=q, program=program, category=category,
            archived={"exclude": False, "only": True, "include": None}[archived],
            sort=sort, page=page_no, size=size,
        )  # fmt: skip
        return page([form_summary(f) for f in items], total, page_no, size)

    @app.get(f"{API}/forms/facets")
    def form_facets(s: Session = Depends(session)) -> list[dict]:
        return catalog.facets(s)

    @app.get(f"{API}/forms/{{form_id}}")
    def form_detail(form_id: int, s: Session = Depends(session)) -> dict[str, Any]:
        form = catalog.get_form(s, form_id)
        schema = catalog.load_schema(form.latest)
        return {
            **form_summary(form),
            "summary": catalog.summarize(schema),
            "variants": [{"index": v.index, "label": v.label} for v in schema.variants],
            "versions": [
                {"version": v.version, "created_at": v.created_at.isoformat()}
                for v in form.versions
            ],
        }

    @app.get(f"{API}/forms/{{form_id}}/schema")
    def form_schema(
        form_id: int, version: int | None = None, s: Session = Depends(session)
    ) -> dict[str, Any]:
        form = catalog.get_form(s, form_id)
        return catalog.load_schema(catalog.get_version(form, version)).model_dump()

    @app.get(f"{API}/forms/{{form_id}}/skeleton")
    def form_skeleton(
        form_id: int, variant: int | None = None, s: Session = Depends(session)
    ) -> dict[str, Any]:
        form = catalog.get_form(s, form_id)
        return skeleton(catalog.load_schema(form.latest), variant)

    # ------------------------------------------------------------ 문서
    @app.post(f"{API}/documents", status_code=201)
    def create_document(
        body: NewDocument, svc: DocumentService = Depends(service), me=Depends(owner)
    ) -> dict[str, Any]:
        return document_summary(svc.create(body.form_id, me, title=body.title))

    @app.get(f"{API}/documents")
    def list_documents(
        status: str | None = None,
        page_no: int = Query(1, alias="page", ge=1),
        size: int = Query(20, ge=1, le=100),
        svc: DocumentService = Depends(service),
        me=Depends(owner),
    ) -> dict[str, Any]:
        items, total = svc.list(me, status=status, page=page_no, size=size)
        return page([document_summary(d) for d in items], total, page_no, size)

    @app.get(f"{API}/documents/{{doc_id}}")
    def get_document(
        doc_id: int, svc: DocumentService = Depends(service), me=Depends(owner)
    ) -> dict[str, Any]:
        doc = svc.get(doc_id, me)
        return {**document_summary(doc), "input": svc.raw_input(doc)}

    @app.get(f"{API}/documents/{{doc_id}}/form")
    def document_form(
        doc_id: int, svc: DocumentService = Depends(service), me=Depends(owner)
    ) -> dict[str, Any]:
        """문서가 묶인 서식 버전의 스키마와 입력 틀. 입력 화면이 쓴다."""
        doc = svc.get(doc_id, me)
        schema = svc.schema_of(doc)
        variant = svc.raw_input(doc).get("variant")
        index = next((v.index for v in schema.variants if variant in (v.label, v.index)), None)
        return {
            "schema": schema.model_dump(),
            "summary": catalog.summarize(schema),
            "skeleton": skeleton(schema, index),
        }

    @app.put(f"{API}/documents/{{doc_id}}/input")
    def save_input(
        doc_id: int,
        raw: dict[str, Any] = Body(...),
        svc: DocumentService = Depends(service),
        me=Depends(owner),
    ) -> dict[str, Any]:
        return document_summary(svc.save_input(doc_id, raw, me))

    @app.patch(f"{API}/documents/{{doc_id}}")
    def rename_document(
        doc_id: int, body: Rename, svc: DocumentService = Depends(service), me=Depends(owner)
    ) -> dict[str, Any]:
        return document_summary(svc.rename(doc_id, body.title, me))

    @app.post(f"{API}/documents/{{doc_id}}/validate")
    def validate_document(
        doc_id: int, svc: DocumentService = Depends(service), me=Depends(owner)
    ) -> dict[str, Any]:
        issues = svc.validate(doc_id, me)
        return {
            "ok": not any(i.severity == "error" for i in issues),
            "issues": [issue_dict(i) for i in issues],
        }

    @app.post(f"{API}/documents/{{doc_id}}/generate")
    def generate_document(
        doc_id: int,
        body: GenerateRequest | None = None,
        svc: DocumentService = Depends(service),
        me=Depends(owner),
    ) -> dict[str, Any]:
        body = body or GenerateRequest()
        out = svc.generate(doc_id, me, formats=tuple(f.upper() for f in body.formats),
                           force=body.force)  # fmt: skip
        return outcome_dict(out)

    @app.get(f"{API}/documents/{{doc_id}}/files/{{fmt}}")
    def download(
        doc_id: int,
        fmt: str,
        revision: int | None = None,
        svc: DocumentService = Depends(service),
        me=Depends(owner),
    ) -> FileResponse:
        path, name = svc.artifact(doc_id, fmt, me, revision=revision)
        return FileResponse(path, filename=name)

    @app.post(f"{API}/documents/{{doc_id}}/copy", status_code=201)
    def copy_document(
        doc_id: int, svc: DocumentService = Depends(service), me=Depends(owner)
    ) -> dict[str, Any]:
        return document_summary(svc.copy(doc_id, me))

    @app.delete(f"{API}/documents/{{doc_id}}", status_code=204)
    def delete_document(
        doc_id: int, svc: DocumentService = Depends(service), me=Depends(owner)
    ) -> None:
        svc.delete(doc_id, me)

    @app.get(f"{API}/documents/{{doc_id}}/history")
    def document_history(
        doc_id: int, svc: DocumentService = Depends(service), me=Depends(owner)
    ) -> list[dict[str, Any]]:
        return [event_dict(e) for e in svc.history(doc_id, me)]

    return app


def _errors(app: FastAPI) -> None:
    def handler(status: int):
        def handle(_request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=status)

        return handle

    app.add_exception_handler(NotFound, handler(404))
    app.add_exception_handler(Conflict, handler(409))
    for bad in (InputError, GenerateError, ValueError):
        app.add_exception_handler(bad, handler(400))

    def failed(_request: Request, exc: ValidationFailed) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "issues": [issue_dict(i) for i in exc.issues]},
            status_code=422,
        )

    app.add_exception_handler(ValidationFailed, failed)
