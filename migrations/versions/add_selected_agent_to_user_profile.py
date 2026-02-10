"""add selected_agent to user_profile

Revision ID: a1b2c3d4e5f6
Revises: 8a26ae0570e8
Create Date: 2026-02-02

"""
from alembic import op
import sqlalchemy as sa


revision = 'a1b2c3d4e5f6'
down_revision = '8a26ae0570e8'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    is_sqlite = conn.dialect.name == 'sqlite'
    if is_sqlite:
        op.execute('ALTER TABLE user_profile ADD COLUMN selected_agent TEXT')
    else:
        op.add_column('user_profile', sa.Column('selected_agent', sa.Text(), nullable=True))


def downgrade():
    op.drop_column('user_profile', 'selected_agent')
