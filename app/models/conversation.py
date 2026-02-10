# app/models/conversation.py
# 对话元数据模型（对应 database-plan conversations）

import json
from app.extensions import db


class Conversation(db.Model):
    """对话元数据，业务主键为 chat_id"""
    __tablename__ = 'conversations'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    openid = db.Column('_openid', db.String(128), db.ForeignKey('user_profile._openid'), nullable=False)
    chat_id = db.Column(db.String(128), nullable=False, unique=True)
    title = db.Column(db.String(256), nullable=False)
    agent_ip = db.Column(db.String(64), nullable=True)
    agent_snapshot = db.Column(db.Text, nullable=True)  # JSON: { id, name, avatar, description, apiBaseUrl? }
    preview = db.Column(db.String(512), nullable=True)
    created_at = db.Column(db.BigInteger, nullable=False)
    updated_at = db.Column(db.BigInteger, nullable=False)

    __table_args__ = (
        db.Index('idx_conversations_openid_updated', '_openid', 'updated_at'),
    )

    def __repr__(self):
        return f'<Conversation {self.chat_id}>'
