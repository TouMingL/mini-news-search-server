-- SQLite schema per database-plan (optional init without ORM)
-- Usage: sqlite3 instance/database.db < docs/sqlite_schema.sql

-- 1. user_profile
CREATE TABLE IF NOT EXISTS user_profile (
    _openid     TEXT NOT NULL PRIMARY KEY,
    nick_name   TEXT NOT NULL,
    avatar_url  TEXT,
    join_time   INTEGER NOT NULL,
    agent_count INTEGER DEFAULT 0,
    conversation_count INTEGER DEFAULT 0,
    updated_at  INTEGER
);

-- 2. conversations
CREATE TABLE IF NOT EXISTS conversations (
    id          INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    _openid     TEXT NOT NULL REFERENCES user_profile(_openid),
    chat_id     TEXT NOT NULL UNIQUE,
    title       TEXT NOT NULL,
    agent_ip    TEXT,
    preview     TEXT,
    created_at  INTEGER NOT NULL,
    updated_at  INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversations_openid_updated ON conversations(_openid, updated_at);

-- 3. conversation_messages
CREATE TABLE IF NOT EXISTS conversation_messages (
    id              INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    _openid         TEXT NOT NULL,
    conversation_id TEXT NOT NULL REFERENCES conversations(chat_id),
    message_id      TEXT NOT NULL UNIQUE,
    speaker         TEXT NOT NULL CHECK (speaker IN ('user', 'agent')),
    content         TEXT NOT NULL,
    created_at      INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_conv_created ON conversation_messages(conversation_id, created_at);
CREATE INDEX IF NOT EXISTS idx_conversation_messages_openid ON conversation_messages(_openid);
