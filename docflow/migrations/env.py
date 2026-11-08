"""Alembic 실행 환경.

DB 주소는 Alembic 설정(sqlalchemy.url)이 있으면 그것을, 없으면 docflow 설정을 쓴다.

SQLite 는 칸·제약을 바꾸려면 표를 새로 만들어야 해서 batch 모드로 돌린다.
"""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from docflow.config import get_settings
from docflow.service.db import Base

config = context.config
url = config.get_main_option("sqlalchemy.url") or get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(url=url, target_metadata=Base.metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:  # docflow.service.db.connect 가 넘긴 연결
        _run(connection)
        return
    # 앱 엔진과 달리 SQLite 외래 키 검사를 켜지 않는다. 표를 새로 만드는 batch 가 막히지 않게
    with create_engine(url).connect() as connection:
        _run(connection)


def _run(connection) -> None:
    context.configure(
        connection=connection, target_metadata=Base.metadata, render_as_batch=True
    )
    with context.begin_transaction():
        context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
