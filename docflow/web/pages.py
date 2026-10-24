"""사용자 화면 (M8). 서버는 쪽 틀만 그리고, 내용은 화면의 스크립트가 /api 로 채운다.

로그인하지 않았으면 로그인 쪽으로, 초기 비밀번호를 바꾸기 전이면 비밀번호 변경 쪽으로 보낸다.
관리자 쪽(/admin)은 관리자만 연다(AU_06). 권한 판정은 API 가 다시 하므로 여기는 길 안내다.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from docflow.service.auth import AuthError, AuthService
from docflow.service.db import User

HERE = Path(__file__).parent
COOKIE = "docflow_session"
ADMIN_ROLES = ("admin", "root")

templates = Jinja2Templates(directory=HERE / "templates")


def mount(app: FastAPI, auth: Callable[..., AuthService]) -> None:
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")

    def viewer(request: Request, svc: AuthService = Depends(auth)) -> User | Response:
        """쪽을 볼 계정. 볼 수 없으면 갈 곳(RedirectResponse)."""
        try:
            row = svc.authenticate(request.cookies.get(COOKIE))
        except AuthError as exc:
            target = quote(request.url.path)
            reason = f"&reason={exc.reason}" if exc.reason else ""
            return RedirectResponse(f"/login?next={target}{reason}", status_code=303)
        if row.user.must_change_password:
            return RedirectResponse("/password?required=1", status_code=303)
        return row.user

    def render(request: Request, name: str, user: User | None, **context) -> HTMLResponse:
        me = None
        if user is not None:
            me = {"id": user.id, "name": user.name, "login_id": user.login_id, "role": user.role,
                  "admin": user.role in ADMIN_ROLES}  # fmt: skip
        return templates.TemplateResponse(request, name, {"me": me, **context})

    def page(name: str, *, admin: bool = False, **context):
        def handler(request: Request, user: User | Response = Depends(viewer)):
            if isinstance(user, Response):
                return user
            if admin and user.role not in ADMIN_ROLES:
                return RedirectResponse("/forms", status_code=303)
            params = dict(request.path_params)
            return render(request, name, user, params=params, **context)

        return handler

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/forms", status_code=303)

    @app.get("/admin", include_in_schema=False)
    def admin_home() -> RedirectResponse:
        return RedirectResponse("/admin/users", status_code=303)

    @app.get("/login", include_in_schema=False)
    def login_page(request: Request) -> HTMLResponse:
        return render(request, "login.html", None)

    @app.get("/password", include_in_schema=False)
    def password_page(request: Request, svc: AuthService = Depends(auth)) -> Response:
        """비밀번호 변경 (AU_02·AU_05). 초기 비밀번호 상태에서도 연다."""
        try:
            user = svc.authenticate(request.cookies.get(COOKIE)).user
        except AuthError:
            return RedirectResponse("/login", status_code=303)
        return render(request, "password.html", user)

    routes = {
        "/forms": ("forms.html", False),
        "/forms/{form_id}": ("form.html", False),
        "/documents": ("documents.html", False),
        "/documents/{doc_id}": ("editor.html", False),
        "/admin/users": ("admin_users.html", True),
        "/admin/forms": ("admin_forms.html", True),
        "/admin/forms/{form_id}": ("admin_form.html", True),
        "/admin/categories": ("admin_categories.html", True),
        "/admin/rules": ("admin_rules.html", True),
        "/admin/system": ("admin_system.html", True),
    }
    for path, (name, admin) in routes.items():
        app.add_api_route(path, page(name, admin=admin), methods=["GET"],
                          include_in_schema=False, response_model=None)  # fmt: skip
