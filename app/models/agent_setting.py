# app/models/agent_setting.py
# 用户级 agent 自定义 LLM 配置（URL、api_key 等），按 openid + agent_id 唯一

from app.extensions import db


class AgentSetting(db.Model):
    """自定义 LLM 配置，业务唯一 (openid, agent_id)"""
    __tablename__ = 'agent_setting'

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    openid      = db.Column('_openid', db.String(128), nullable=False)
    agent_id    = db.Column(db.String(128), nullable=False)
    base_url    = db.Column(db.String(512), nullable=False)
    api_key     = db.Column(db.String(512), nullable=True)
    model       = db.Column(db.String(128), nullable=True)
    chat_path   = db.Column(db.String(256), nullable=True)
    updated_at  = db.Column(db.BigInteger,  nullable=True)

    __table_args__ = (
        db.UniqueConstraint('_openid', 'agent_id', name='uq_agent_setting_openid_agent_id'),
    )

    def __repr__(self):
        return f'<AgentSetting {self.openid} {self.agent_id}>'
