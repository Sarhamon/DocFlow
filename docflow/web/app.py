"""DocFlow 웹 API (M6·M7)와 화면(M8). 화면은 이 API 위에 올린다(`docflow/web/pages.py`).

    uvicorn --factory docflow.web.app:create_app          # 개발 서버
    python tools/sync_forms.py                            # templates/ 의 서식을 먼저 등록
    python tools/manage_users.py create-root admin 관리자  # 루트 관리자를 먼저 만든다

경로는 모두 /api 아래다. 오류는 {"detail": ...} 로 돌려주고, 검증 오류(422)는 오류 목록을 함께 준다.
인증 오류는 {"detail", "reason"} 이다. reason 으로 화면이 분기한다(expired, duplicate_login,
password_change_required, locked …).

로그인은 세션 쿠키다. /api/health 와 /api/auth/login 말고는 모두 로그인이 필요하고, 초기
비밀번호를 바꾸기 전에는 /api/auth/* 만 쓸 수 있다. 문서는 로그인한 본인 것만 보인다.
/api/admin/* 는 관리자 전용이다.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any, Literal

from fastapi import Body, Depends, FastAPI, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session, sessionmaker

from docflow.config import Settings, get_settings
from docflow.convert import Converter
from docflow.generate import GenerateError
from docflow.pipeline import InputError, skeleton
from docflow.schema.labels import field_label
from docflow.schema.model import FormSchema
from docflow.service import catalog
from docflow.service.auth import AuthError, AuthService, Client, Forbidden, SessionInvalid
from docflow.service.catalog import NotFound, Unavailable
from docflow.service.db import Document, DocumentEvent, Form, User, UserSession, connect, now
from docflow.service.documents import (
    Conflict,
    DocumentService,
    GenerateOutcome,
    ValidationFailed,
    issue_dict,
)
from docflow.service.storage import FileStore, LocalFileStore
from docflow.validate import field_required
from docflow.web import admin_api, pages

API = "/api"
COOKIE = "docflow_session"


# ---------------------------------------------------------------- 요청 본문
class NewDocument(BaseModel):
    form_id: int
    title: str | None = None


class Rename(BaseModel):
    title: str = Field(min_length=1, max_length=300)


class LoginRequest(BaseModel):
    login_id: str = Field(min_length=1, max_length=50)
    password: str = Field(min_length=1, max_length=128)


class PasswordChange(BaseModel):
    current: str = Field(max_length=128)
    new: str = Field(max_length=128)


class NewUser(BaseModel):
    login_id: str
    name: str = Field(max_length=50)
    role: Literal["user", "admin"] = "user"
    department: str = Field("", max_length=100)
    phone: str | None = Field(None, max_length=30)
    email: str | None = Field(None, max_length=255)


class UserUpdate(BaseModel):
    name: str | None = Field(None, max_length=50)
    department: str | None = Field(None, max_length=100)
    phone: str | None = Field(None, max_length=30)
    email: str | None = Field(None, max_length=255)


class StatusChange(BaseModel):
    status: Literal["active", "inactive", "deleted"]


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


def fields_meta(schema: FormSchema) -> list[dict[str, Any]]:
    """입력 화면의 누름틀 목록 (IN_01~03): 한글 이름, 필수 여부. 결재란은 뺀다."""
    return [
        {
            "name": f.name,
            "label": f.label or field_label(f.name),
            "input_type": f.input_type,
            "placeholder": f.placeholder,
            "required": field_required(f),
        }
        for f in schema.fields
        if f.role == "input"
    ]


def user_dict(user: User) -> dict[str, Any]:
    return {
        "id": user.id,
        "login_id": user.login_id,
        "name": user.name,
        "department": user.department,
        "phone": user.phone,
        "email": user.email,
        "role": user.role,
        "status": user.status,
        "locked": user.locked_at is not None,
        "must_change_password": user.must_change_password,
        "last_login_at": user.last_login_at.isoformat() if user.last_login_at else None,
        "created_at": user.created_at.isoformat(),
    }


def session_dict(row: UserSession, at: datetime) -> dict[str, Any]:
    """우측 상단 타이머가 쓴다: 남은 초, 자동 연장을 썼는지."""
    return {
        "user": user_dict(row.user),
        "expires_at": row.expires_at.isoformat(),
        "remaining_seconds": max(0, int((row.expires_at - at).total_seconds())),
        "grace_used": row.grace_used,
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
    clock: Callable[[], datetime] = now,
) -> FastAPI:
    """앱을 만든다. converter 가 "auto" 면 한컴오피스가 있을 때만 한컴 변환기를 쓴다.

    clock 은 세션 시간 계산에 쓴다(테스트가 시간을 옮긴다). HTTPS 로 띄울 때는 환경변수
    DOCFLOW_SECURE_COOKIE=1 로 쿠키에 Secure 를 붙인다.
    """
    settings = settings or get_settings()
    settings.data.mkdir(parents=True, exist_ok=True)
    sessions: sessionmaker = connect(database_url or settings.database_url)
    store = store or LocalFileStore(settings.data / "files")
    if converter == "auto":
        from docflow.adapters.hancom import HancomConverter, available

        converter = HancomConverter() if available() else None

    secure_cookie = os.environ.get("DOCFLOW_SECURE_COOKIE") == "1"

    app = FastAPI(title="DocFlow", version="0.7")
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

    def auth(s: Session = Depends(session)) -> AuthService:
        return AuthService(s, clock)

    def current_session(request: Request, svc: AuthService = Depends(auth)) -> UserSession:
        return svc.authenticate(request.cookies.get(COOKIE))

    def current_user(row: UserSession = Depends(current_session)) -> User:
        """로그인한 계정. 초기 비밀번호를 바꾸기 전에는 막는다 (AU_02)."""
        if row.user.must_change_password:
            raise Forbidden("비밀번호를 먼저 바꾼다", "password_change_required")
        return row.user

    def admin_user(user: User = Depends(current_user)) -> User:
        """관리자 전용 경로 (AU_06). 루트도 관리자다."""
        if user.role not in ("admin", "root"):
            raise Forbidden("관리자만 쓸 수 있다")
        return user

    def owner(user: User = Depends(current_user)) -> int:
        """문서 주인. 로그인한 본인 문서만 보인다 (관리자도 자기 문서만)."""
        return user.id

    signed_in = [Depends(current_user)]

    def public_only(user: User = Depends(current_user)) -> bool:
        """사용자는 공개 서식만 본다. 관리자는 올린 서식을 공개 전에 미리 본다 (AD_04)."""
        return user.role not in ("admin", "root")

    # ------------------------------------------------------------ 로그인
    @app.post(f"{API}/auth/login")
    def login(
        body: LoginRequest, request: Request, response: Response, svc: AuthService = Depends(auth)
    ) -> dict[str, Any]:
        client = Client(
            ip=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
        )
        result = svc.login(body.login_id, body.password, client)
        response.set_cookie(
            COOKIE, result.token, httponly=True, samesite="lax", secure=secure_cookie
        )
        return {
            **session_dict(result.session, clock()),
            "must_change_password": result.session.user.must_change_password,
        }

    @app.post(f"{API}/auth/logout", status_code=204)
    def logout(request: Request, response: Response, svc: AuthService = Depends(auth)) -> None:
        svc.logout(request.cookies.get(COOKIE))
        response.delete_cookie(COOKIE)

    @app.get(f"{API}/auth/me")
    def me(row: UserSession = Depends(current_session)) -> dict[str, Any]:
        return session_dict(row, clock())

    @app.post(f"{API}/auth/extend")
    def extend(request: Request, svc: AuthService = Depends(auth)) -> dict[str, Any]:
        """연장 버튼 (AU_04): 지금부터 다시 30분."""
        return session_dict(svc.extend(request.cookies.get(COOKIE)), clock())

    @app.post(f"{API}/auth/grace")
    def grace(request: Request, svc: AuthService = Depends(auth)) -> dict[str, Any]:
        """입력 중 만료 (AU_04): 화면이 경고를 띄우기 전에 부른다. 5분, 한 번만."""
        return session_dict(svc.grace(request.cookies.get(COOKIE)), clock())

    @app.post(f"{API}/auth/password", status_code=204)
    def change_password(
        body: PasswordChange,
        row: UserSession = Depends(current_session),
        svc: AuthService = Depends(auth),
    ) -> None:
        svc.change_password(row.user, body.current, body.new)

    # ------------------------------------------------------------ 계정 관리 (관리자)
    @app.get(f"{API}/admin/users")
    def list_users(
        role: str | None = None,
        status: str | None = None,
        actor: User = Depends(admin_user),
        svc: AuthService = Depends(auth),
    ) -> list[dict[str, Any]]:
        return [user_dict(u) for u in svc.list_users(actor, role=role, status=status)]

    @app.post(f"{API}/admin/users", status_code=201)
    def create_user(
        body: NewUser, actor: User = Depends(admin_user), svc: AuthService = Depends(auth)
    ) -> dict[str, Any]:
        """계정 발급 (AD_18). 초기 비밀번호는 이 응답에서 한 번만 보인다."""
        user, password = svc.create_user(
            actor, body.login_id, body.name, role=body.role,
            department=body.department, phone=body.phone, email=body.email,
        )  # fmt: skip
        return {"user": user_dict(user), "initial_password": password}

    @app.get(f"{API}/admin/users/{{user_id}}")
    def get_user(
        user_id: int, _actor: User = Depends(admin_user), svc: AuthService = Depends(auth)
    ) -> dict[str, Any]:
        return user_dict(svc.get_user(user_id))

    @app.patch(f"{API}/admin/users/{{user_id}}")
    def update_user(
        user_id: int,
        body: UserUpdate,
        actor: User = Depends(admin_user),
        svc: AuthService = Depends(auth),
    ) -> dict[str, Any]:
        return user_dict(svc.update_user(actor, user_id, **body.model_dump(exclude_unset=True)))

    @app.post(f"{API}/admin/users/{{user_id}}/reset-password")
    def reset_password(
        user_id: int, actor: User = Depends(admin_user), svc: AuthService = Depends(auth)
    ) -> dict[str, Any]:
        user, password = svc.reset_password(actor, user_id)
        return {"user": user_dict(user), "initial_password": password}

    @app.post(f"{API}/admin/users/{{user_id}}/unlock")
    def unlock_user(
        user_id: int, actor: User = Depends(admin_user), svc: AuthService = Depends(auth)
    ) -> dict[str, Any]:
        return user_dict(svc.unlock(actor, user_id))

    @app.put(f"{API}/admin/users/{{user_id}}/status")
    def set_user_status(
        user_id: int,
        body: StatusChange,
        actor: User = Depends(admin_user),
        svc: AuthService = Depends(auth),
    ) -> dict[str, Any]:
        return user_dict(svc.set_status(actor, user_id, body.status))

    # ------------------------------------------------------------ 서식
    @app.get(f"{API}/health")
    def health() -> dict[str, Any]:
        return {"ok": True, "converter": converter is not None}

    @app.get(f"{API}/forms", dependencies=signed_in)
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

    @app.get(f"{API}/forms/facets", dependencies=signed_in)
    def form_facets(s: Session = Depends(session)) -> list[dict]:
        return catalog.facets(s)

    @app.get(f"{API}/forms/{{form_id}}", dependencies=signed_in)
    def form_detail(
        form_id: int, s: Session = Depends(session), only: bool = Depends(public_only)
    ) -> dict[str, Any]:
        form = catalog.get_form(s, form_id, public_only=only)
        schema = catalog.load_schema(form.latest)
        return {
            **form_summary(form),
            "summary": catalog.summarize(schema),
            "variants": [{"index": v.index, "label": v.label} for v in schema.choices],
            "versions": [
                {"version": v.version, "created_at": v.created_at.isoformat()}
                for v in form.versions
            ],
        }

    @app.get(f"{API}/forms/{{form_id}}/schema", dependencies=signed_in)
    def form_schema(
        form_id: int,
        version: int | None = None,
        s: Session = Depends(session),
        only: bool = Depends(public_only),
    ) -> dict[str, Any]:
        form = catalog.get_form(s, form_id, public_only=only)
        return catalog.load_schema(catalog.get_version(form, version)).model_dump()

    @app.get(f"{API}/forms/{{form_id}}/layout", dependencies=signed_in)
    def form_layout(
        form_id: int, s: Session = Depends(session), only: bool = Depends(public_only)
    ) -> dict[str, Any]:
        """서식 구조 보기 (FM_07): 스키마와 표 배치(병합 포함)."""
        form = catalog.get_form(s, form_id, public_only=only)
        schema = catalog.load_schema(form.latest)
        return {
            "schema": schema.model_dump(),
            "layout": catalog.layout(form.latest, store),
            "fields": fields_meta(schema),
        }

    @app.get(f"{API}/forms/{{form_id}}/skeleton", dependencies=signed_in)
    def form_skeleton(
        form_id: int, variant: int | None = None, s: Session = Depends(session)
    ) -> dict[str, Any]:
        form = catalog.get_form(s, form_id)
        return skeleton(catalog.load_schema(form.latest), variant)

    # ------------------------------------------------------------ 문서
    @app.post(f"{API}/documents", status_code=201)
    def create_document(
        body: NewDocument,
        svc: DocumentService = Depends(service),
        me=Depends(owner),
        only: bool = Depends(public_only),
    ) -> dict[str, Any]:
        return document_summary(svc.create(body.form_id, me, title=body.title, public_only=only))

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
        index = next((v.index for v in schema.choices if variant in (v.label, v.index)), None)
        return {
            "schema": schema.model_dump(),
            "summary": catalog.summarize(schema),
            "skeleton": skeleton(schema, index),
            "layout": catalog.layout(doc.form_version, store),
            "fields": fields_meta(schema),
        }

    @app.put(f"{API}/documents/{{doc_id}}/input")
    def save_input(
        doc_id: int,
        raw: dict[str, Any] = Body(...),
        svc: DocumentService = Depends(service),
        me=Depends(owner),
    ) -> dict[str, Any]:
        return document_summary(svc.save_input(doc_id, raw, me))

    @app.post(f"{API}/documents/{{doc_id}}/check")
    def check_document(
        doc_id: int,
        raw: dict[str, Any] = Body(...),
        svc: DocumentService = Depends(service),
        me=Depends(owner),
    ) -> dict[str, Any]:
        """입력 중 실시간 점검: 저장하지 않은 입력값을 같은 규칙으로 본다 (이력 없음)."""
        issues = svc.check(doc_id, raw, me)
        return {
            "ok": not any(i.severity == "error" for i in issues),
            "issues": [issue_dict(i) for i in issues],
        }

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

    admin_api.mount(app, session=session, admin_user=admin_user, store=store, converter=converter)
    pages.mount(app, auth)
    return app


def _errors(app: FastAPI) -> None:
    def handler(status: int):
        def handle(_request: Request, exc: Exception) -> JSONResponse:
            return JSONResponse({"detail": str(exc)}, status_code=status)

        return handle

    app.add_exception_handler(NotFound, handler(404))
    app.add_exception_handler(Conflict, handler(409))
    app.add_exception_handler(Unavailable, handler(503))
    for bad in (InputError, GenerateError, ValueError):
        app.add_exception_handler(bad, handler(400))

    def failed(_request: Request, exc: ValidationFailed) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc), "issues": [issue_dict(i) for i in exc.issues]},
            status_code=422,
        )

    app.add_exception_handler(ValidationFailed, failed)

    def denied(_request: Request, exc: AuthError) -> JSONResponse:
        response = JSONResponse(
            {"detail": str(exc), "reason": exc.reason, **exc.extra}, status_code=exc.status
        )
        if isinstance(exc, SessionInvalid):
            response.delete_cookie(COOKIE)
        return response

    app.add_exception_handler(AuthError, denied)
