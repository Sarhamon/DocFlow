"""서비스·API 테스트용 서식 폴더. 코드로 만든 서식을 templates/ 구조대로 둔다."""

from __future__ import annotations

from pathlib import Path

from docflow.config import Settings
from docflow.convert import SIGNATURES, ConversionJob, ConversionResult
from docflow.service.auth import AuthService, hash_password
from tests import hwpx_factory as hf
from tests.test_schema_extract import tbl, 교통비

TRAFFIC = "test/2026_AID/5. 학생지원비/[AID 서식 5-05호] 교통비 지급 내역서.hwpx"
MEETING = "test/2026_AID/7. 회의비/[AID 서식 7-01호] 회의계획서.hwpx"
INDEX = "test/2026_AID/[AID] 서식 목차.hwpx"


def meeting_form() -> str:
    """회의계획서: 회의명(필수) + 일시(날짜)."""
    return tbl(
        2, 2,
        (0, 0, "회의명"), (0, 1, "○○○ 회의"),
        (1, 0, "일시"), (1, 1, "2000.00.00"),
    )  # fmt: skip


def write_templates(settings: Settings, *, meeting_title: str = "○○○ 회의") -> None:
    """서식 2개와 서식이 아닌 목차 1개를 templates/ 에 둔다."""
    forms = {
        TRAFFIC: hf.section(교통비()),
        MEETING: hf.section(meeting_form().replace("○○○ 회의", meeting_title)),
        INDEX: hf.section(tbl(1, 1, (0, 0, "목차"))),
    }
    for rel, xml in forms.items():
        path = settings.templates / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        hf.build(path, xml)


def traffic_input(name: str = "김철수") -> dict:
    return {"cells": {"t0.r0.c1": "컴퓨터과"}, "rows": {"t0.r3-5": [{"성명": name}]}}


#: 테스트 계정 비밀번호 (규칙을 지킨다)
PASSWORD = "Test#pass1"


def make_user(sessions, login_id: str = "writer", *, role: str = "user") -> int:
    """비밀번호를 이미 바꾼 계정을 만든다 (CLI 로 발급한 것과 같다). 계정 ID 를 돌려준다."""
    with sessions() as s:
        user, _ = AuthService(s).create_user(None, login_id, login_id, role=role)
        user.password_hash = hash_password(PASSWORD)
        user.must_change_password = False
        s.commit()
        return user.id


def login(client, login_id: str = "writer", password: str = PASSWORD):
    """client 의 쿠키에 세션을 심는다."""
    r = client.post("/api/auth/login", json={"login_id": login_id, "password": password})
    assert r.status_code == 200, r.text
    return r


class FakeConverter:
    """산출물을 형식별 시작 바이트로 만든다. fail 에 든 형식은 실패한다."""

    def __init__(self, fail: tuple[str, ...] = ()) -> None:
        self.fail = fail
        self.jobs: list[ConversionJob] = []

    def convert(self, job: ConversionJob) -> ConversionResult:
        self.jobs.append(job)
        result = ConversionResult(job=job, attempts=1)
        for fmt in job.formats:
            if fmt in self.fail:
                result.errors[fmt] = "가짜 실패"
                continue
            target: Path = job.target(fmt)
            target.write_bytes(SIGNATURES[fmt] + b" fake")
            result.outputs[fmt] = target
        return result
