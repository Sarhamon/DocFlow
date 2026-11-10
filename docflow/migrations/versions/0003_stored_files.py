"""stored_files: DB 에 두는 파일 (S3 대신, M12)

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-10 12:00:00
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Alembic 도입 전 DB 는 올리기 전에 create_all 로 빠진 표를 만든다(db.migrate) — 이미 있으면 둔다
    if sa.inspect(op.get_bind()).has_table('stored_files'):
        return
    op.create_table(
        'stored_files',
        sa.Column('key', sa.String(length=512), nullable=False),
        sa.Column('data', sa.LargeBinary(), nullable=False),
        sa.Column('size', sa.Integer(), nullable=False),
        sa.Column('sha256', sa.String(length=64), nullable=False),
        sa.Column('updated_at', sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint('key'),
    )


def downgrade() -> None:
    op.drop_table('stored_files')
