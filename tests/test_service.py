"""서식 목록·문서 서비스 테스트. DB 는 임시 SQLite, 파일 저장소는 임시 폴더다."""

from __future__ import annotations

import json

import pytest

from docflow.config import Settings
from docflow.pipeline import InputError
from docflow.service import catalog
from docflow.service.catalog import NotFound, sync_templates
from docflow.service.db import Form, connect
from docflow.service.documents import Conflict, DocumentService, ValidationFailed, safe_name
from docflow.service.storage import LocalFileStore
from docflow.validate import REQUIRED_EMPTY
from tests import hwpx_factory as hf
from tests import service_fixtures as fx


@pytest.fixture
def env(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    sessions = connect(f"sqlite:///{(tmp_path / 'db.sqlite').as_posix()}")
    store = LocalFileStore(tmp_path / "files")
    with sessions() as s:
        sync_templates(s, store, settings)
    return settings, sessions, store


@pytest.fixture
def session(env):
    _settings, sessions, _store = env
    with sessions() as s:
        yield s


def form_id(session, title: str) -> int:
    return session.query(Form).filter(Form.title == title).one().id


def service(env, session, converter=None) -> DocumentService:
    return DocumentService(session, env[2], converter)


# ------------------------------------------------------------ 서식 등록
def test_서식만_등록하고_목차는_뺀다(session):
    forms = session.query(Form).order_by(Form.form_no).all()
    assert [(f.program, f.form_no, f.title, f.category) for f in forms] == [
        ("AID", "5-05", "교통비 지급 내역서", "학생지원비"),
        ("AID", "7-01", "회의계획서", "회의비"),
    ]
    assert all(f.latest.version == 1 for f in forms)


def test_바뀌지_않은_서식은_다시_등록하지_않는다(env, session):
    settings, _sessions, store = env
    report = sync_templates(session, store, settings)
    assert (report.added, report.updated, report.unchanged) == ([], [], 2)


def test_템플릿이_바뀌면_새_버전을_쌓고_옛_버전을_보존한다(env, session):
    settings, _sessions, store = env
    fx.write_templates(settings, meeting_title="○○○ 정기회의")
    report = sync_templates(session, store, settings)
    assert report.updated == [fx.MEETING]
    form = session.get(Form, form_id(session, "회의계획서"))
    assert [v.version for v in form.versions] == [1, 2]
    old, new = (catalog.load_schema(v) for v in form.versions)
    assert old.slots[0].placeholder == "○○○ 회의"
    assert new.slots[0].placeholder == "○○○ 정기회의"
    assert store.exists(form.versions[0].template_key)


BUNDLE = "test/2026_AID/4. 인건비/[AID 서식 4-01~02호] 인건비 확인서 등.hwpx"


def _write_bundle(settings: Settings, *, numbered: bool = True) -> None:
    def mark(no: str) -> str:
        return f"[ AID 서식 {no}호 ]" if numbered else "머리"

    table = hf.para_runs(f'<hp:run charPrIDRef="0">{fx.meeting_form()}</hp:run>')
    xml = hf.body(hf.para(mark("4-01")), hf.para("입금계좌 확인서"), table,
                  hf.para(mark("4-02")), hf.para("강의시간 배당표"), table)  # fmt: skip
    path = settings.templates / BUNDLE
    path.parent.mkdir(parents=True, exist_ok=True)
    hf.build(path, xml)


def test_묶음_파일은_서식_번호마다_따로_등록하고_통째_항목은_숨긴다(env, session):
    settings, _sessions, store = env
    _write_bundle(settings, numbered=False)
    sync_templates(session, store, settings)
    whole = session.query(Form).filter(Form.key == BUNDLE).one()
    assert whole.public

    _write_bundle(settings)
    report = sync_templates(session, store, settings)
    assert report.added == [f"{BUNDLE}#4-01호", f"{BUNDLE}#4-02호"]
    assert report.hidden == [BUNDLE] and not whole.public
    parts = session.query(Form).filter(Form.key.startswith(f"{BUNDLE}#")).order_by(Form.key).all()
    assert [(f.form_no, f.title) for f in parts] == [("4-01", "입금계좌 확인서"),
                                                     ("4-02", "강의시간 배당표")]  # fmt: skip
    assert catalog.load_schema(parts[1].latest).variant == 1
    summary = catalog.summarize(catalog.load_schema(parts[1].latest))
    assert (summary["variants"], summary["tables"], summary["cells"]) == (0, 1, 2)

    again = sync_templates(session, store, settings)
    assert (again.added, again.updated, again.hidden) == ([], [], [])


def test_스키마_형식이_바뀌면_템플릿이_같아도_다시_뽑는다(env, session):
    settings, _sessions, store = env
    form = session.get(Form, form_id(session, "회의계획서"))
    data = json.loads(form.latest.schema_json)
    form.latest.schema_json = json.dumps({**data, "schema_version": 2})
    session.commit()
    report = sync_templates(session, store, settings)
    assert report.updated == [fx.MEETING]


# ------------------------------------------------------------ 서식 찾기
def test_키워드와_사업으로_찾는다(session):
    items, total = catalog.search_forms(session, q="교통", program="AID")
    assert total == 1 and items[0].title == "교통비 지급 내역서"
    items, total = catalog.search_forms(session, q="7-01")
    assert [f.title for f in items] == ["회의계획서"]
    assert catalog.search_forms(session, q="없는서식") == ([], 0)


def test_비공개_서식은_보이지_않는다(session):
    form = session.get(Form, form_id(session, "회의계획서"))
    form.public = False
    session.commit()
    assert catalog.search_forms(session)[1] == 1
    with pytest.raises(NotFound):
        catalog.get_form(session, form.id)


def test_페이지와_정렬(session):
    items, total = catalog.search_forms(session, sort="title", size=1, page=2)
    assert total == 2 and [f.title for f in items] == ["회의계획서"]
    with pytest.raises(ValueError, match="정렬"):
        catalog.search_forms(session, sort="이상한")


def test_사업_카테고리별_서식_수(session):
    assert catalog.facets(session) == [
        {
            "program": "AID",
            "count": 2,
            "categories": [
                {"no": 5, "name": "학생지원비", "count": 1},
                {"no": 7, "name": "회의비", "count": 1},
            ],
        }
    ]


def test_채울_항목_수_요약(session):
    form = session.get(Form, form_id(session, "교통비 지급 내역서"))
    summary = catalog.summarize(catalog.load_schema(form.latest))
    assert summary == {
        "fields": 0, "cells": 1, "required": 1, "repeats": 1, "variants": 0, "tables": 1,
    }  # fmt: skip


# ------------------------------------------------------------ 문서
def test_새_문서는_최신_버전에_묶이고_서식이_바뀌어도_그대로다(env, session):
    svc = service(env, session)
    fid = form_id(session, "회의계획서")
    old = svc.create(fid)
    fx.write_templates(env[0], meeting_title="○○○ 정기회의")
    sync_templates(session, env[2], env[0])
    new = svc.create(fid)
    assert (old.form_version.version, new.form_version.version) == (1, 2)
    assert svc.schema_of(old).slots[0].placeholder == "○○○ 회의"


def test_임시저장과_불러오기(env, session):
    svc = service(env, session)
    doc = svc.create(form_id(session, "교통비 지급 내역서"), title="10월 교통비")
    svc.save_input(doc.id, fx.traffic_input())
    again = svc.get(doc.id)
    assert again.title == "10월 교통비"
    assert svc.raw_input(again) == fx.traffic_input()


def test_서식에_맞지_않는_입력은_저장하지_않는다(env, session):
    svc = service(env, session)
    doc = svc.create(form_id(session, "교통비 지급 내역서"))
    with pytest.raises(InputError, match="없는 열"):
        svc.save_input(doc.id, {"rows": {"t0.r3-5": [{"주소": "x"}]}})
    assert svc.raw_input(svc.get(doc.id)) == {}


def test_검증_결과를_이력에_남긴다(env, session):
    svc = service(env, session)
    doc = svc.create(form_id(session, "교통비 지급 내역서"))
    issues = svc.validate(doc.id)
    assert [(i.code, i.where) for i in issues] == [(REQUIRED_EMPTY, "t0.r0.c1")]
    event = svc.history(doc.id)[-1]
    assert event.kind == "validated"
    assert json.loads(event.issues_json)[0]["where"] == "t0.r0.c1"


def test_검증_오류가_있으면_만들지_않는다(env, session):
    svc = service(env, session, fx.FakeConverter())
    doc = svc.create(form_id(session, "교통비 지급 내역서"))
    with pytest.raises(ValidationFailed) as err:
        svc.generate(doc.id)
    assert [i.where for i in err.value.issues] == ["t0.r0.c1"]
    assert svc.get(doc.id).revision == 0 and svc.get(doc.id).artifacts == []


def test_만들면_산출물을_저장하고_완료된다(env, session):
    converter = fx.FakeConverter()
    svc = service(env, session, converter)
    doc = svc.create(form_id(session, "교통비 지급 내역서"), title="10월 교통비")
    svc.save_input(doc.id, fx.traffic_input())
    out = svc.generate(doc.id)
    assert (out.revision, out.document.status, out.conversion_errors) == (1, "completed", {})
    assert sorted(a.format for a in out.document.artifacts) == ["HWP", "HWPX", "PDF"]

    data, name = svc.artifact(doc.id, "pdf")
    assert data.startswith(b"%PDF-")
    # 저장소에는 암호화해 둔다 (주민등록번호가 든 문서일 수 있다)
    [pdf] = [a for a in out.document.artifacts if a.format == "PDF"]
    stored = env[2].local_path(pdf.file_key).read_bytes()
    assert stored.startswith(b"DFENC1") and b"%PDF-" not in stored
    assert name.startswith("AID 5-05_10월 교통비_") and name.endswith(".pdf")

    event = svc.history(doc.id)[-1]
    assert (event.kind, event.note) == ("generated", "1판")
    # 줄인 교통비 서식에는 날짜 칸이 없다. 입금액 열이 금액이라 금액 형식 규칙이 붙는다
    assert json.loads(event.rules_json) == ["AMOUNT_INVALID", "REQUIRED_EMPTY"]
    assert json.loads(event.input_json) == fx.traffic_input()


def test_완료된_문서는_고치지_않고_복제한다(env, session):
    svc = service(env, session, fx.FakeConverter())
    doc = svc.create(form_id(session, "교통비 지급 내역서"))
    svc.save_input(doc.id, fx.traffic_input())
    svc.generate(doc.id)
    with pytest.raises(Conflict):
        svc.save_input(doc.id, fx.traffic_input("이영희"))
    with pytest.raises(Conflict):
        svc.delete(doc.id)
    copy = svc.copy(doc.id)
    assert (copy.status, copy.form_version_id) == ("draft", doc.form_version_id)
    assert svc.raw_input(copy) == fx.traffic_input()
    svc.save_input(copy.id, fx.traffic_input("이영희"))


def test_변환이_일부_실패하면_HWPX를_보존하고_작성_중으로_둔다(env, session):
    svc = service(env, session, fx.FakeConverter(fail=("PDF",)))
    doc = svc.create(form_id(session, "교통비 지급 내역서"))
    svc.save_input(doc.id, fx.traffic_input())
    out = svc.generate(doc.id)
    assert out.document.status == "draft"
    assert out.conversion_errors == {"PDF": "가짜 실패"}
    assert svc.artifact(doc.id, "hwpx")[0].startswith(b"PK")
    with pytest.raises(NotFound):
        svc.artifact(doc.id, "pdf")
    # 다시 만들면 다음 판
    svc.converter.inner.fail = ()
    assert svc.generate(doc.id).revision == 2


def test_변환기가_없으면_HWPX만_만들_수_있다(env, session):
    svc = service(env, session)
    doc = svc.create(form_id(session, "교통비 지급 내역서"))
    svc.save_input(doc.id, fx.traffic_input())
    assert svc.generate(doc.id).conversion_errors.keys() == {"HWP", "PDF"}
    out = svc.generate(doc.id, formats=())
    assert out.document.status == "completed"


def test_작성_중_문서를_지우면_파일도_지운다(env, session):
    svc = service(env, session, fx.FakeConverter(fail=("PDF",)))
    doc = svc.create(form_id(session, "교통비 지급 내역서"))
    svc.save_input(doc.id, fx.traffic_input())
    svc.generate(doc.id)
    [hwpx] = [a for a in svc.get(doc.id).artifacts if a.format == "HWPX"]
    svc.delete(doc.id)
    assert not env[2].exists(hwpx.file_key)
    with pytest.raises(NotFound):
        svc.get(doc.id)


def test_남의_문서는_없는_것으로_다룬다(env, session):
    svc = service(env, session)
    mine = svc.create(form_id(session, "교통비 지급 내역서"), owner_id=1)
    with pytest.raises(NotFound):
        svc.get(mine.id, owner_id=2)
    assert svc.list(owner_id=2) == ([], 0)
    assert svc.list(owner_id=1)[1] == 1


def test_내_문서함은_상태로_거른다(env, session):
    svc = service(env, session, fx.FakeConverter())
    fid = form_id(session, "교통비 지급 내역서")
    done = svc.create(fid)
    svc.save_input(done.id, fx.traffic_input())
    svc.generate(done.id)
    svc.create(fid)
    assert [d.status for d in svc.list(status="completed")[0]] == ["completed"]
    assert svc.list(status="draft")[1] == 1
    with pytest.raises(ValueError):
        svc.list(status="이상한")


def test_파일_이름에_못_쓰는_글자는_바꾼다():
    assert safe_name('10/12 "교통비"?') == "10_12 _교통비"
    assert safe_name("  ") == "문서"
