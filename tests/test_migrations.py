"""DB 마이그레이션 (Alembic, M12).

빈 DB·도입 전 DB·마이그레이션만으로 만든 DB 가 모두 모델과 같아야 한다.
"""

from pathlib import Path

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect, text

from docflow.service import db
from docflow.service.db import Base, connect
from tests.conftest import TEST_DB

MIGRATIONS = Path(db.__file__).resolve().parents[1] / "migrations"


@pytest.fixture
def url(tmp_path) -> str:
    """tmp 의 SQLite, 또는 DOCFLOW_TEST_DATABASE_URL (conftest 가 테스트마다 비운다)"""
    return TEST_DB or f"sqlite:///{(tmp_path / 'a.db').as_posix()}"


def config(url: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS))
    cfg.set_main_option("sqlalchemy.url", url)
    return cfg


def head() -> str:
    return ScriptDirectory.from_config(config("sqlite://")).get_current_head()


def state(url: str) -> tuple[str | None, list]:
    """(현재 판, 모델과 다른 점)"""
    with create_engine(url).connect() as conn:
        ctx = MigrationContext.configure(conn)
        return ctx.get_current_revision(), compare_metadata(ctx, Base.metadata)


def owner_fk(url: str) -> list:
    fks = inspect(create_engine(url)).get_foreign_keys("documents")
    return [fk for fk in fks if fk["constrained_columns"] == ["owner_id"]]


def test_빈_DB는_모델대로_만들고_head로_표시한다(url):
    connect(url)
    assert state(url) == (head(), [])


def test_마이그레이션만_돌려도_모델과_같다(url):
    command.upgrade(config(url), "head")
    assert state(url) == (head(), [])


def test_도입_전_DB는_기준점에서_올리고_데이터를_지킨다(url):
    command.upgrade(config(url), db.BASELINE)
    engine = create_engine(url)
    with engine.begin() as conn:  # create_all 로 만든 옛 DB 처럼: 판 기록이 없다
        conn.execute(text("DROP TABLE alembic_version"))
        conn.execute(
            text(
                "INSERT INTO users (id, login_id, password_hash, name, department, role, status,"
                " must_change_password, failed_login_count, created_at, updated_at)"
                " VALUES (1, 'kim', 'x', '김', '', 'user', 'active', false, 0,"
                " '2026-11-01', '2026-11-01')"
            )
        )
    engine.dispose()
    assert owner_fk(url) == []

    connect(url)

    assert state(url) == (head(), [])
    assert owner_fk(url)[0]["referred_table"] == "users"
    with create_engine(url).connect() as conn:
        assert conn.execute(text("SELECT login_id FROM users")).scalar_one() == "kim"


def test_되돌리고_다시_올릴_수_있다(url):
    cfg = config(url)
    command.upgrade(cfg, "head")
    command.downgrade(cfg, db.BASELINE)
    assert owner_fk(url) == []
    command.upgrade(cfg, "head")
    assert state(url) == (head(), [])


def test_이미_head면_그대로_둔다(url):
    connect(url)
    connect(url)
    assert state(url) == (head(), [])
