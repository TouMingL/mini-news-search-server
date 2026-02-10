"""add agent_setting table

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-02-02

"""
from alembic import op
import sqlalchemy as sa


revision = 'b2c3d4e5f6a7'
down_revision = 'a1b2c3d4e5f6'
branch_labels = None
depends_on = None


def upgrade():
    conn = op.get_bind()
    is_sqlite = conn.dialect.name == 'sqlite'
    if is_sqlite:
        op.execute('''
            CREATE TABLE IF NOT EXISTS agent_setting (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                _openid TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                base_url TEXT NOT NULL,
                api_key TEXT,
                model TEXT,
                chat_path TEXT,
                updated_at INTEGER,
                UNIQUE(_openid, agent_id)
            )
        ''')
        op.execute('CREATE INDEX IF NOT EXISTS idx_agent_setting_openid ON agent_setting(_openid)')
    else:
        op.create_table(
            'agent_setting',
            sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
            sa.Column('_openid', sa.String(128), nullable=False),
            sa.Column('agent_id', sa.String(128), nullable=False),
            sa.Column('base_url', sa.String(512), nullable=False),
            sa.Column('api_key', sa.String(512), nullable=True),
            sa.Column('model', sa.String(128), nullable=True),
            sa.Column('chat_path', sa.String(256), nullable=True),
            sa.Column('updated_at', sa.BigInteger(), nullable=True),
            sa.PrimaryKeyConstraint('id'),
            sa.UniqueConstraint('_openid', 'agent_id', name='uq_agent_setting_openid_agent_id'),
        )
        op.create_index('idx_agent_setting_openid', 'agent_setting', ['_openid'], unique=False)


def downgrade():
    op.drop_index('idx_agent_setting_openid', table_name='agent_setting')
    op.drop_table('agent_setting')
