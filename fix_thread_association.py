"""修复 threads 和 emails 表之间 thread_id 关联断裂的问题。

根因: batch_collect.py 旧版 download_one_thread 有两个 bug:
  1. 仅 thread_emails > 1 时才创建 thread 记录 → 单封邮件的线程无 thread 记录
  2. thread.id 用 build_threads() 重建的 root message_id，
     而 email.thread_id 用搜索时的 root_mid → 两者可能不一致

修复策略:
  A) 查找 "有 thread 记录但无邮件" 的情况:
     - 如果 emails 表中有 thread_id 能匹配到 thread.root_message_id → 修正 thread.id
     - 如果 emails 表有以 thread.id 为 message_id 的邮件且 thread_id 不同 → 更正
     - 确实无邮件数据 → 标记 hidden=1
  B) 查找 "有邮件但无 thread 记录" 的 thread_id:
     - 为其创建 thread 记录

Usage:
    python fix_thread_association.py          # 预览模式
    python fix_thread_association.py --apply  # 执行修复
"""
import sqlite3
import sys
import time


DB_PATH = "data/knowledge.db"


def main():
    apply = "--apply" in sys.argv
    mode = "执行" if apply else "预览"
    print(f"=== 修复 thread-email 关联 ({mode}模式) ===\n")

    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. 统计当前状态
    total_threads = conn.execute(
        "SELECT COUNT(*) FROM threads WHERE hidden=0"
    ).fetchone()[0]
    total_emails = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    orphan_threads = conn.execute(
        "SELECT COUNT(*) FROM threads WHERE hidden=0 "
        "AND id NOT IN (SELECT DISTINCT thread_id FROM emails "
        "WHERE thread_id IS NOT NULL AND thread_id != '')"
    ).fetchone()[0]
    orphan_emails = conn.execute(
        "SELECT COUNT(DISTINCT thread_id) FROM emails "
        "WHERE thread_id NOT IN (SELECT id FROM threads) "
        "AND thread_id IS NOT NULL AND thread_id != ''"
    ).fetchone()[0]

    print(f"可见线程: {total_threads}, 总邮件: {total_emails}")
    print(f"无邮件的线程: {orphan_threads}, 无 thread 记录的 email thread_id: {orphan_emails}")
    print()

    # 2. 处理 "有 thread 记录但无邮件" 的线程
    empty_threads = conn.execute(
        "SELECT id, root_message_id, subject, email_count FROM threads "
        "WHERE hidden=0 AND id NOT IN "
        "(SELECT DISTINCT thread_id FROM emails "
        "WHERE thread_id IS NOT NULL AND thread_id != '')"
    ).fetchall()

    fixed_count = 0
    hidden_count = 0

    for t in empty_threads:
        tid = t["id"]
        root_mid = t["root_message_id"]

        # 策略 A1: 检查 emails 中是否有 message_id = tid 的邮件，其 thread_id 不同
        row = conn.execute(
            "SELECT thread_id FROM emails WHERE message_id = ?", (tid,)
        ).fetchone()
        if row and row["thread_id"] and row["thread_id"] != tid:
            actual_tid = row["thread_id"]
            # 看看那个 thread_id 下有多少邮件
            cnt = conn.execute(
                "SELECT COUNT(*) FROM emails WHERE thread_id = ?",
                (actual_tid,)
            ).fetchone()[0]
            if cnt > 0:
                # 检查是否已有另一个 thread 记录占用了这个 id
                existing = conn.execute(
                    "SELECT id FROM threads WHERE id = ?", (actual_tid,)
                ).fetchone()
                if existing:
                    # 另一个 thread 已占用，直接 hidden 当前这个
                    print(f"  HIDDEN: {tid[:50]} (重复, 另一个 thread {actual_tid[:50]} 已存在)")
                    if apply:
                        conn.execute("UPDATE threads SET hidden=1 WHERE id = ?", (tid,))
                    hidden_count += 1
                else:
                    # 修正 thread.id 为 actual_tid
                    print(f"  FIX ID: {tid[:50]} → {actual_tid[:50]} ({cnt} emails)")
                    if apply:
                        conn.execute(
                            "UPDATE threads SET id = ?, root_message_id = ? WHERE id = ?",
                            (actual_tid, actual_tid, tid)
                        )
                    fixed_count += 1
                continue

        # 策略 A2: 检查 root_message_id 在 emails 中的 thread_id
        if root_mid and root_mid != tid:
            row2 = conn.execute(
                "SELECT thread_id FROM emails WHERE message_id = ?", (root_mid,)
            ).fetchone()
            if row2 and row2["thread_id"]:
                actual_tid2 = row2["thread_id"]
                cnt2 = conn.execute(
                    "SELECT COUNT(*) FROM emails WHERE thread_id = ?",
                    (actual_tid2,)
                ).fetchone()[0]
                if cnt2 > 0:
                    existing2 = conn.execute(
                        "SELECT id FROM threads WHERE id = ?", (actual_tid2,)
                    ).fetchone()
                    if existing2:
                        print(f"  HIDDEN: {tid[:50]} (重复, root_mid 的 thread 已存在)")
                        if apply:
                            conn.execute("UPDATE threads SET hidden=1 WHERE id = ?", (tid,))
                        hidden_count += 1
                    else:
                        print(f"  FIX ID(root): {tid[:50]} → {actual_tid2[:50]} ({cnt2} emails)")
                        if apply:
                            conn.execute(
                                "UPDATE threads SET id = ?, root_message_id = ? WHERE id = ?",
                                (actual_tid2, actual_tid2, tid)
                            )
                        fixed_count += 1
                    continue

        # 策略 A3: 无法修复 → 标记 hidden
        print(f"  HIDDEN (无邮件): {tid[:60]} | {t['subject'][:40]}")
        if apply:
            conn.execute("UPDATE threads SET hidden=1 WHERE id = ?", (tid,))
        hidden_count += 1

    print(f"\n--- 线程修复: fixed={fixed_count}, hidden={hidden_count} ---\n")

    # 3. 处理 "有邮件但无 thread 记录" 的 orphan thread_id → 创建 thread 记录
    orphan_tids = conn.execute(
        "SELECT thread_id, COUNT(*) as cnt, MIN(date) as min_date, "
        "MAX(date) as max_date, MIN(subject) as subj "
        "FROM emails WHERE thread_id NOT IN (SELECT id FROM threads) "
        "AND thread_id IS NOT NULL AND thread_id != '' "
        "GROUP BY thread_id"
    ).fetchall()

    created_count = 0
    for row in orphan_tids:
        tid = row["thread_id"]
        cnt = row["cnt"]
        # 获取第一封邮件的 from 信息来计算 participant_count
        participants = conn.execute(
            "SELECT COUNT(DISTINCT from_email) FROM emails WHERE thread_id = ?",
            (tid,)
        ).fetchone()[0]

        print(f"  CREATE: {tid[:60]} ({cnt} emails, {participants} participants)")
        if apply:
            conn.execute(
                "INSERT OR IGNORE INTO threads "
                "(id, root_message_id, subject, start_date, end_date, "
                "email_count, participant_count, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (tid, tid, row["subj"] or "", row["min_date"] or "",
                 row["max_date"] or "", cnt, participants, time.time())
            )
        created_count += 1

    print(f"\n--- 新建 thread 记录: {created_count} ---\n")

    if apply:
        conn.commit()
        # 验证
        final_orphan = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE hidden=0 "
            "AND id NOT IN (SELECT DISTINCT thread_id FROM emails "
            "WHERE thread_id IS NOT NULL AND thread_id != '')"
        ).fetchone()[0]
        final_visible = conn.execute(
            "SELECT COUNT(*) FROM threads WHERE hidden=0"
        ).fetchone()[0]
        print(f"修复后: 可见线程={final_visible}, 仍无邮件的线程={final_orphan}")
    else:
        print("(预览模式，加 --apply 参数执行修复)")

    conn.close()


if __name__ == "__main__":
    main()