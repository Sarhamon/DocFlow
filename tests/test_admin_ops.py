"""관리자 운영 현황 API (M11, AD_23~26): 문서 현황·열람(사유), 오류 통계, 로그인 이력, 감사 로그."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.service.catalog import sync_templates
from docflow.service.db import connect
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import service_fixtures as fx


@pytest.fixture
def app(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    app = create_app(settings, converter=fx.FakeConverter())
    fx.make_user(app.state.sessions)
    fx.make_user(app.state.sessions, "boss", role="admin")
    return app


@pytest.fixture
def admin(app):
    with TestClient(app) as c:
        fx.login(c, "boss")
        yield c


@pytest.fixture
def user(app):
    with TestClient(app) as c:
        fx.login(c)
        yield c


def traffic_doc(user, raw: dict | None = None) -> int:
    [form] = user.get("/api/forms", params={"q": "교통비"}).json()["items"]
    doc = user.post("/api/documents", json={"form_id": form["id"], "title": "10월 교통비"}).json()
    if raw is not None:
        user.put(f"/api/documents/{doc['id']}/input", json=raw)
    return doc["id"]


def audit_of(admin, action: str) -> list[dict]:
    return admin.get("/api/admin/audit", params={"action": action}).json()["items"]


def test_사용자는_운영_현황을_볼_수_없다(user):
    for name in ("documents", "stats", "logins", "audit"):
        assert user.get(f"/api/admin/{name}").status_code == 403, name


# ------------------------------------------------------------ 문서 현황 (AD_23)
def test_문서_목록은_작성자와_마지막_검증만_주고_입력값은_주지_않는다(admin, user):
    doc = traffic_doc(user, {})
    user.post(f"/api/documents/{doc}/validate")
    data = admin.get("/api/admin/documents", params={"q": "writer"}).json()
    [row] = data["items"]
    assert (row["id"], row["title"], row["owner"]["login_id"], row["status"]) == (
        doc, "10월 교통비", "writer", "draft")  # fmt: skip
    assert row["errors"] >= 1 and "input" not in row
    assert data["counts"] == {"total": 1, "draft": 1, "completed": 0}


def test_문서_열람은_사유가_있어야_하고_감사_로그에_남는다(admin, user):
    doc = traffic_doc(user, {"cells": {"t0.r0.c1": "컴퓨터과"}})
    url = f"/api/admin/documents/{doc}/open"
    assert admin.post(url, json={"reason": "확인"}).status_code == 400
    r = admin.post(url, json={"reason": "  사용자 문의  대응 "})
    assert r.status_code == 200, r.text
    assert r.json()["input"]["cells"] == {"t0.r0.c1": "컴퓨터과"}
    [log] = audit_of(admin, "document.view")
    assert (log["actor"], log["detail"]) == ("boss", "사용자 문의 대응")
    assert f"문서 {doc}" in log["target"] and "writer" in log["target"]


def test_남의_산출물은_사유를_받고_내려준다(admin, user):
    doc = traffic_doc(user, fx.traffic_input())
    user.post(f"/api/documents/{doc}/generate", json={"formats": ["PDF"]})
    url = f"/api/admin/documents/{doc}/files/PDF"
    assert admin.get(url).status_code == 422  # 사유 없음
    r = admin.get(url, params={"reason": "정산 서류 점검"})
    assert r.status_code == 200 and r.content.startswith(b"%PDF")
    [log] = audit_of(admin, "document.download")
    assert log["detail"].endswith("정산 서류 점검")


# ------------------------------------------------------------ 오류 통계 (AD_24)
def test_오류_통계는_규칙별·서식별·날짜별로_센다(admin, user):
    doc = traffic_doc(user, {})
    user.post(f"/api/documents/{doc}/validate")
    user.post(f"/api/documents/{doc}/validate")
    stats = admin.get("/api/admin/stats", params={"days": 7}).json()
    assert (stats["checks"], stats["failed"]) == (2, 2)
    rule = next(r for r in stats["by_rule"] if r["code"] == "REQUIRED_EMPTY")
    assert (rule["name"], rule["documents"]) == ("필수 누락", 1) and rule["errors"] >= 2
    [form] = stats["by_form"]
    assert (form["title"], form["checks"], form["failed"]) == ("교통비 지급 내역서", 2, 2)
    [day] = stats["daily"]
    assert (day["checks"], day["failed"]) == (2, 2)


# ------------------------------------------------------------ 로그인 이력 (AD_25)
def test_로그인_이력은_실패도_보이고_걸러_볼_수_있다(app, admin):
    with TestClient(app) as c:
        c.post("/api/auth/login", json={"login_id": "writer", "password": "wrong"})
        c.post("/api/auth/login", json={"login_id": "ghost", "password": "x"})
    failed = admin.get("/api/admin/logins", params={"success": False}).json()["items"]
    assert [(r["login_id"], r["fail_reason"]) for r in failed] == [
        ("ghost", "unknown_id"), ("writer", "wrong_password")]  # fmt: skip
    mine = admin.get("/api/admin/logins", params={"login_id": "boss"}).json()["items"]
    assert [r["success"] for r in mine] == [True]
    later = admin.get("/api/admin/logins", params={"since": "2999-01-01T00:00:00+09:00"})
    assert later.json()["total"] == 0


# ------------------------------------------------------------ 감사 로그 (AD_26)
def test_관리자_변경은_감사_로그에_남고_비밀번호는_적지_않는다(admin):
    out = admin.post("/api/admin/users", json={"login_id": "newbie", "name": "신입"}).json()
    admin.post(f"/api/admin/users/{out['user']['id']}/reset-password")
    [form] = admin.get("/api/admin/forms", params={"q": "회의"}).json()["items"]
    admin.patch(f"/api/admin/forms/{form['id']}", json={"public": False})
    admin.put("/api/admin/rules/DATE_ORDER", json={"enabled": False})

    data = admin.get("/api/admin/audit").json()
    got = [(r["action"], r["target"]) for r in data["items"]]
    assert got == [
        ("rule.toggle", "DATE_ORDER"),
        ("form.update", "AID 7-01 회의계획서"),
        ("user.reset_password", "newbie"),
        ("user.create", "newbie"),
    ]
    assert data["items"][0]["action_name"] == "규칙 켜기·끄기"
    assert all(out["initial_password"] not in r["detail"] for r in data["items"])
    assert admin.get("/api/admin/audit", params={"q": "회의계획서"}).json()["total"] == 1


# ------------------------------------------------------------ 문서 간 대조 (VA_11·VA_12)
def amount_form(tmp_path, name: str) -> bytes:
    from tests import hwpx_factory as hf
    from tests.test_schema_extract import tbl

    xml = hf.section(tbl(2, 2, (0, 0, "사업명"), (0, 1, "○○○ 사업"),
                         (1, 0, "금액"), (1, 1, "0,000,000원")))  # fmt: skip
    return hf.build(tmp_path / f"{name}.hwpx", xml).read_bytes()


@pytest.fixture
def pair(admin, tmp_path):
    """계획서·정산서를 올려 공개하고 (계획 id, 정산 id) 를 준다."""
    ids = []
    for no, name in (("3-01", "사업 계획서"), ("3-02", "사업 정산서")):
        upload = (f"[AID 서식 {no}호] {name}.hwpx", amount_form(tmp_path, no))
        r = admin.post("/api/admin/forms", data={"program": "AID"}, files={"file": upload})
        [form] = r.json()["forms"]
        admin.patch(f"/api/admin/forms/{form['id']}", json={"public": True})
        ids.append(form["id"])
    return tuple(ids)


def rule_body(pair, op="le", **kw):
    plan, settle = pair
    return {"name": "계획 대비 정산", "source_form_id": plan, "source_keys": ["t0.r1.c1"],
            "target_form_id": settle, "target_keys": ["t0.r1.c1"], "op": op, **kw}  # fmt: skip


def new_doc(c, form_id: int, money: str) -> int:
    doc = c.post("/api/documents", json={"form_id": form_id}).json()["id"]
    cells = {"t0.r0.c1": "취업캠프", "t0.r1.c1": money}
    c.put(f"/api/documents/{doc}/input", json={"cells": cells})
    return doc


def cross(c, doc: int) -> list[str]:
    issues = c.post(f"/api/documents/{doc}/validate").json()["issues"]
    return [i["message"] for i in issues if i["code"] == "CROSS_AMOUNT_MISMATCH"]


def test_대조_규칙은_서식에_있는_항목으로만_만든다(admin, pair):
    r = admin.post("/api/admin/cross-rules", json=rule_body(pair))
    assert r.status_code == 201, r.text
    rule = r.json()
    assert (rule["source"]["keys"], rule["op_name"]) == (
        [{"key": "t0.r1.c1", "label": "금액"}], "대상이 기준보다 크지 않아야 한다")  # fmt: skip
    keys = admin.get("/api/admin/cross-rules/keys", params={"form_id": pair[0]}).json()
    assert [k["key"] for k in keys] == ["t0.r0.c1", "t0.r1.c1"]
    bad = [rule_body(pair, source_keys=["t9.r9.c9"]), rule_body(pair, op="ge"),
           rule_body(pair, name=" "), rule_body(pair, target_keys=[])]  # fmt: skip
    for body in bad:
        assert admin.post("/api/admin/cross-rules", json=body).status_code == 400
    [log] = audit_of(admin, "cross.save")
    assert log["target"] == "계획 대비 정산"


def test_이어_둔_문서의_금액이_어긋나면_양쪽에서_경고한다(admin, user, pair):
    plan_form, settle_form = pair
    rule = admin.post("/api/admin/cross-rules", json=rule_body(pair)).json()
    plan = new_doc(user, plan_form, "1,000,000")
    settle = new_doc(user, settle_form, "1,200,000원")

    [link] = user.get(f"/api/documents/{settle}/links").json()
    assert (link["role"], link["linked"], [d["id"] for d in link["candidates"]]) == (
        "target", None, [plan])  # fmt: skip
    assert cross(user, settle) == []  # 잇기 전에는 보지 않는다

    put = user.put(f"/api/documents/{settle}/links/{rule['id']}", json={"document_id": plan})
    assert put.status_code == 200
    [msg] = cross(user, settle)
    assert "1,200,000원" in msg and "1,000,000원" in msg
    assert len(cross(user, plan)) == 1  # 계획서 쪽에서도 보인다
    assert user.get(f"/api/documents/{plan}/links").json()[0]["linked"]["id"] == settle

    cells = {"t0.r0.c1": "취업캠프", "t0.r1.c1": "900,000"}
    user.put(f"/api/documents/{settle}/input", json={"cells": cells})
    assert cross(user, settle) == []  # 정산 ≤ 계획
    admin.put(f"/api/admin/cross-rules/{rule['id']}", json=rule_body(pair, op="eq"))
    assert len(cross(user, settle)) == 1  # 같아야 한다
    out = user.post(f"/api/documents/{settle}/generate", json={"formats": []})
    assert out.status_code == 200 and any(
        i["code"] == "CROSS_AMOUNT_MISMATCH" for i in out.json()["issues"])  # fmt: skip


def test_남의_문서는_이을_수_없고_지우면_연결도_사라진다(app, admin, user, pair):
    plan_form, settle_form = pair
    rule = admin.post("/api/admin/cross-rules", json=rule_body(pair)).json()
    others = new_doc(admin, plan_form, "1,000,000")  # 관리자가 쓴 계획서
    settle = new_doc(user, settle_form, "1,200,000")
    url = f"/api/documents/{settle}/links/{rule['id']}"
    assert user.put(url, json={"document_id": others}).status_code == 400

    plan = new_doc(user, plan_form, "1,000,000")
    user.put(url, json={"document_id": plan})
    assert user.delete(f"/api/documents/{plan}").status_code == 204
    assert user.get(f"/api/documents/{settle}/links").json()[0]["linked"] is None
    assert user.put(url, json={"document_id": None}).status_code == 200


def test_대조_규칙을_끄거나_지우면_보지_않는다(admin, user, pair):
    plan_form, settle_form = pair
    rule = admin.post("/api/admin/cross-rules", json=rule_body(pair)).json()
    plan = new_doc(user, plan_form, "1,000,000")
    settle = new_doc(user, settle_form, "2,000,000")
    user.put(f"/api/documents/{settle}/links/{rule['id']}", json={"document_id": plan})
    admin.put(f"/api/admin/cross-rules/{rule['id']}", json=rule_body(pair, enabled=False))
    assert cross(user, settle) == [] and user.get(f"/api/documents/{settle}/links").json() == []
    assert admin.delete(f"/api/admin/cross-rules/{rule['id']}").status_code == 204
    assert admin.get("/api/admin/cross-rules").json() == []
