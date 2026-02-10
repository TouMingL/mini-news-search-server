#!/usr/bin/env python
# 测试 conversations 表插入：确保 user_profile 存在后插入一条对话（使用指定 openid）
# 运行：cd miniprogram-server && conda activate py39 && python scripts/test_insert_conversation.py

import sys
import os
import time

# 项目根目录加入 path
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT_DIR)
os.chdir(ROOT_DIR)

# 使用与 run.py 相同的数据库（当前目录或 instance/database.db）
from app import create_app
from app.models import db, UserProfile, Conversation, ConversationMessage

TEST_OPENID = 'o5RWF18ui4EtpRYbqAffn4HvuGJk'
TEST_CHAT_ID = 'test_chat_' + str(int(time.time() * 1000))


def main():
    app = create_app()
    with app.app_context():
        # 1. 确保 user_profile 存在（Conversation 有 FK 到 user_profile._openid）
        profile = UserProfile.query.filter_by(openid=TEST_OPENID).first()
        if not profile:
            now_ms = int(time.time() * 1000)
            profile = UserProfile(
                openid=TEST_OPENID,
                nick_name='测试用户',
                avatar_url='',
                join_time=now_ms,
                agent_count=0,
                conversation_count=0,
                updated_at=now_ms,
            )
            db.session.add(profile)
            db.session.commit()
            print('已创建 user_profile:', TEST_OPENID)
        else:
            print('user_profile 已存在:', TEST_OPENID)

        # 2. 插入一条 conversation
        now_ms = int(time.time() * 1000)
        conv = Conversation.query.filter_by(chat_id=TEST_CHAT_ID).first()
        if conv:
            print('对话已存在，跳过插入:', TEST_CHAT_ID)
        else:
            conv = Conversation(
                openid=TEST_OPENID,
                chat_id=TEST_CHAT_ID,
                title='测试对话',
                preview='第一条消息预览',
                created_at=now_ms,
                updated_at=now_ms,
            )
            db.session.add(conv)
            db.session.commit()
            print('已插入 conversation:', TEST_CHAT_ID)

        # 3. 可选：插入一条 user 消息
        msg_id = str(now_ms * 1000 + 1)
        existing = ConversationMessage.query.filter_by(message_id=msg_id).first()
        if not existing:
            msg = ConversationMessage(
                openid=TEST_OPENID,
                conversation_id=TEST_CHAT_ID,
                message_id=msg_id,
                speaker='user',
                content='测试消息内容',
                created_at=now_ms,
            )
            db.session.add(msg)
            db.session.commit()
            print('已插入 conversation_message:', msg_id)

        # 4. 查询确认
        n_conv = Conversation.query.filter_by(openid=TEST_OPENID).count()
        n_msg = ConversationMessage.query.filter_by(conversation_id=TEST_CHAT_ID).count()
        print('当前该 openid 对话数:', n_conv, '，测试对话消息数:', n_msg)
        print('请运行 python scripts/print_tables.py 查看表行数。')


if __name__ == '__main__':
    main()
