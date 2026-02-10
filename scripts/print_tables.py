#!/usr/bin/env python
# 打印 SQLite 数据库中的表结构及数据，用于检查 database-plan 实施结果
# 运行：在 miniprogram-server 根目录执行 python scripts/print_tables.py

import os
import sqlite3
import sys

# 解析数据库路径（与 Flask 默认一致：instance/database.db 或 database.db）
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR = os.path.dirname(SCRIPT_DIR)
DB_PATHS = [
    os.path.join(ROOT_DIR, 'instance', 'database.db'),
    os.path.join(ROOT_DIR, 'database.db'),
]


def find_db():
    for path in DB_PATHS:
        if os.path.isfile(path):
            return path
    return None


def main():
    db_path = find_db()
    if not db_path:
        print('未找到数据库文件，尝试路径:', DB_PATHS)
        sys.exit(1)

    print('数据库路径:', db_path)
    print('-' * 60)

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    # 所有表名（按 schema 顺序）
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    tables = [row[0] for row in cur.fetchall()]

    for table in tables:
        print('\n[表]', table)

        # 列信息
        cur.execute('PRAGMA table_info(%s)' % table)
        cols = cur.fetchall()
        col_names = [c[1] for c in cols]
        print('  列:', ', '.join(col_names))

        # 行数
        cur.execute('SELECT COUNT(*) FROM %s' % table)
        n = cur.fetchone()[0]
        print('  行数:', n)

        # 前 5 行（如有）
        if n > 0:
            cur.execute('SELECT * FROM %s LIMIT 5' % table)
            rows = cur.fetchall()
            for i, row in enumerate(rows, 1):
                d = dict(zip(col_names, row))
                # 长内容截断
                out = {k: (v[:40] + '...' if isinstance(v, str) and len(v) > 40 else v) for k, v in d.items()}
                print('  #%d' % i, out)
            if n > 5:
                print('  ... 共 %d 行，仅显示前 5 行' % n)

    conn.close()
    print('\n' + '-' * 60)
    print('完成')


if __name__ == '__main__':
    main()
