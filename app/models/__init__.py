# app/models/__init__.py
from app.extensions import db

from .user import UserProfile
from .conversation import Conversation
from .conversation_message import ConversationMessage
from .agent_setting import AgentSetting
from .rag import IntentRequest, IntentResponse, QueryRequest, QueryResponse, SourceItem

__all__ = [
    'db', 
    'UserProfile', 
    'Conversation', 
    'ConversationMessage', 
    'AgentSetting',
    'IntentRequest',
    'IntentResponse',
    'QueryRequest',
    'QueryResponse',
    'SourceItem'
]
