"""계정 정보로 문서 칸 미리 채우기: 부서·전화·팩스·전자우편. 팩스는 연락처와 같은 번호다."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from docflow.config import Settings
from docflow.schema.model import FieldSpec, FormSchema, SlotSpec, TableSpec
from docflow.service import profile
from docflow.service.catalog import sync_templates
from docflow.service.db import User, connect
from docflow.service.storage import LocalFileStore
from docflow.web.app import create_app
from tests import service_fixtures as fx


def 기안() -> FormSchema:
    return FormSchema(
        title="기안",
        tables=[TableSpec(index=0, rows=5, cols=2)],
        fields=[FieldSpec(name="telephone", type="", placeholder="031-441-0000"),
                FieldSpec(name="fax", type="", placeholder="031-441-0000"),
                FieldSpec(name="email", type="", placeholder="000000@yeonsung.ac.kr"),
                FieldSpec(name="docnumber", type="", placeholder="○○○○처-000000")],
        slots=[SlotSpec(table=0, row=0, col=1, label="학과/부서명"),
               SlotSpec(table=0, row=1, col=1, label="강사 연락처"),
               SlotSpec(table=0, row=2, col=1, label="참여학과"),
               SlotSpec(table=0, row=3, col=1, label="연락처"),
               SlotSpec(table=0, row=4, col=1, label="E-mail")],
    )  # fmt: skip


def 계정(**over) -> User:
    base = {"login_id": "w", "name": "김작성", "department": "교무처", "phone": "031-441-1234",
            "email": "kim@yeonsung.ac.kr"}  # fmt: skip
    return User(**{**base, **over})


def test_작성자_정보는_누름틀과_이름이_맞는_칸에만_넣는다():
    out = profile.defaults(기안(), 계정())
    # 팩스는 계정의 연락처를 같이 쓴다
    assert out["fields"] == {"telephone": "031-441-1234", "fax": "031-441-1234",
                             "email": "kim@yeonsung.ac.kr"}  # fmt: skip
    # `강사 연락처`·`참여학과` 는 작성자가 아니다
    assert out["cells"] == {"t0.r0.c1": "교무처", "t0.r3.c1": "031-441-1234",
                            "t0.r4.c1": "kim@yeonsung.ac.kr"}  # fmt: skip


def test_계정에_없는_정보는_넣지_않는다():
    out = profile.defaults(기안(), 계정(department="", phone=None, email=None))
    assert out == {"fields": {}, "cells": {}}


def test_틀의_빈_칸만_채운다():
    skeleton = {"fields": {"telephone": "", "fax": "031-000-1111"}, "cells": {"t0.r0.c1": ""}}
    values = {"fields": {"telephone": "1", "fax": "2", "email": "3"}, "cells": {"t0.r0.c1": "4"}}
    assert profile.fill_blanks(skeleton, values) == {
        "fields": {
            "telephone": "1",
            "fax": "031-000-1111",
        },  # 박힌 값은 두고, 틀에 없는 칸은 넣지 않는다
        "cells": {"t0.r0.c1": "4"},
    }


# ---------------------------------------------------------------- API
@pytest.fixture
def client(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    sessions = connect(settings.database_url)
    with sessions() as s:
        sync_templates(s, LocalFileStore(settings.data / "files"), settings)
    app = create_app(settings, converter=None)
    fx.make_user(app.state.sessions)
    with app.state.sessions() as s:
        user = s.query(User).filter_by(login_id="writer").one()
        user.department = "컴퓨터공학과"
        s.commit()
    with TestClient(app) as c:
        fx.login(c)
        yield c


def test_새_문서의_빈_칸을_계정_정보로_채운다(client):
    [form] = client.get("/api/forms", params={"q": "교통비"}).json()["items"]
    doc = client.post("/api/documents", json={"form_id": form["id"]}).json()["id"]
    skeleton = client.get(f"/api/documents/{doc}/form").json()["skeleton"]
    assert skeleton["cells"]["t0.r0.c1"] == "컴퓨터공학과"
    assert client.get(f"/api/documents/{doc}").json()["input"] == {}  # 저장하지는 않는다
