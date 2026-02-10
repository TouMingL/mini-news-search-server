"""add agent_snapshot to conversations

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-02-02

"""
from alembic import op
import sqlalchemy as sa


revision = 'c3d4e5f6a7b8'
down_revision = 'b2c3d4e5f6a7'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    is_sqlite = conn.dialect.name == 'sqlite'
    if is_sqlite:
        op.execute('ALTER TABLE conversations ADD COLUMN agent_snapshot TEXT')
    else:
        op.add_column('conversations', sa.Column('agent_snapshot', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('conversations', 'agent_snapshot')
