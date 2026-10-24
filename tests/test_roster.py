"""명단 가져오기 (M10, F2-d): 학생이 낸 입금계좌 확인서에서 명단을 읽어 서식의 명단에 넣는다."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.hwpx.document import HwpxDocument
from docflow.schema.extract import extract
from docflow.service.catalog import sync_templates
from docflow.service.db import connect
from docflow.service.roster import concept, read_rosters, target_mapping, to_rows
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import hwpx_factory as hf
from tests import service_fixtures as fx
from tests.test_schema_extract import tbl, 교통비


def 확인서(*people: tuple[str, str, str, str, str]) -> str:
    """입금계좌 확인서 (4-01호 모양): 한 줄 머리, 예시 줄, 사람들, 합계."""
    rows = [(0, 0, "연번"), (0, 1, "성명 (예금주)"), (0, 2, "주민등록번호"), (0, 3, "은행명"),
            (0, 4, "계좌번호"), (0, 5, "입금액(원)"),
            (1, 0, "1"), (1, 1, "000"), (1, 2, "000000-0000000"), (1, 3, "국민"),
            (1, 4, "000-00-0000"), (1, 5, "0")]  # fmt: skip
    for i, (name, rrn, bank, account, amount) in enumerate(people, 2):
        rows += [(i, 0, str(i)), (i, 1, name), (i, 2, rrn), (i, 3, bank), (i, 4, account),
                 (i, 5, amount)]  # fmt: skip
    last = len(people) + 2
    rows += [(last, 0, ""), (last, 1, ""), (last, 2, ""), (last, 3, ""), (last, 4, ""),
             (last, 5, ""), (last + 1, 0, "합계", 5, 1), (last + 1, 5, "")]  # fmt: skip
    return tbl(last + 2, 6, *rows)


KIM = ("김철수", "900101-1234567", "국민", "123-45-678901", "5,000")
LEE = ("이영희", "010203-4123456", "신한", "110-123-456789", "7,000")


def doc_of(tmp_path, body: str, name: str = "f.hwpx") -> HwpxDocument:
    return HwpxDocument.open(hf.build(tmp_path / name, hf.section(body)))


@pytest.mark.parametrize(
    ("label", "want"),
    [("성명 (예금주)", "name"), ("교통비 입금계좌 은행명", "bank"),
     ("입금계좌 계좌번호", "account"), ("교통비 입금계좌 입금액(원)", "amount"),
     ("주민등록번호", "rrn"), ("학번", "student_id"), ("연번", None), ("서명", None)],
)  # fmt: skip
def test_열_이름으로_뜻을_찾는다(label, want):
    assert concept(label) == want


def test_확인서의_사람들을_읽고_예시·빈_줄·합계는_뺀다(tmp_path):
    [roster] = read_rosters(doc_of(tmp_path, 확인서(KIM, LEE)))
    assert roster.labels == {"name": "성명 (예금주)", "rrn": "주민등록번호", "bank": "은행명",
                             "account": "계좌번호", "amount": "입금액(원)"}  # fmt: skip
    assert [r["name"] for r in roster.records] == ["김철수", "이영희"]
    assert roster.records[0] == {"name": "김철수", "rrn": "900101-1234567", "bank": "국민",
                                 "account": "123-45-678901", "amount": "5,000"}  # fmt: skip


def test_두_줄_머리도_읽는다(tmp_path):
    """교통비 내역서처럼 `입금계좌` 아래 `은행명 / 입금액` 이 나뉜 표.

    예시 줄(홍길동)과 미리 적힌 금액만 있는 줄은 뺀다.
    """
    [roster] = read_rosters(doc_of(tmp_path, 교통비()))
    assert roster.labels == {
        "name": "성명", "bank": "입금계좌 은행명", "amount": "입금계좌 입금액(원)"}  # fmt: skip
    assert roster.records == []


def test_명단_표가_없으면_읽지_않는다(tmp_path):
    assert read_rosters(doc_of(tmp_path, fx.meeting_form())) == []


def test_받는_명단의_열에_뜻대로_넣고_같은_사람은_한_번만(tmp_path):
    [rep] = extract(doc_of(tmp_path, 교통비())).repeats
    assert [(m.col, m.concept) for m in target_mapping(rep)] == [
        (0, None), (1, "name"), (2, "bank"), (3, "amount")]  # fmt: skip
    records = read_rosters(doc_of(tmp_path, 확인서(KIM, LEE, KIM), "a.hwpx"))[0].records
    assert to_rows(rep, records) == [
        {"1": "김철수", "2": "국민", "3": "5,000"},
        {"1": "이영희", "2": "신한", "3": "7,000"},
    ]


# ------------------------------------------------------------ API
@pytest.fixture
def client(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    app = create_app(settings, converter=None)
    fx.make_user(app.state.sessions)
    with TestClient(app) as c:
        fx.login(c)
        yield c


def upload(tmp_path, name: str, *people) -> tuple[str, bytes]:
    return (name, hf.build(tmp_path / name, hf.section(확인서(*people))).read_bytes())


def meeting_bytes(tmp_path) -> bytes:
    return hf.build(tmp_path / "meeting.hwpx", hf.section(fx.meeting_form())).read_bytes()


def test_여러_확인서를_올리면_명단으로_읽고_저장하지는_않는다(client, tmp_path):
    [form] = client.get("/api/forms", params={"q": "교통비"}).json()["items"]
    doc = client.post("/api/documents", json={"form_id": form["id"]}).json()["id"]
    files = [("files", upload(tmp_path, "김철수.hwpx", KIM)),
             ("files", upload(tmp_path, "이영희.hwpx", LEE, KIM)),
             ("files", ("메모.txt", b"hello")),
             ("files", ("빈 서식.hwpx", meeting_bytes(tmp_path))),
             ("files", ("옛 파일.hwp", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1 hwp"))]  # fmt: skip
    r = client.post(f"/api/documents/{doc}/roster", params={"repeat": "t0.r3-5"}, files=files)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["rows"] == [{"1": "김철수", "2": "국민", "3": "5,000"},
                           {"1": "이영희", "2": "신한", "3": "7,000"}]  # fmt: skip
    assert out["duplicates"] == 1
    got = [(f["name"], f["people"], bool(f["error"])) for f in out["files"]]
    assert got == [("김철수.hwpx", 1, False), ("이영희.hwpx", 2, False), ("메모.txt", 0, True),
                   ("빈 서식.hwpx", 0, True), ("옛 파일.hwp", 0, True)]  # fmt: skip
    assert "HWPX" in out["files"][4]["error"]  # 한글이 없으면 HWP 는 못 읽는다
    assert [(m["label"], m["found"]) for m in out["mapping"]] == [
        ("연번", False), ("성명", True), ("입금계좌 은행명", True),
        ("입금계좌 입금액(원)", True)]  # fmt: skip
    assert client.get(f"/api/documents/{doc}").json()["input"] == {}  # 저장하지 않는다


def test_없는_명단이나_남의_문서는_안_된다(app_and_other, tmp_path):
    client, other_doc = app_and_other
    files = [("files", upload(tmp_path, "a.hwpx", KIM))]
    assert client.post(f"/api/documents/{other_doc}/roster", params={"repeat": "t0.r3-5"},
                       files=files).status_code == 404  # fmt: skip


@pytest.fixture
def app_and_other(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    with connect(settings.database_url)() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    app = create_app(settings, converter=None)
    fx.make_user(app.state.sessions)
    fx.make_user(app.state.sessions, "other")
    with TestClient(app) as other, TestClient(app) as me:
        fx.login(other, "other")
        [form] = other.get("/api/forms", params={"q": "교통비"}).json()["items"]
        other_doc = other.post("/api/documents", json={"form_id": form["id"]}).json()["id"]
        fx.login(me)
        mine = me.post("/api/documents", json={"form_id": form["id"]}).json()["id"]
        r = me.post(f"/api/documents/{mine}/roster", params={"repeat": "t9.r1-2"},
                    files=[("files", upload(tmp_path, "b.hwpx", KIM))])  # fmt: skip
        assert r.status_code == 404
        yield me, other_doc
