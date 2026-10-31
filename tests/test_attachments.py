"""붙임 문서: 기안문에 함께 올릴 내 문서(사업 계획서 등)를 이어 둔다."""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.service import attachments, draft
from docflow.service.catalog import sync_templates
from docflow.service.db import connect
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import hwpx_factory as hf
from tests import service_fixtures as fx
from tests.test_prefill import FakeLlm

DRAFT = "test/2026_AID/1. 기안문/[AID 서식 1-03호] 일반기안문.hwpx"


@pytest.fixture
def client(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    path = settings.templates / DRAFT
    path.parent.mkdir(parents=True, exist_ok=True)
    hf.build(path, hf.body(*hf.body_field()))
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    # 본문 초안: LLM 은 붙임 줄을 어림해 `붙임 참조` 로 쓴다(실측)
    llm = FakeLlm({"text": "회의를 열고자 함.\n\n붙임 참조\n끝."})
    app = create_app(settings, converter=None, llm=llm)
    fx.make_user(app.state.sessions)
    fx.make_user(app.state.sessions, "other")
    with TestClient(app) as c:
        yield c


def new_doc(c: TestClient, q: str) -> int:
    [form] = c.get("/api/forms", params={"q": q}).json()["items"]
    return c.post("/api/documents", json={"form_id": form["id"]}).json()["id"]


def test_기안문에_내_문서를_순서대로_붙이고_뺀다(client):
    fx.login(client, "other")
    theirs = new_doc(client, "회의계획서")
    fx.login(client)
    doc = new_doc(client, "일반기안문")
    plan, traffic = new_doc(client, "회의계획서"), new_doc(client, "교통비")
    url = f"/api/documents/{doc}/attachments"

    out = client.get(url).json()
    assert out["enabled"] and out["attached"] == []
    assert {d["id"] for d in out["candidates"]} == {plan, traffic}  # 나 자신·남의 문서는 없다

    assert client.post(url, json={"document_id": traffic}).status_code == 201
    assert client.post(url, json={"document_id": plan}).status_code == 201
    assert client.post(url, json={"document_id": plan}).status_code == 400  # 한 번씩
    assert client.post(url, json={"document_id": theirs}).status_code == 400  # 남의 문서
    assert client.post(url, json={"document_id": doc}).status_code == 400  # 자기 자신
    out = client.get(url).json()
    assert [d["id"] for d in out["attached"]] == [traffic, plan]
    assert out["attached"][0]["form"]["form_no"] and out["candidates"] == []

    assert client.delete(f"{url}/{traffic}").status_code == 204
    assert client.delete(f"{url}/{traffic}").status_code == 404
    assert [d["id"] for d in client.get(url).json()["attached"]] == [plan]

    # 붙인 문서를 지우면 붙임에서도 빠진다
    assert client.delete(f"/api/documents/{plan}").status_code == 204
    assert client.get(url).json()["attached"] == []


def test_본문이_없는_서식은_붙임을_받지_않는다(client):
    fx.login(client)
    plan, traffic = new_doc(client, "회의계획서"), new_doc(client, "교통비")
    url = f"/api/documents/{plan}/attachments"
    assert client.get(url).json() == {"enabled": False, "attached": [], "candidates": []}
    assert client.post(url, json={"document_id": traffic}).status_code == 400


def test_남의_문서의_붙임은_보지_못한다(client):
    fx.login(client)
    doc = new_doc(client, "일반기안문")
    fx.login(client, "other")
    assert client.get(f"/api/documents/{doc}/attachments").status_code == 404


# ---------------------------------------------------------------- 함께 내려받기
def make(c: TestClient, doc: int, raw: dict) -> None:
    assert c.put(f"/api/documents/{doc}/input", json=raw).status_code == 200
    r = c.post(f"/api/documents/{doc}/generate", json={"formats": []})
    assert r.status_code == 200, r.text


def test_기안문과_붙임을_zip_하나로_받는다(client):
    fx.login(client)
    doc = new_doc(client, "일반기안문")
    plan, traffic = new_doc(client, "회의계획서"), new_doc(client, "교통비")
    url = f"/api/documents/{doc}/attachments"
    for other in (plan, traffic):
        assert client.post(url, json={"document_id": other}).status_code == 201
    make(client, doc, {"fields": {"content_body": "회의를 열고자 함."}})
    make(client, plan, {"cells": {"t0.r0.c1": "중간점검 회의", "t0.r1.c1": "2026.10.30"}})

    # 붙임에 HWPX 가 없는 문서가 있으면 빠뜨린 채 주지 않는다
    r = client.get(f"/api/documents/{doc}/bundle/hwpx")
    assert r.status_code == 409 and "붙임2" in r.json()["detail"]

    make(client, traffic, fx.traffic_input())
    r = client.get(f"/api/documents/{doc}/bundle/hwpx")
    assert r.status_code == 200, r.text
    assert "%EB%B6%99%EC%9E%84%ED%8F%AC%ED%95%A8.zip" in r.headers["content-disposition"]
    with zipfile.ZipFile(io.BytesIO(r.content)) as z:
        names = z.namelist()
        assert len(names) == 3 and names[0].endswith(".hwpx")
        assert names[1].startswith("붙임1_") and "회의계획서" in names[1]
        assert names[2].startswith("붙임2_") and "교통비" in names[2]
        assert z.read(names[1]).startswith(b"PK")  # 푼 HWPX 다


def test_붙임이_없거나_파일이_없으면_받지_않는다(client):
    fx.login(client)
    doc = new_doc(client, "일반기안문")
    assert client.get(f"/api/documents/{doc}/bundle/hwpx").status_code == 404  # 기안문 파일이 없다
    make(client, doc, {"fields": {"content_body": "회의를 열고자 함."}})
    assert client.get(f"/api/documents/{doc}/bundle/hwpx").status_code == 400  # 붙임이 없다


# ---------------------------------------------------------------- 본문의 붙임 줄
def test_붙임_줄은_공문_표기로_쓴다():
    assert attachments.body_lines(["사업 계획서"]) == "붙임  사업 계획서 1부.  끝."
    assert attachments.body_lines(["사업 계획서", "회의록"]) == (
        "붙임  1. 사업 계획서 1부.\n      2. 회의록 1부.  끝."
    )


def test_붙임으로_시작하는_줄부터_끝까지만_바꾼다():
    body = (
        "과제를 실시하고자 합니다.\n다. 내 용 : 붙임 사업 계획서 참조\n\n"
        "붙임 [2026 AID] 과제명 사업 계획서 1부.\n붙임 참조\n끝."
    )
    assert attachments.fill_body(body, ["사업 계획서"]) == (
        "과제를 실시하고자 합니다.\n다. 내 용 : 붙임 사업 계획서 참조\n\n"
        "붙임  사업 계획서 1부.  끝."
    )
    # 붙임 줄이 없으면 끝의 `끝.` 을 떼고 붙인다
    assert attachments.fill_body("회의를 열고자 함.  끝.", ["회의록"]) == (
        "회의를 열고자 함.\n\n붙임  회의록 1부.  끝."
    )
    # 붙인 문서가 없으면 손대지 않는다 (손으로 적은 붙임일 수 있다)
    assert attachments.fill_body("붙임  종이 서류 1부.  끝.", []) == "붙임  종이 서류 1부.  끝."


def test_본문_붙임_줄을_붙인_문서로_고친_글을_돌려준다(client):
    fx.login(client)
    doc = new_doc(client, "일반기안문")
    plan = new_doc(client, "회의계획서")
    client.patch(f"/api/documents/{plan}", json={"title": "중간점검 회의계획서"})
    client.post(f"/api/documents/{doc}/attachments", json={"document_id": plan})
    raw = {
        "fields": {"content_body": "회의를 열고자 함.\n\n붙임 [2026 AID] 과제명 계획서 1부.  끝."}
    }
    out = client.post(f"/api/documents/{doc}/attachments/body", json=raw).json()
    assert out["text"] == "회의를 열고자 함.\n\n붙임  중간점검 회의계획서 1부.  끝."
    assert client.get(f"/api/documents/{doc}").json()["input"] == {}  # 저장하지 않는다

    # 붙인 문서가 있으면 채택한 초안의 붙임은 근거가 있다 (경고하지 않는다)
    saved = {"fields": {"content_body": out["text"]}, "_drafted": {"content_body": "회의"}}
    checked = client.post(f"/api/documents/{doc}/check", json=saved).json()["issues"]
    assert [i for i in checked if i["code"] == draft.DRAFT_UNCITED] == []
    client.delete(f"/api/documents/{doc}/attachments/{plan}")
    checked = client.post(f"/api/documents/{doc}/check", json=saved).json()["issues"]
    assert [i["code"] for i in checked if i["code"] == draft.DRAFT_UNCITED] == [draft.DRAFT_UNCITED]


def test_초안과_글로_채우기의_본문도_붙임_줄을_붙인_문서로_쓴다(client):
    fx.login(client)
    doc = new_doc(client, "일반기안문")
    plan = new_doc(client, "회의계획서")
    client.patch(f"/api/documents/{plan}", json={"title": "중간점검 회의계획서"})
    client.post(f"/api/documents/{doc}/attachments", json={"document_id": plan})
    want = "회의를 열고자 함.\n\n붙임  중간점검 회의계획서 1부.  끝."

    body = {"target": "content_body", "input": {}, "instruction": "회의를 연다"}
    assert client.post(f"/api/documents/{doc}/draft", json=body).json()["text"] == want
    out = client.post(f"/api/documents/{doc}/prefill", json={"prompt": "회의를 연다"}).json()
    assert out["input"]["fields"]["content_body"] == want
    assert [f["value"] for f in out["filled"] if f["where"] == "content_body"] == [want]


def test_복제하면_붙임도_같은_순서로_넘어간다(client):
    fx.login(client)
    doc = new_doc(client, "일반기안문")
    plan, traffic = new_doc(client, "회의계획서"), new_doc(client, "교통비")
    for other in (traffic, plan):
        client.post(f"/api/documents/{doc}/attachments", json={"document_id": other})
    make(client, doc, {"fields": {"content_body": "회의를 열고자 함."}})  # 완료 -> 복제해서 고친다

    copy = client.post(f"/api/documents/{doc}/copy").json()["id"]
    url = f"/api/documents/{copy}/attachments"
    assert [d["id"] for d in client.get(url).json()["attached"]] == [traffic, plan]

    # 복제본에서 빼도 원래 문서의 붙임은 그대로다
    assert client.delete(f"{url}/{traffic}").status_code == 204
    original = client.get(f"/api/documents/{doc}/attachments").json()["attached"]
    assert [d["id"] for d in original] == [traffic, plan]
    assert [d["id"] for d in client.get(url).json()["attached"]] == [plan]
