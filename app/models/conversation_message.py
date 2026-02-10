# app/models/conversation_message.py
# 消息明细模型（对应 database-plan conversation_messages）

from app.extensions import db


class ConversationMessage(db.Model):
    """消息明细，业务主键为 message_id"""
    __tablename__ = 'conversation_messages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    openid = db.Column('_openid', db.String(128), nullable=False)
    conversation_id = db.Column(
        db.String(128),
        db.ForeignKey('conversations.chat_id'),
        nullable=False,
    )
    message_id = db.Column(db.String(64), nullable=False, unique=True)
    speaker = db.Column(db.String(16), nullable=False)  # 'user' | 'agent'
    content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.BigInteger, nullable=False)

    __table_args__ = (
        db.Index('idx_conversation_messages_conv_created', 'conversation_id', 'created_at'),
        db.Index('idx_conversation_messages_openid', '_openid'),
    )

    def __repr__(self):
        return f'<ConversationMessage {self.message_id}>'
