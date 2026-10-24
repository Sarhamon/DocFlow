"""실제 서식(templates/)으로 웹 API 를 끝까지 돌려 보는 로컬 회귀 테스트 (M6 완료 기준).

내부 자료가 필요해서 `pytest -m local` 로만 돈다. 데이터(DB·파일)는 임시 폴더에 둔다.

1. templates/ 의 서식을 번호별로 234개 등록하고(묶음 12개를 나눔), 다시 등록해도 버전이 늘지 않는다.
2. 수직 슬라이스 서식 3종을 API 만으로 만들기 → 임시저장 → 재조회 → 만들기 → 내려받기.
   한컴오피스가 있으면 HWP·PDF 까지, 없으면 HWPX 까지.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from docflow.adapters.hancom import available
from docflow.config import Settings, get_settings
from docflow.service.catalog import sync_templates
from docflow.service.db import connect
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import service_fixtures as fx

pytestmark = pytest.mark.local

#: 파일 183개 중 묶음 12개를 서식 번호별로 나눠 234개
EXPECTED_FORMS = 234

#: 입력 예시 -> (검색어, 서식 번호)
SLICE = {
    "교통비_지급_내역서": ("교통비 지급 내역서", "5-05"),
    "정산서": ("정산서", "3-01"),
    "일반기안문_1-03호": ("일반기안문(샘플 10종)", "1-03"),
}


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    base = get_settings()
    if not any(base.templates.rglob("*.hwpx")):
        pytest.skip("templates/ 가 없다")
    settings = Settings.from_root(base.root, data=tmp_path_factory.mktemp("data"))
    store = LocalFileStore(settings.data / "files")
    with connect(settings.database_url)() as s:
        first = sync_templates(s, store, settings)
        again = sync_templates(s, store, settings)
    assert (len(first.added), first.failed) == (EXPECTED_FORMS, [])
    assert (again.added, again.updated, again.unchanged) == ([], [], 183)
    app = create_app(settings, store=store)
    fx.make_user(app.state.sessions)
    with TestClient(app) as c:
        fx.login(c)
        yield c


def test_서식을_모두_등록하고_사업별로_나뉜다(client):
    every = client.get("/api/forms", params={"archived": "include"}).json()
    assert every["total"] == EXPECTED_FORMS
    programs = {f["program"] for f in client.get("/api/forms/facets").json()}
    assert programs == {"AID", "앵커(신산업)", "혁신"}


@pytest.mark.parametrize("name", SLICE)
def test_수직_슬라이스_서식을_API로_끝까지_만든다(client, name):
    path = get_settings().root / "examples" / "ysu_forms" / "m5" / f"{name}.json"
    if not path.exists():
        pytest.skip(f"입력 예시가 없다: {path}")
    query, number = SLICE[name]
    found = client.get("/api/forms", params={"q": query, "program": "AID"}).json()["items"]
    [form] = [f for f in found if f["form_no"] == number]

    doc = client.post("/api/documents", json={"form_id": form["id"]}).json()
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert client.put(f"/api/documents/{doc['id']}/input", json=raw).status_code == 200
    assert client.get(f"/api/documents/{doc['id']}").json()["input"] == raw

    formats = ["HWP", "PDF"] if available() else []
    r = client.post(f"/api/documents/{doc['id']}/generate", json={"formats": formats})
    assert r.status_code == 200, r.json()
    out = r.json()
    assert out["conversion_errors"] == {} and out["skipped_formulas"] == []
    assert out["document"]["status"] == "completed"
    for fmt in ["HWPX", *formats]:
        got = client.get(f"/api/documents/{doc['id']}/files/{fmt}")
        assert got.status_code == 200 and len(got.content) > 1000
