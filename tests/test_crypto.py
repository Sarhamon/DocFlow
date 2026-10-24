"""개인정보 암호화: 문서 입력값·이력 사본은 DB 에, 만든 파일은 저장소에 암호문으로만 남는다."""

from __future__ import annotations

import base64

import pytest
from cryptography.exceptions import InvalidTag
from sqlalchemy import text

from docflow.config import Settings
from docflow.service import crypto
from docflow.service.catalog import sync_templates
from docflow.service.db import Document, Form, connect
from docflow.service.documents import DocumentService
from docflow.service.storage import LocalFileStore
from tests import service_fixtures as fx
from tools import encrypt_data

RRN = "900101-1234567"


def test_같은_값도_매번_다르게_암호화하고_되돌린다():
    c = crypto.Cipher(b"k" * 32)
    a, b = c.encrypt_text(RRN), c.encrypt_text(RRN)
    assert a != b and a.startswith("enc:v1:") and RRN not in a
    assert c.decrypt_text(a) == RRN
    assert c.encrypt_text(a) == a  # 두 번 암호화하지 않는다
    assert c.decrypt_text('{"old": "평문"}') == '{"old": "평문"}'  # 예전 평문은 그대로
    data = b"%PDF-1.7 " + RRN.encode()
    sealed = c.encrypt_bytes(data)
    assert sealed.startswith(b"DFENC1") and RRN.encode() not in sealed
    assert c.decrypt_bytes(sealed) == data and c.decrypt_bytes(data) == data


def test_다른_키나_고친_암호문은_풀지_않는다():
    sealed = crypto.Cipher(b"k" * 32).encrypt_text(RRN)
    with pytest.raises(InvalidTag):
        crypto.Cipher(b"x" * 32).decrypt_text(sealed)
    with pytest.raises(ValueError):
        crypto.Cipher(b"short")


def test_키_파일이_없으면_만들고_다음에는_그것을_읽는다(tmp_path, monkeypatch):
    monkeypatch.delenv(crypto.KEY_ENV)
    settings = Settings.from_root(tmp_path)
    key = crypto.load_key(settings)
    assert len(key) == 32 and settings.key_file == tmp_path / "secrets" / "data.key"
    assert crypto.load_key(settings) == key
    monkeypatch.setenv(crypto.KEY_ENV, base64.b64encode(b"e" * 32).decode())
    assert crypto.load_key(settings) == b"e" * 32  # 환경변수가 우선한다


def test_주민등록번호_뒷자리를_가린다():
    assert crypto.mask_rrn(f'{{"주민": "{RRN}"}}') == '{"주민": "900101-1******"}'
    assert crypto.mask_rrn("9001011234567") == "900101-1******"
    assert crypto.mask_rrn("계좌 110-123-456789") == "계좌 110-123-456789"
    assert crypto.mask_rrn("901301-1234567") == "901301-1234567"  # 13월은 생년월일이 아니다


@pytest.fixture
def env(tmp_path):
    settings = Settings.from_root(tmp_path)
    fx.write_templates(settings)
    sessions = connect(settings.database_url)
    store = LocalFileStore(settings.data / "files")  # 전환 도구가 보는 자리
    with sessions() as s:
        sync_templates(s, store, settings)
    return settings, sessions, store


def raw_values(session) -> list[str]:
    rows = session.execute(text(
        "SELECT input_json FROM documents UNION ALL SELECT input_json FROM document_events "
        "WHERE input_json IS NOT NULL"
    ))  # fmt: skip
    return [r[0] for r in rows]


def test_입력값과_이력_사본은_DB_에_암호문으로만_남는다(env):
    _settings, sessions, store = env
    with sessions() as s:
        svc = DocumentService(s, store, fx.FakeConverter())
        form = s.query(Form).filter_by(title="교통비 지급 내역서").one()
        doc = svc.create(form.id)
        svc.save_input(doc.id, {**fx.traffic_input(), "replace": {"주민번호": RRN}})
        svc.validate(doc.id)
        svc.generate(doc.id)
        values = raw_values(s)
        assert values and all(v.startswith("enc:v1:") for v in values)
        assert not any(RRN in v or "컴퓨터과" in v for v in values)
        s.expire_all()
        assert svc.raw_input(s.get(Document, doc.id))["replace"] == {"주민번호": RRN}


def test_예전_평문은_읽히고_도구로_한꺼번에_암호화한다(env, monkeypatch, capsys):
    settings, sessions, store = env
    with sessions() as s:
        svc = DocumentService(s, store, fx.FakeConverter())
        form = s.query(Form).filter_by(title="교통비 지급 내역서").one()
        doc = svc.create(form.id)
        svc.save_input(doc.id, fx.traffic_input())
        svc.generate(doc.id, formats=("PDF",))
        # 암호화 전에 저장된 것처럼 되돌린다
        old = '{"cells": {"t0.r0.c1": "옛값"}}'
        s.execute(text("UPDATE documents SET input_json = :v"), {"v": old})
        s.commit()
        pdf = next(a for a in s.get(Document, doc.id).artifacts if a.format == "PDF")
        path = store.local_path(pdf.file_key)
        path.write_bytes(b"%PDF-1.7 plain")
        s.expire_all()
        assert svc.raw_input(s.get(Document, doc.id)) == {"cells": {"t0.r0.c1": "옛값"}}
        assert svc.artifact(doc.id, "pdf")[0] == b"%PDF-1.7 plain"

    monkeypatch.setattr(encrypt_data, "get_settings", lambda: settings)
    files = store.root
    encrypt_data.main([])
    assert "documents.input_json: 평문 1건" in capsys.readouterr().out
    encrypt_data.main(["--apply"])
    with sessions() as s:
        assert all(v.startswith("enc:v1:") for v in raw_values(s))
    assert all(p.read_bytes().startswith(b"DFENC1")
               for p in (files / "documents").rglob("*") if p.is_file())  # fmt: skip
    encrypt_data.main([])
    assert "평문 0건" in capsys.readouterr().out
