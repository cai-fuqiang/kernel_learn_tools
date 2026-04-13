#!/usr/bin/env python3
"""知识库快速查询工具

用法:
    python3 query_kb.py                          # 总览统计
    python3 query_kb.py search "fair sleeper"     # 全文搜索
    python3 query_kb.py emails                    # 最新20封邮件
    python3 query_kb.py emails 50                 # 最新50封
    python3 query_kb.py threads                   # 所有线程
    python3 query_kb.py sql "SELECT ..."          # 自定义SQL
"""
import sys, sqlite3, textwrap
from pathlib import Path

DB_PATH = str(Path(__file__).parent / "data" / "knowledge.db")

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def cmd_stats(conn):
    print("=== 知识库统计 ===")
    for table in ("emails", "threads", "knowledge_reports", "collect_jobs"):
        n = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        print(f"  {table:25s} {n:>6} 条")
    print()
    row = conn.execute("SELECT MIN(date) as d1, MAX(date) as d2 FROM emails").fetchone()
    print(f"  邮件日期范围: {row['d1'] or 'N/A'}  ~  {row['d2'] or 'N/A'}")
    top = conn.execute("""
        SELECT from_name, COUNT(*) as cnt FROM emails
        GROUP BY from_name ORDER BY cnt DESC LIMIT 10
    """).fetchall()
    if top:
        print("\n  Top 10 发件人:")
        for r in top:
            print(f"    {r['cnt']:>4}  {r['from_name']}")

def cmd_search(conn, query, limit=20):
    print(f'=== 全文搜索: "{query}" (limit={limit}) ===\n')
    rows = conn.execute("""
        SELECT e.subject, e.from_name, e.date, substr(e.body, 1, 200) as preview
        FROM email_fts f
        JOIN emails e ON e.message_id = f.message_id
        WHERE email_fts MATCH ?
        LIMIT ?
    """, (query, limit)).fetchall()
    if not rows:
        rows = conn.execute("""
            SELECT subject, from_name, date, substr(body, 1, 200) as preview
            FROM emails
            WHERE subject LIKE ? OR body LIKE ?
            ORDER BY date DESC LIMIT ?
        """, (f"%{query}%", f"%{query}%", limit)).fetchall()
    print(f"找到 {len(rows)} 条结果:\n")
    for i, r in enumerate(rows, 1):
        print(f"  {i}. [{r['date']}] {r['subject']}")
        print(f"     From: {r['from_name']}")
        preview = r['preview'].replace('\n', ' ')[:120] if r['preview'] else ''
        print(f"     {preview}...")
        print()

def cmd_emails(conn, limit=20):
    print(f"=== 最新 {limit} 封邮件 ===\n")
    rows = conn.execute(
        "SELECT date, subject, from_name FROM emails ORDER BY date DESC LIMIT ?",
        (limit,)
    ).fetchall()
    for i, r in enumerate(rows, 1):
        print(f"  {i:>3}. [{r['date']}] {r['subject'][:80]}")
        print(f"       From: {r['from_name']}")

def cmd_threads(conn):
    print("=== 线程列表 ===\n")
    rows = conn.execute(
        "SELECT id, subject, email_count, participant_count, start_date FROM threads ORDER BY start_date DESC"
    ).fetchall()
    print(f"共 {len(rows)} 个线程:\n")
    for i, r in enumerate(rows, 1):
        print(f"  {i:>3}. [{r['start_date']}] ({r['email_count']}封/{r['participant_count']}人) {r['subject'][:70]}")

def cmd_sql(conn, sql):
    rows = conn.execute(sql).fetchall()
    for r in rows:
        print(dict(r))

if __name__ == "__main__":
    conn = get_conn()
    args = sys.argv[1:]
    if not args:
        cmd_stats(conn)
    elif args[0] == "search" and len(args) > 1:
        cmd_search(conn, args[1], int(args[2]) if len(args) > 2 else 20)
    elif args[0] == "emails":
        cmd_emails(conn, int(args[1]) if len(args) > 1 else 20)
    elif args[0] == "threads":
        cmd_threads(conn)
    elif args[0] == "sql" and len(args) > 1:
        cmd_sql(conn, args[1])
    else:
        print(__doc__)