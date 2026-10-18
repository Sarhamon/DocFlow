"""웹 API 테스트 — M6 완료 기준: API 만으로 문서 생성 → 저장 → 재조회 → 산출물 다운로드.

서식은 코드로 만들고(tests/service_fixtures.py), 변환은 가짜 변환기로 한다.
"""

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
def client(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    converter = fx.FakeConverter()
    app = create_app(settings, converter=converter)
    fx.make_user(app.state.sessions)
    with TestClient(app) as c:
        c.converter = converter
        fx.login(c)
        yield c


def traffic_id(client) -> int:
    [item] = client.get("/api/forms", params={"q": "교통비"}).json()["items"]
    return item["id"]


def new_doc(client, **body) -> dict:
    r = client.post("/api/documents", json={"form_id": traffic_id(client), **body})
    assert r.status_code == 201
    return r.json()


def test_상태(client):
    assert client.get("/api/health").json() == {"ok": True, "converter": True}


# ------------------------------------------------------------ 서식
def test_서식_목록과_검색(client):
    page = client.get("/api/forms").json()
    assert page["total"] == 2 and page["page"] == 1
    assert [f["title"] for f in page["items"]] == ["교통비 지급 내역서", "회의계획서"]
    found = client.get("/api/forms", params={"q": "회의", "program": "AID"}).json()
    assert [f["form_no"] for f in found["items"]] == ["7-01"]
    assert client.get("/api/forms", params={"q": "없음"}).json()["items"] == []


def test_서식_상세는_요약과_버전을_준다(client):
    detail = client.get(f"/api/forms/{traffic_id(client)}").json()
    assert detail["summary"]["required"] == 1
    assert [v["version"] for v in detail["versions"]] == [1]


def test_서식_스키마와_입력_틀(client):
    fid = traffic_id(client)
    schema = client.get(f"/api/forms/{fid}/schema").json()
    assert schema["repeats"][0]["id"] == "t0.r3-5"
    skeleton = client.get(f"/api/forms/{fid}/skeleton").json()
    assert skeleton["cells"] == {"t0.r0.c1": ""}


def test_없는_서식(client):
    assert client.get("/api/forms/999").status_code == 404
    assert client.post("/api/documents", json={"form_id": 999}).status_code == 404


def test_지난_학년도_서식_필터(client):
    assert client.get("/api/forms", params={"archived": "include"}).json()["total"] == 2
    assert client.get("/api/forms", params={"archived": "only"}).json()["total"] == 0
    assert client.get("/api/forms", params={"archived": "x"}).status_code == 422


def test_잘못된_정렬은_400(client):
    assert client.get("/api/forms", params={"sort": "x"}).status_code == 400


# ------------------------------------------------------------ 문서: 완료 기준 흐름
def test_API로_문서를_만들고_저장하고_다시_보고_내려받는다(client):
    doc = new_doc(client, title="10월 교통비")
    assert (doc["status"], doc["form"]["version"]) == ("draft", 1)

    assert (
        client.put(f"/api/documents/{doc['id']}/input", json=fx.traffic_input()).status_code == 200
    )
    again = client.get(f"/api/documents/{doc['id']}").json()
    assert again["input"] == fx.traffic_input()

    assert client.post(f"/api/documents/{doc['id']}/validate").json() == {"ok": True, "issues": []}

    out = client.post(f"/api/documents/{doc['id']}/generate").json()
    assert out["document"]["status"] == "completed"
    assert out["document"]["files"] == ["HWP", "HWPX", "PDF"]

    r = client.get(f"/api/documents/{doc['id']}/files/pdf")
    assert r.status_code == 200 and r.content.startswith(b"%PDF-")
    assert "10%EC%9B%94" in r.headers["content-disposition"]  # 파일 이름에 문서명(10월)이 들어간다

    listed = client.get("/api/documents", params={"status": "completed"}).json()
    assert [d["id"] for d in listed["items"]] == [doc["id"]]
    kinds = [e["kind"] for e in client.get(f"/api/documents/{doc['id']}/history").json()]
    assert kinds == ["created", "validated", "generated"]


def test_검증_오류는_422와_오류_목록(client):
    doc = new_doc(client)
    r = client.post(f"/api/documents/{doc['id']}/generate")
    assert r.status_code == 422
    [issue] = r.json()["issues"]
    assert (issue["code"], issue["where"], issue["location"]["kind"]) == (
        "REQUIRED_EMPTY", "t0.r0.c1", "cell",
    )  # fmt: skip


def test_서식에_맞지_않는_입력은_400(client):
    doc = new_doc(client)
    r = client.put(f"/api/documents/{doc['id']}/input", json={"rows": {"t9.r0-1": []}})
    assert r.status_code == 400 and "반복영역" in r.json()["detail"]


def test_완료_문서는_409이고_복제해서_고친다(client):
    doc = new_doc(client)
    client.put(f"/api/documents/{doc['id']}/input", json=fx.traffic_input())
    client.post(f"/api/documents/{doc['id']}/generate")
    assert client.put(f"/api/documents/{doc['id']}/input", json={}).status_code == 409
    assert client.delete(f"/api/documents/{doc['id']}").status_code == 409

    copy = client.post(f"/api/documents/{doc['id']}/copy").json()
    assert copy["status"] == "draft"
    assert client.get(f"/api/documents/{copy['id']}").json()["input"] == fx.traffic_input()
    assert client.delete(f"/api/documents/{copy['id']}").status_code == 204
    assert client.get(f"/api/documents/{copy['id']}").status_code == 404


def test_HWPX만_만들기(client):
    doc = new_doc(client)
    client.put(f"/api/documents/{doc['id']}/input", json=fx.traffic_input())
    out = client.post(f"/api/documents/{doc['id']}/generate", json={"formats": []}).json()
    assert out["document"]["files"] == ["HWPX"]
    assert client.converter.jobs == []
    assert client.get(f"/api/documents/{doc['id']}/files/hwp").status_code == 404


def test_문서_이름_바꾸기와_입력_화면용_서식(client):
    doc = new_doc(client)
    assert (
        client.patch(f"/api/documents/{doc['id']}", json={"title": "새 이름"}).json()["title"]
        == "새 이름"
    )
    form = client.get(f"/api/documents/{doc['id']}/form").json()
    assert form["summary"]["repeats"] == 1
    assert form["skeleton"]["cells"] == {"t0.r0.c1": ""}
