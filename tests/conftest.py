"""테스트 공통 설정. 개인정보 암호 키는 고정 값을 쓴다 (키 파일을 만들지 않도록).

DB 는 테스트마다 tmp 의 SQLite 다. DOCFLOW_TEST_DATABASE_URL 을 주면 그 DB(PostgreSQL)로 돌린다.
테스트마다 스키마를 비우니 버려도 되는 DB 만 준다 (예: Docker 의 postgres:16, 포트 55432):

    DOCFLOW_TEST_DATABASE_URL=postgresql+psycopg://docflow:docflow@localhost:55432/docflow_test
"""

import base64
import os

import pytest
from sqlalchemy import create_engine, text

os.environ.setdefault("DOCFLOW_DATA_KEY", base64.b64encode(b"t" * 32).decode("ascii"))
# 셸에 운영 DB 주소·버킷이 있어도 테스트가 거기에 쓰지 않게 지운다.
os.environ.pop("DOCFLOW_DATABASE_URL", None)
os.environ.pop("DOCFLOW_S3_BUCKET", None)
TEST_DB = os.environ.get("DOCFLOW_TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _test_database(monkeypatch):
    if not TEST_DB:
        yield
        return
    engine = create_engine(TEST_DB)
    with engine.begin() as conn:
        # 앞 테스트가 열어 둔 연결이 표를 잡고 있으면 DROP 이 멈춘다
        conn.execute(
            text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity"
                " WHERE datname = current_database() AND pid <> pg_backend_pid()"
            )
        )
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))
    engine.dispose()
    monkeypatch.setenv("DOCFLOW_DATABASE_URL", TEST_DB)
    yield
