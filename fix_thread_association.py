#!/usr/bin/env python3
"""fix_thread_association.py — 修复旧数据中邮件与线程的关联

根因: batch_collect.py 旧版本入库时未设置 thread_id,
导致 get_thread_emails() 返回空列表，--translate 全部跳过。

修复策略:
  1. threads.id == root_message_id (线程根邮件的 message_id)
  2. 邮件.message_id == threads.id          → 根邮件，直接关联
  3. 邮件.in_reply_to 指向线程中某封邮件    → 该线程的回复

用法:
    python fix_thread_association.py              # 预览影响行数
    python fix_thread_association.py --apply      # 正式执行修复
"""
import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "knowledge.db"


def main():
    parser = argparse.ArgumentParser(description="修复邮件 thread_id 关联")
    parser.add_argument("--apply", action="store_true", help="正式执行修复（不加则只预览）")
    parser.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA journal_mode=WAL")

    # 统计
    empty = db.execute(
        "SELECT COUNT(*) FROM emails WHERE thread_id = '' OR thread_id IS NULL"
    ).fetchone()[0]
    threads = db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    print(f"邮件总数: {db.execute('SELECT COUNT(*) FROM emails').fetchone()[0]}")
    print(f"线程总数: {threads}")
    print(f"thread_id 为空的邮件: {empty}")
    print()

    if empty == 0:
        print("无需修复，所有邮件已有 thread_id。")
        return

    # 构建: message_id -> thread_id 映射 (threads.id == root_message_id)
    rows = db.execute("SELECT id, root_message_id FROM threads").fetchall()
    root_to_thread = {}  # root_message_id -> thread_id (threads.id)
    for tid, root_mid in rows:
        if root_mid:
            root_to_thread[root_mid] = tid

    # 构建: message_id -> thread_id 映射 (所有已关联邮件)
    mid_to_tid = {}  # message_id -> thread_id
    for tid, root_mid in rows:
        if root_mid:
            mid_to_tid[root_mid] = tid

    # 用递归 CTE 建立 message_id -> thread_id 的传递闭包
    # 规则: 如果 A.in_reply_to == B.message_id 且 B.thread_id == T，则 A.thread_id == T
    db.execute("DROP TABLE IF EXISTS _mid_to_tid_temp")
    db.execute("""
        CREATE TEMP TABLE _mid_to_tid_temp AS
        SELECT message_id, thread_id FROM emails WHERE thread_id != '' AND thread_id IS NOT NULL
    """)
    db.execute("CREATE INDEX IF NOT EXISTS _idx_irt ON _mid_to_tid_temp(message_id)")

    # 递归: 从已有 thread_id 的邮件出发，反向传播到回复链上游
    db.execute("DROP TABLE IF EXISTS _full_mid_tid")
    db.execute("""
        CREATE TEMP TABLE _full_mid_tid AS
        WITH RECURSIVE chain(message_id, thread_id) AS (
            -- 种子: 已关联的邮件
            SELECT message_id, thread_id FROM _mid_to_tid_temp
            UNION
            -- 扩展: 如果邮件的 in_reply_to 属于某线程，则该邮件也属于该线程
            SELECT e.message_id, c.thread_id
            FROM emails e
            JOIN chain c ON e.in_reply_to = c.message_id
            WHERE e.thread_id = '' OR e.thread_id IS NULL
        )
        SELECT DISTINCT message_id, thread_id FROM chain
    """)
    db.execute("CREATE INDEX IF NOT EXISTS _idx_mid ON _full_mid_tid(message_id)")

    # 直接关联: 邮件.message_id == threads.root_message_id
    direct = db.execute("""
        SELECT COUNT(*) FROM emails e
        JOIN threads t ON e.message_id = t.root_message_id
        WHERE e.thread_id = '' OR e.thread_id IS NULL
    """).fetchone()[0]

    # 通过 reply chain 关联
    indirect = db.execute("""
        SELECT COUNT(DISTINCT e.rowid) FROM emails e
        JOIN _full_mid_tid m ON e.message_id = m.message_id
        WHERE (e.thread_id = '' OR e.thread_id IS NULL)
          AND e.message_id NOT IN (SELECT root_message_id FROM threads WHERE root_message_id IS NOT NULL)
    """).fetchone()[0]

    print(f"可修复: 直接关联(根邮件) {direct} 封, 通过回复链 {indirect} 封")
    total_fixable = direct + indirect
    print(f"无法关联: {empty - total_fixable} 封\n")

    if not args.apply:
        print("预览完成。添加 --apply 执行实际修复。")
        return

    # 执行修复
    print("开始修复...")

    # 1. 直接关联: 邮件.message_id == threads.root_message_id
    updated = db.execute("""
        UPDATE emails
        SET thread_id = (
            SELECT t.id FROM threads t WHERE t.root_message_id = emails.message_id
        )
        WHERE thread_id = '' OR thread_id IS NULL
          AND message_id IN (SELECT root_message_id FROM threads WHERE root_message_id IS NOT NULL)
    """)
    print(f"  直接关联: 更新 {updated.rowcount} 行")

    # 2. 通过 reply chain 关联
    updated2 = db.execute("""
        UPDATE emails
        SET thread_id = (
            SELECT m.thread_id FROM _full_mid_tid m WHERE m.message_id = emails.message_id
        )
        WHERE thread_id = '' OR thread_id IS NULL
          AND message_id IN (SELECT message_id FROM _full_mid_tid)
    """)
    print(f"  回复链关联: 更新 {updated2.rowcount} 行")

    db.commit()

    # 验证
    still_empty = db.execute(
        "SELECT COUNT(*) FROM emails WHERE thread_id = '' OR thread_id IS NULL"
    ).fetchone()[0]
    print(f"\n修复后仍无关联: {still_empty} 封")

    # 更新 threads 表 email_count (因为旧数据也可能不准)
    db.execute("""
        UPDATE threads SET email_count = (
            SELECT COUNT(*) FROM emails WHERE emails.thread_id = threads.id
        )
    """)
    db.commit()
    print("threads.email_count 已重新统计。")
    db.close()


if __name__ == "__main__":
    main()