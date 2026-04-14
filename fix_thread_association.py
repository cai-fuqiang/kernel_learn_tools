#!/usr/bin/env python3
"""fix_thread_association.py — 完整重建 threads 表 + 修复 emails.thread_id

数据问题诊断:
  - threads 表是按 Lore 搜索结果创建的，与 emails 表完全脱节
  - emails.thread_id 全为空，无法关联线程
  - 6850 封邮件中 5031 封有 in_reply_to，1819 封是根

重建策略:
  1. 用 emails 的 in_reply_to/message_id 关系构建线程树
  2. 递归向上找到根邮件 → 每个根 = 一个线程
  3. 写入 threads 表，id = 根邮件 message_id
  4. 用递归向下传播 thread_id 到所有回复邮件
"""
import argparse
import sqlite3
import sys
import time
from pathlib import Path

DB_PATH = Path(__file__).parent / "data" / "knowledge.db"


def main():
    parser = argparse.ArgumentParser(description="重建 threads 表 + 修复 emails.thread_id")
    parser.add_argument("--apply", action="store_true", help="执行重建（不加则预览）")
    parser.add_argument("--db", default=str(DB_PATH), help="数据库路径")
    args = parser.parse_args()

    db = sqlite3.connect(args.db)
    db.execute("PRAGMA journal_mode=WAL")

    total = db.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    with_reply = db.execute(
        "SELECT COUNT(*) FROM emails WHERE in_reply_to != '' AND in_reply_to IS NOT NULL"
    ).fetchone()[0]
    print(f"邮件总数: {total}, 有 in_reply_to: {with_reply}, 根邮件: {total - with_reply}")
    print(f"现有 threads 数: {db.execute('SELECT COUNT(*) FROM threads').fetchone()[0]}")
    print()

    # 收集所有 message_id
    all_mids = set(
        r[0] for r in db.execute("SELECT message_id FROM emails").fetchall()
        if r[0]
    )
    print(f"去重 message_id 数: {len(all_mids)}")

    # 找根邮件: 没有 in_reply_to，或 in_reply_to 指向不存在的邮件
    roots = []
    for row in db.execute(
        "SELECT id, message_id, subject, from_name, date FROM emails "
        "WHERE in_reply_to = '' OR in_reply_to IS NULL"
    ).fetchall():
        roots.append(row)
    print(f"候选根邮件（无 in_reply_to）: {len(roots)}")

    # 对每个候选根，用递归 CTE 向下探索所有回复
    # 建立: message_id -> [child_message_ids]
    irt_to_children = {}
    for row in db.execute(
        "SELECT in_reply_to, message_id FROM emails WHERE in_reply_to != '' AND in_reply_to IS NOT NULL"
    ).fetchall():
        parent_mid, child_mid = row
        irt_to_children.setdefault(parent_mid, []).append(child_mid)

    def walk_thread(root_mid, root_id):
        """从根向下遍历，收集所有属于同一线程的 message_id"""
        visited = set()
        queue = [root_mid]
        while queue:
            mid = queue.pop()
            if mid in visited:
                continue
            visited.add(mid)
            for child in irt_to_children.get(mid, []):
                if child not in visited:
                    queue.append(child)
        return visited

    # 构建新 threads
    new_threads = []  # (thread_id, subject, start_date, email_count)
    mid_to_thread = {}  # message_id -> thread_id

    for row in roots:
        eid, root_mid, subject, from_name, date = row
        thread_id = root_mid
        # 向下收集所有回复
        all_mids_in_thread = walk_thread(root_mid, eid)
        email_count = len(all_mids_in_thread)

        # 取线程中最早和最晚的日期
        dates = [
            r[0] for r in db.execute(
                "SELECT date FROM emails WHERE message_id IN (%s)"
                % ",".join("?" * len(all_mids_in_thread)),
                list(all_mids_in_thread)
            ).fetchall() if r[0]
        ]

        # 收集参与者
        participants = set(
            r[0] for r in db.execute(
                "SELECT from_name FROM emails WHERE message_id IN (%s) AND from_name != ''"
                % ",".join("?" * len(all_mids_in_thread)),
                list(all_mids_in_thread)
            ).fetchall() if r[0]
        )

        new_threads.append({
            "id": thread_id,
            "root_message_id": root_mid,
            "subject": subject or "No Subject",
            "start_date": min(dates) if dates else "",
            "end_date": max(dates) if dates else "",
            "email_count": email_count,
            "participant_count": len(participants),
        })

        for mid in all_mids_in_thread:
            mid_to_thread[mid] = thread_id

    print(f"重建后 threads 数: {len(new_threads)}")
    thread_email_counts = [t["email_count"] for t in new_threads]
    multi = sum(1 for c in thread_email_counts if c > 1)
    print(f"多邮件线程: {multi}, 单邮件线程: {len(new_threads) - multi}")
    print()

    if not args.apply:
        print("预览完成。添加 --apply 执行实际重建。")
        return

    print("开始重建...")
    t0 = time.time()

    # 1. 清空旧 threads 表
    db.execute("DELETE FROM threads")
    print("  清空 threads 表")

    # 2. 插入新 threads
    for t in new_threads:
        db.execute(
            """INSERT INTO threads
               (id, root_message_id, subject, start_date, end_date,
                email_count, participant_count, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (t["id"], t["root_message_id"], t["subject"],
             t["start_date"], t["end_date"], t["email_count"],
             t["participant_count"], time.time())
        )
    print(f"  插入 {len(new_threads)} 个线程")

    # 3. 更新 emails.thread_id
    updated = db.execute(
        "UPDATE emails SET thread_id = ? WHERE message_id = ?",
        ("", "")
    )
    db.execute("UPDATE emails SET thread_id = ''")

    updated_count = 0
    for mid, tid in mid_to_thread.items():
        n = db.execute(
            "UPDATE emails SET thread_id = ? WHERE message_id = ?",
            (tid, mid)
        )
        updated_count += 1

    db.commit()
    elapsed = time.time() - t0

    # 验证
    empty_tid = db.execute(
        "SELECT COUNT(*) FROM emails WHERE thread_id = '' OR thread_id IS NULL"
    ).fetchone()[0]
    new_total_threads = db.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    new_multi = db.execute(
        "SELECT COUNT(*) FROM threads WHERE email_count > 1"
    ).fetchone()[0]

    print(f"\n重建完成! 耗时 {elapsed:.1f}s")
    print(f"  threads 数: {new_total_threads} (多邮件: {new_multi})")
    print(f"  thread_id 已关联: {updated_count}/{total} 封")
    print(f"  仍无关联: {empty_tid} 封")
    db.close()


if __name__ == "__main__":
    main()