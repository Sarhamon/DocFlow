"""정성 판정 (M10, ER_05·ER_06): 관리자가 정한 체크리스트로 LLM 이 판정하고 근거 문장을 댄다."""

from __future__ import annotations

from datetime import date

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.service import review as rv
from docflow.service.catalog import sync_templates
from docflow.service.db import ChecklistItem, Form, connect
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import service_fixtures as fx
from tests.test_draft import RAW, 기안
from tests.test_prefill import FakeLlm

TODAY = date(2026, 10, 27)
QUESTIONS = [rv.Question(1, "다과비가 회의 인원에 비추어 과다하지 않은가"),
             rv.Question(2, "사업명이 적혀 있는가"),
             rv.Question(3, "회의 목적이 사업과 관련되는가")]  # fmt: skip


def run(answer: dict, raw: dict = RAW):
    return rv.review(기안(), raw, QUESTIONS, FakeLlm(answer), today=TODAY)


# ---------------------------------------------------------------- 판정
def test_요청은_물음마다_판정·이유·근거를_받고_개인정보는_넘기지_않는다():
    llm = FakeLlm({})
    rv.review(기안(), RAW, QUESTIONS, llm, today=TODAY)
    req = llm.requests[0]
    assert req.schema["required"] == ["q1", "q2", "q3"]
    q1 = req.schema["properties"]["q1"]
    assert q1["properties"]["verdict"]["enum"] == ["pass", "fail", "unknown"]
    assert q1["description"] == QUESTIONS[0].text
    assert "- q2: 사업명이 적혀 있는가" in req.prompt
    assert "- 표 1 명단 인원: 2명" in req.prompt
    assert "김철수" not in req.prompt and "110-123" not in req.prompt


def test_부적합·판단_불가만_경고하고_근거가_있는_칸을_가리킨다():
    out = run({
        "q1": {"verdict": "fail", "reason": "2명에 다과 50,000원은 많음", "evidence": "다과"},
        "q2": {"verdict": "pass", "reason": "적혀 있음", "evidence": "“캡스톤  디자인”"},
        "q3": {"verdict": "unknown", "reason": "목적이 없음", "evidence": ""},
    })
    assert [(v.verdict, v.location.key, v.verified) for v in out.verdicts] == [
        ("fail", "t0.r5-6[0].c0", True),
        ("pass", "t0.r0.c1", True),  # 따옴표·띄어쓰기가 달라도 찾는다
        ("unknown", "document", False),
    ]
    assert [(i.code, i.severity, i.where) for i in out.issues] == [
        (rv.QUALITATIVE, "warning", "t0.r5-6[0].c0"),
        (rv.QUALITATIVE, "warning", "document"),
    ]
    assert out.issues[0].message == f"[부적합] {QUESTIONS[0].text} — 2명에 다과 50,000원은 많음"
    assert out.issues[0].action.startswith("근거: “다과”")
    assert "판단할 내용이 없습니다" in out.issues[1].action


def test_문서에_없는_근거는_믿지_않는다():
    out = run({"q1": {"verdict": "fail", "reason": "", "evidence": "다과비 30만원"}})
    v = out.verdicts[0]
    assert not v.verified and v.location == rv.DOCUMENT
    assert "찾지 못했습니다" in out.issues[0].action


def test_빠지거나_엉뚱한_답은_판단_불가():
    out = run({"q1": {"verdict": "yes"}, "q2": "pass"})
    assert [v.verdict for v in out.verdicts] == ["unknown", "unknown", "unknown"]
    assert len(out.issues) == 3


def test_물음이나_채운_칸이_없으면_거절():
    with pytest.raises(ValueError, match="체크리스트가 없습니다"):
        rv.review(기안(), RAW, [], FakeLlm({}))
    with pytest.raises(ValueError, match="채운 칸이 없습니다"):
        rv.review(기안(), {}, QUESTIONS, FakeLlm({}))


# ---------------------------------------------------------------- 체크리스트 관리
@pytest.fixture
def app(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    llm = FakeLlm({"q1": {"verdict": "fail", "reason": "회의명이 모호함", "evidence": "중간점검"}})
    app = create_app(settings, converter=None, llm=llm)
    app.state.llm = llm
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


def form_id(client, q: str) -> int:
    [form] = client.get("/api/forms", params={"q": q}).json()["items"]
    return form["id"]


def test_서식별_물음과_공통_물음을_합쳐_묻는다(app):
    with app.state.sessions() as s:
        meeting, traffic = (f.id for f in s.query(Form).order_by(Form.id) if "목차" not in f.title)
        s.add_all([ChecklistItem(text="공통", form_id=None),
                   ChecklistItem(text="회의 전용", form_id=meeting),
                   ChecklistItem(text="교통비 전용", form_id=traffic),
                   ChecklistItem(text="꺼짐", enabled=False)])  # fmt: skip
        s.commit()
        texts = {f.id: [i.text for i in rv.items_for(s, f.id)] for f in s.query(Form)}
    assert texts[meeting] == ["공통", "회의 전용"]
    assert texts[traffic] == ["공통", "교통비 전용"]


def test_관리자가_물음을_추가·수정·삭제하고_감사_로그에_남는다(admin, user):
    assert user.get("/api/admin/checklist").status_code == 403
    meeting = form_id(admin, "회의계획서")
    body = {"text": "  회의 목적이   분명한가 ", "form_id": meeting}
    r = admin.post("/api/admin/checklist", json=body)
    assert r.status_code == 201, r.text
    item = r.json()
    assert item["text"] == "회의 목적이 분명한가"
    assert item["form"]["id"] == meeting
    body = {"text": "공통 물음", "enabled": False}
    r = admin.put(f"/api/admin/checklist/{item['id']}", json=body)
    assert r.json()["form"] is None and r.json()["enabled"] is False
    assert admin.post("/api/admin/checklist", json={"text": "  "}).status_code == 400
    assert admin.post("/api/admin/checklist", json={"text": "x", "form_id": 999}).status_code == 400
    assert admin.delete(f"/api/admin/checklist/{item['id']}").status_code == 204
    assert admin.get("/api/admin/checklist").json() == []
    logs = admin.get("/api/admin/audit", params={"action": "checklist.save"}).json()["items"]
    assert len(logs) == 3


def test_사용자는_문서의_정성_판정을_받는다(app, admin, user):
    meeting = form_id(user, "회의계획서")
    doc = user.post("/api/documents", json={"form_id": meeting}).json()["id"]
    assert user.get(f"/api/documents/{doc}/form").json()["checklist"] == 0
    raw = {"cells": {"t0.r0.c1": "캡스톤 중간점검 회의"}}
    assert user.post(f"/api/documents/{doc}/review", json={"input": raw}).status_code == 400

    item = admin.post("/api/admin/checklist", json={"text": "회의명이 구체적인가"}).json()
    app.state.llm.answer = {f"q{item['id']}": app.state.llm.answer["q1"]}
    assert user.get(f"/api/documents/{doc}/form").json()["checklist"] == 1
    r = user.post(f"/api/documents/{doc}/review", json={"input": raw})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["results"] == [{
        "id": item["id"], "question": "회의명이 구체적인가", "verdict": "fail",
        "verdict_name": "부적합", "reason": "회의명이 모호함", "evidence": "중간점검",
        "where": "t0.r0.c1", "verified": True,
    }]  # fmt: skip
    assert [(i["code"], i["where"]) for i in out["issues"]] == [(rv.QUALITATIVE, "t0.r0.c1")]
    assert user.get(f"/api/documents/{doc}").json()["input"] == {}  # 저장하지 않는다


def test_여러_줄_근거는_줄마다_나눠_찾는다():
    """실측: '일시 : …\n장소 : …' 처럼 원문의 두 줄을 이어 근거로 댔다."""
    raw = {**RAW, "fields": {**RAW["fields"],
                             "content_body": "가. 일시 : 2026. 10. 30.\n나. 장소 : 공학관 301호"}}
    answer = {"q2": {"verdict": "pass", "reason": "",
                     "evidence": "일시 : 2026. 10. 30.\n장소 : 공학관 301호"}}  # fmt: skip
    out = run(answer, raw)
    assert out.verdicts[1].location.key == "content_body"
