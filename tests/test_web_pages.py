"""사용자 화면 테스트 (M8). 화면 내용은 스크립트가 API 로 채우므로, 여기서는 길 안내(로그인·
비밀번호 변경·권한)와 화면이 쓰는 API(표 배치·실시간 점검·누름틀 목록)를 본다.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.service.auth import AuthService
from docflow.service.catalog import sync_templates
from docflow.service.db import Document, connect
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import service_fixtures as fx

PAGES = ("/forms", "/documents")


@pytest.fixture
def app(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    app = create_app(settings, converter=fx.FakeConverter())
    fx.make_user(app.state.sessions)
    return app


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        fx.login(c)
        yield c


def traffic_id(c) -> int:
    [item] = c.get("/api/forms", params={"q": "교통비"}).json()["items"]
    return item["id"]


def new_doc(c) -> int:
    return c.post("/api/documents", json={"form_id": traffic_id(c)}).json()["id"]


# ------------------------------------------------------------ 길 안내
def test_로그인하지_않으면_로그인_쪽으로_보낸다(app):
    c = TestClient(app)
    for path in (*PAGES, "/documents/1", "/forms/1"):
        r = c.get(path, follow_redirects=False)
        assert r.status_code == 303, path
        assert r.headers["location"].startswith(f"/login?next={path}")
    assert c.get("/login").status_code == 200


def test_파비콘은_로그인_없이_받는다(app):
    r = TestClient(app).get("/favicon.ico")
    assert (r.status_code, r.headers["content-type"]) == (200, "image/x-icon")


def test_첫_화면은_서식_선택이다(client):
    r = client.get("/", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/forms")


def test_로그인하면_화면이_열린다(client):
    for path in (*PAGES, f"/forms/{traffic_id(client)}", f"/documents/{new_doc(client)}"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert "writer" in r.text  # 우측 상단 계정 표시


def test_초기_비밀번호면_비밀번호_변경_쪽으로_보낸다(app):
    with app.state.sessions() as s:
        _, password = AuthService(s).create_user(None, "newbie", "새내기")
    c = TestClient(app)
    fx.login(c, "newbie", password)
    r = c.get("/forms", follow_redirects=False)
    assert r.headers["location"] == "/password?required=1"
    assert c.get("/password").status_code == 200


def test_관리자_메뉴는_관리자에게만_보인다(app, client):
    assert 'href="/admin"' not in client.get("/forms").text
    fx.make_user(app.state.sessions, "boss", role="admin")
    admin = TestClient(app)
    fx.login(admin, "boss")
    assert 'href="/admin"' in admin.get("/forms").text


def test_체험_계정에는_비밀번호_버튼이_없다(tmp_path, monkeypatch, client):
    assert 'href="/password"' in client.get("/forms").text
    monkeypatch.setenv("DOCFLOW_DEMO_LOGINS", "demo")
    app = create_app(Settings.from_root(tmp_path), converter=fx.FakeConverter())
    fx.make_user(app.state.sessions, "demo")
    demo = TestClient(app)
    fx.login(demo, "demo")
    assert 'href="/password"' not in demo.get("/forms").text


def test_QR_주소는_체험_계정으로_바로_들어간다(tmp_path, monkeypatch, app):
    with TestClient(app) as c:  # 체험 계정이 없으면 로그인 쪽으로
        assert c.get("/demo", follow_redirects=False).headers["location"] == "/login"
    monkeypatch.setenv("DOCFLOW_DEMO_LOGINS", "demo")
    demo_app = create_app(Settings.from_root(tmp_path), converter=fx.FakeConverter())
    fx.make_user(demo_app.state.sessions, "demo")
    with TestClient(demo_app) as c:
        res = c.get("/demo", follow_redirects=False)
        assert (res.status_code, res.headers["location"]) == (303, "/forms")
        assert c.get("/api/auth/me").json()["user"]["login_id"].startswith("demo~")
        assert 'href="/password"' not in c.get("/forms").text


def test_QR_체험은_사람마다_문서가_따로고_하루_뒤_지운다(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCFLOW_DEMO_LOGINS", "demo")
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    store = LocalFileStore(settings.data / "files")
    with connect(settings.database_url)() as s:
        sync_templates(s, store, settings)
    clock = Clock()
    demo_app = create_app(settings, converter=fx.FakeConverter(), store=store, clock=clock)
    fx.make_user(demo_app.state.sessions, "demo")
    with TestClient(demo_app) as a, TestClient(demo_app) as b:
        a.get("/demo")
        b.get("/demo")
        doc = new_doc(a)
        a.put(f"/api/documents/{doc}/input", json=fx.traffic_input())
        assert a.post(f"/api/documents/{doc}/generate", json={"formats": []}).status_code == 200
        assert [d["id"] for d in a.get("/api/documents").json()["items"]] == [doc]
        assert b.get("/api/documents").json()["items"] == []
        assert b.get(f"/api/documents/{doc}").status_code == 404
        files = settings.data / "files" / "documents" / str(doc)
        assert any(files.rglob("*"))

        clock.advance(hours=24)
        with TestClient(demo_app) as c:
            c.get("/demo")
        assert a.get("/api/auth/me").status_code == 401
        assert not any(files.rglob("*"))
        with demo_app.state.sessions() as s:
            assert s.query(Document).count() == 0


class Clock:
    def __init__(self) -> None:
        self.at = datetime(2026, 11, 20, 10, 0)

    def __call__(self) -> datetime:
        return self.at

    def advance(self, **delta: float) -> None:
        self.at += timedelta(**delta)


ADMIN_PAGES = (
    "/admin/users", "/admin/forms", "/admin/forms/1", "/admin/categories", "/admin/rules",
    "/admin/system", "/admin/documents", "/admin/stats", "/admin/logins", "/admin/audit",
    "/admin/cross", "/admin/checklist",
)  # fmt: skip


def test_관리자_쪽은_관리자만_연다(app, client):
    for path in ADMIN_PAGES:
        r = client.get(path, follow_redirects=False)
        assert (r.status_code, r.headers["location"]) == (303, "/forms"), path
    fx.make_user(app.state.sessions, "boss", role="admin")
    admin = TestClient(app)
    fx.login(admin, "boss")
    r = admin.get("/admin", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/admin/users")
    for path in ADMIN_PAGES:
        page = admin.get(path)
        assert page.status_code == 200 and "관리자" in page.text, path
    # 로그인 이력과 감사 로그는 한 틀을 탭으로 나눈다
    assert "const audit = true" in admin.get("/admin/audit").text
    assert "const audit = false" in admin.get("/admin/logins").text


def test_정적_파일(client):
    for name in ("app.css", "app.js", "sheet.js", "editor.js"):
        assert client.get(f"/static/{name}").status_code == 200


def test_정적_파일은_매번_서버에_확인한다(client):
    """배포 뒤 캐시에 남은 예전 스크립트가 새 화면 틀과 섞이지 않게 한다 (2026-11-13 배포)."""
    page = client.get("/static/editor.js")
    assert page.headers["cache-control"] == "no-cache"
    again = client.get("/static/editor.js", headers={"if-none-match": page.headers["etag"]})
    assert again.status_code == 304


# ------------------------------------------------------------ 화면이 쓰는 API
def test_표_배치는_병합을_담는다(client):
    data = client.get(f"/api/forms/{traffic_id(client)}/layout").json()
    [table] = data["layout"]
    spans = {(c["row"], c["col"]): (c["row_span"], c["col_span"]) for c in table["cells"]}
    assert spans[(0, 1)] == (1, 3)  # 학과/부서명 값 칸은 세 칸을 합쳤다
    assert spans[(1, 0)] == (2, 1)  # 두 줄 헤더 '연번'
    assert data["schema"]["repeats"][0]["id"] == "t0.r3-5"


def test_문서_입력_틀은_표_배치와_누름틀_목록을_준다(client):
    data = client.get(f"/api/documents/{new_doc(client)}/form").json()
    assert data["layout"][0]["rows"] == 7
    assert data["fields"] == []


def test_실시간_점검은_저장하지_않고_이력도_남기지_않는다(client):
    doc = new_doc(client)
    r = client.post(f"/api/documents/{doc}/check", json={"cells": {}})
    assert r.status_code == 200
    body = r.json()
    assert not body["ok"]
    assert [i["code"] for i in body["issues"]] == ["REQUIRED_EMPTY"]
    r = client.post(f"/api/documents/{doc}/check", json=fx.traffic_input())
    assert r.json() == {"ok": True, "issues": []}
    assert client.get(f"/api/documents/{doc}").json()["input"] == {}
    assert [e["kind"] for e in client.get(f"/api/documents/{doc}/history").json()] == ["created"]


def test_실시간_점검은_금액_형식을_경고한다(client):
    doc = new_doc(client)
    raw = fx.traffic_input()
    raw["rows"]["t0.r3-5"][0]["입금액"] = "오천원"
    body = client.post(f"/api/documents/{doc}/check", json=raw).json()
    assert body["ok"]  # 경고는 막지 않는다
    assert [(i["code"], i["severity"], i["where"]) for i in body["issues"]] == [
        ("AMOUNT_INVALID", "warning", "t0.r3-5[0].c3")
    ]


def test_실시간_점검도_서식에_없는_칸은_거부한다(client):
    doc = new_doc(client)
    r = client.post(f"/api/documents/{doc}/check", json={"cells": {"t9.r9.c9": "x"}, "bogus": 1})
    assert r.status_code == 400


def test_남의_문서는_점검할_수_없다(app, client):
    doc = new_doc(client)
    fx.make_user(app.state.sessions, "other")
    other = TestClient(app)
    fx.login(other, "other")
    assert other.post(f"/api/documents/{doc}/check", json={}).status_code == 404
    assert other.get(f"/documents/{doc}").status_code == 200  # 쪽 틀은 열리지만 API 가 막는다
