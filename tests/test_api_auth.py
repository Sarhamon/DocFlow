"""웹 API 인증 테스트 — M7 완료 기준: 발급된 계정으로만 로그인, 타인 문서 접근 차단, 관리자 전용
경로 차단. 세션 시간은 가짜 시계로 옮긴다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.service.catalog import sync_templates
from docflow.service.db import connect
from docflow.service.storage import LocalFileStore
from docflow.web.app import COOKIE, create_app
from tests import service_fixtures as fx


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 10, 18, 9, 0)

    def __call__(self) -> datetime:
        return self.at


@pytest.fixture
def app(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    clock = Clock()
    app = create_app(settings, converter=fx.FakeConverter(), clock=clock)
    app.clock = clock
    for login_id, role in (("root", "root"), ("admin", "admin"), ("kim", "user"), ("lee", "user")):
        fx.make_user(app.state.sessions, login_id, role=role)
    return app


def client_as(app, login_id: str) -> TestClient:
    c = TestClient(app)
    fx.login(c, login_id)
    return c


def traffic_id(c) -> int:
    [item] = c.get("/api/forms", params={"q": "교통비"}).json()["items"]
    return item["id"]


# ------------------------------------------------------------ 완료 기준
def test_로그인하지_않으면_상태_확인_말고는_막는다(app):
    c = TestClient(app)
    assert c.get("/api/health").status_code == 200
    for path in ("/api/forms", "/api/documents", "/api/auth/me", "/api/admin/users"):
        r = c.get(path)
        assert (r.status_code, r.json()["reason"]) == (401, "unauthenticated"), path


def test_발급된_계정으로만_로그인한다(app):
    c = TestClient(app)
    r = c.post("/api/auth/login", json={"login_id": "ghost", "password": fx.PASSWORD})
    assert (r.status_code, r.json()["reason"]) == (401, "wrong_credentials")
    r = fx.login(c, "kim")
    assert COOKIE in r.cookies and "httponly" in r.headers["set-cookie"].lower()
    assert r.json()["user"]["login_id"] == "kim"
    assert c.get("/api/forms").status_code == 200


def test_남의_문서는_없는_것으로_다룬다(app):
    kim, lee = client_as(app, "kim"), client_as(app, "lee")
    doc = kim.post("/api/documents", json={"form_id": traffic_id(kim)}).json()
    assert kim.get(f"/api/documents/{doc['id']}").status_code == 200
    for method, path in (
        ("get", f"/api/documents/{doc['id']}"),
        ("put", f"/api/documents/{doc['id']}/input"),
        ("post", f"/api/documents/{doc['id']}/copy"),
        ("delete", f"/api/documents/{doc['id']}"),
    ):
        kwargs = {"json": {}} if method == "put" else {}
        assert getattr(lee, method)(path, **kwargs).status_code == 404, path
    assert lee.get("/api/documents").json()["total"] == 0
    assert kim.get("/api/documents").json()["total"] == 1


def test_관리자_전용_경로는_사용자에게_막힌다(app):
    kim, admin = client_as(app, "kim"), client_as(app, "admin")
    r = kim.get("/api/admin/users")
    assert (r.status_code, r.json()["reason"]) == (403, "forbidden")
    assert kim.post("/api/admin/users", json={"login_id": "x1", "name": "x"}).status_code == 403
    assert admin.get("/api/admin/users").status_code == 200


# ------------------------------------------------------------ 세션 (AU_04)
def test_세션_만료와_연장(app):
    kim = client_as(app, "kim")
    assert kim.get("/api/auth/me").json()["remaining_seconds"] == 30 * 60
    app.clock.at += timedelta(minutes=20)
    assert kim.post("/api/auth/extend").json()["remaining_seconds"] == 30 * 60
    app.clock.at += timedelta(minutes=30)
    r = kim.get("/api/forms")
    assert (r.status_code, r.json()["reason"]) == (401, "expired")


def test_입력_중_만료되면_5분_자동_연장(app):
    kim = client_as(app, "kim")
    app.clock.at += timedelta(minutes=30)
    out = kim.post("/api/auth/grace").json()
    assert (out["remaining_seconds"], out["grace_used"]) == (5 * 60, True)
    assert kim.get("/api/forms").status_code == 200
    r = kim.post("/api/auth/grace")
    assert (r.status_code, r.json()["reason"]) == (403, "grace_used")


def test_다른_곳에서_로그인하면_기존_화면은_사유를_받는다(app):
    first, second = client_as(app, "kim"), client_as(app, "kim")
    r = first.get("/api/forms")
    assert (r.status_code, r.json()["reason"]) == (401, "duplicate_login")
    assert "다른 곳" in r.json()["detail"]
    assert second.get("/api/forms").status_code == 200


def test_로그아웃(app):
    kim = client_as(app, "kim")
    assert kim.post("/api/auth/logout").status_code == 204
    assert kim.get("/api/forms").status_code == 401


# ------------------------------------------------------------ 잠금·비밀번호 (AU_02·AU_03)
def test_3회_실패하면_잠기고_관리자가_푼다(app):
    c = TestClient(app)
    bad = {"login_id": "kim", "password": "Wrong#pass1"}
    assert c.post("/api/auth/login", json=bad).json()["remaining"] == 2
    c.post("/api/auth/login", json=bad)
    r = c.post("/api/auth/login", json=bad)
    assert (r.status_code, r.json()["reason"]) == (423, "locked")
    good = {"login_id": "kim", "password": fx.PASSWORD}
    assert c.post("/api/auth/login", json=good).status_code == 423

    admin = client_as(app, "admin")
    kim_id = next(u["id"] for u in admin.get("/api/admin/users").json() if u["login_id"] == "kim")
    assert admin.post(f"/api/admin/users/{kim_id}/unlock").json()["locked"] is False
    assert c.post("/api/auth/login", json=good).status_code == 200


def test_발급한_계정은_비밀번호를_바꾸기_전에_아무것도_못한다(app):
    admin = client_as(app, "admin")
    out = admin.post("/api/admin/users", json={"login_id": "park", "name": "박"}).json()
    assert out["user"]["must_change_password"] is True

    park = TestClient(app)
    r = fx.login(park, "park", out["initial_password"])
    assert r.json()["must_change_password"] is True
    r = park.get("/api/forms")
    assert (r.status_code, r.json()["reason"]) == (403, "password_change_required")
    assert park.get("/api/auth/me").status_code == 200

    weak = {"current": out["initial_password"], "new": "weakpass"}
    assert park.post("/api/auth/password", json=weak).status_code == 400
    ok = {"current": out["initial_password"], "new": "Park#pass1"}
    assert park.post("/api/auth/password", json=ok).status_code == 204
    assert park.get("/api/forms").status_code == 200


def test_관리자는_관리자_계정을_발급하지_못하고_루트는_한다(app):
    admin, root = client_as(app, "admin"), client_as(app, "root")
    body = {"login_id": "admin2", "name": "관리자2", "role": "admin"}
    assert admin.post("/api/admin/users", json=body).status_code == 403
    assert root.post("/api/admin/users", json=body).status_code == 201
    assert root.post("/api/admin/users", json={**body, "role": "root"}).status_code == 422


def test_비활성화하면_그_계정의_세션이_끊긴다(app):
    kim, admin = client_as(app, "kim"), client_as(app, "admin")
    kim_id = kim.get("/api/auth/me").json()["user"]["id"]
    r = admin.put(f"/api/admin/users/{kim_id}/status", json={"status": "inactive"})
    assert r.json()["status"] == "inactive"
    r = kim.get("/api/forms")
    assert (r.status_code, r.json()["reason"]) == (401, "account_disabled")
