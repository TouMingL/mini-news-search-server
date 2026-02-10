# app/models/user.py
# 用户资料模型（对应 database-plan user_profile）

import json
from app.extensions import db


class UserProfile(db.Model):
    """用户资料模型，业务主键为 _openid"""
    __tablename__ = 'user_profile'

    openid = db.Column('_openid', db.String(128), primary_key=True)
    nick_name = db.Column(db.String(128), nullable=False)
    avatar_url = db.Column(db.String(512), nullable=True)
    join_time = db.Column(db.BigInteger, nullable=False)
    agent_count = db.Column(db.Integer, default=0)
    conversation_count = db.Column(db.Integer, default=0)
    updated_at = db.Column(db.BigInteger, nullable=True)
    selected_agent = db.Column(db.Text, nullable=True)  # JSON: { id, name, avatar, description, bgClass } or null

    def __repr__(self):
        return f'<UserProfile {self.openid}>'

    def to_dict(self):
        """转换为字典格式（与前端 /api/user/info 一致）"""
        join_date_str = None
        if self.join_time:
            from datetime import datetime
            dt = datetime.utcfromtimestamp(self.join_time / 1000.0)
            join_date_str = dt.strftime('%Y-%m-%d')
        selected_agent_val = None
        if self.selected_agent:
            try:
                selected_agent_val = json.loads(self.selected_agent) if isinstance(self.selected_agent, str) else self.selected_agent
            except (TypeError, ValueError):
                pass
        return {
            'openid': self.openid,
            'nickName': self.nick_name,
            'avatarUrl': self.avatar_url,
            'joinDate': join_date_str,
            'agentCount': self.agent_count,
            'conversationCount': self.conversation_count,
            'selectedAgent': selected_agent_val,
        }
