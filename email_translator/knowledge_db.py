"""knowledge_db.py — 内核邮件知识库 SQLite 存储层

提供邮件、线程、摘要、综合报告的持久化存储和全文搜索。
复用 translation_cache.py 的 SQLite 使用模式。

表结构:
  emails            — 邮件元数据 + 正文
  threads           — 邮件线程 + AI 摘要
  knowledge_reports — 跨线程综合分析报告
  collect_jobs      — 采集任务记录（断点续传）
  email_fts         — FTS5 全文搜索虚拟表
"""

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_SCHEMA_VERSION = 1


class KnowledgeDB:
    """内核邮件知识库"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(__file__).parent.parent / "data" / "knowledge.db")
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self._init_db()

    # ------------------------------------------------------------------
    # 初始化
    # ------------------------------------------------------------------

    def _init_db(self):
        with self.conn:
            self.conn.executescript("""
                CREATE TABLE IF NOT EXISTS emails (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id      TEXT UNIQUE NOT NULL,
                    subject         TEXT NOT NULL DEFAULT '',
                    from_name       TEXT DEFAULT '',
                    from_email      TEXT DEFAULT '',
                    date            TEXT DEFAULT '',
                    body            TEXT DEFAULT '',
                    list_name       TEXT DEFAULT '',
                    thread_id       TEXT DEFAULT '',
                    in_reply_to     TEXT DEFAULT '',
                    refs            TEXT DEFAULT '',
                    priority        TEXT DEFAULT '',
                    relevance_score REAL DEFAULT 0,
                    relevance_reason TEXT DEFAULT '',
                    raw_json_path   TEXT DEFAULT '',
                    processed       INTEGER DEFAULT 0,
                    created_at      REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS threads (
                    id                TEXT PRIMARY KEY,
                    root_message_id   TEXT DEFAULT '',
                    subject           TEXT DEFAULT '',
                    start_date        TEXT DEFAULT '',
                    end_date          TEXT DEFAULT '',
                    email_count       INTEGER DEFAULT 0,
                    participant_count INTEGER DEFAULT 0,
                    summary_zh        TEXT DEFAULT '',
                    key_points        TEXT DEFAULT '',
                    consensus         TEXT DEFAULT '',
                    design_decisions  TEXT DEFAULT '',
                    related_files     TEXT DEFAULT '',
                    related_functions TEXT DEFAULT '',
                    tags              TEXT DEFAULT '',
                    processed         INTEGER DEFAULT 0,
                    created_at        REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS knowledge_reports (
                    id                INTEGER PRIMARY KEY AUTOINCREMENT,
                    topic             TEXT NOT NULL,
                    report_type       TEXT NOT NULL DEFAULT 'cross_analysis',
                    content           TEXT NOT NULL DEFAULT '',
                    source_thread_ids TEXT DEFAULT '',
                    created_at        REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS collect_jobs (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    keywords    TEXT NOT NULL,
                    date_from   TEXT DEFAULT '',
                    date_to     TEXT DEFAULT '',
                    list_name   TEXT DEFAULT 'all',
                    status      TEXT DEFAULT 'pending',
                    total_found INTEGER DEFAULT 0,
                    total_relevant INTEGER DEFAULT 0,
                    progress    TEXT DEFAULT '',
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_emails_thread   ON emails(thread_id);
                CREATE INDEX IF NOT EXISTS idx_emails_date      ON emails(date);
                CREATE INDEX IF NOT EXISTS idx_emails_processed ON emails(processed);
                CREATE INDEX IF NOT EXISTS idx_threads_processed ON threads(processed);
            """)

            # FTS5 虚拟表（忽略已存在的错误）
            try:
                self.conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS email_fts
                    USING fts5(message_id, subject, body, summary)
                """)
            except sqlite3.OperationalError:
                pass  # 已存在

    # ------------------------------------------------------------------
    # 邮件操作
    # ------------------------------------------------------------------

    def insert_email(self, email: Dict) -> bool:
        """插入一封邮件，按 message_id 去重。返回 True=新插入，False=已存在。"""
        mid = email.get("message_id", "").strip()
        if not mid:
            return False

        existing = self.conn.execute(
            "SELECT id FROM emails WHERE message_id = ?", (mid,)
        ).fetchone()
        if existing:
            return False

        refs = email.get("references", [])
        if isinstance(refs, list):
            refs = " ".join(refs)

        self.conn.execute("""
            INSERT INTO emails
            (message_id, subject, from_name, from_email, date, body,
             list_name, thread_id, in_reply_to, refs, priority,
             relevance_score, relevance_reason, raw_json_path, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            mid,
            email.get("subject", ""),
            email.get("from", "").split("<")[0].strip(),
            email.get("from", ""),
            email.get("date", ""),
            email.get("body", ""),
            email.get("list_name", ""),
            email.get("thread_id", ""),
            email.get("in_reply_to", ""),
            refs,
            email.get("priority", ""),
            email.get("relevance_score", 0),
            email.get("relevance_reason", ""),
            email.get("raw_json_path", ""),
            time.time(),
        ))
        self.conn.commit()

        # 同步到 FTS
        try:
            self.conn.execute(
                "INSERT INTO email_fts(message_id, subject, body, summary) VALUES (?, ?, ?, ?)",
                (mid, email.get("subject", ""), email.get("body", "")[:2000], "")
            )
            self.conn.commit()
        except Exception:
            pass

        return True

    def insert_emails_bulk(self, emails: List[Dict]) -> Tuple[int, int]:
        """批量插入邮件。返回 (新增数, 跳过数)。"""
        new, skip = 0, 0
        for em in emails:
            if self.insert_email(em):
                new += 1
            else:
                skip += 1
        return new, skip

    def email_exists(self, message_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM emails WHERE message_id = ?", (message_id,)
        ).fetchone()
        return row is not None

    def get_unprocessed_emails(self, limit: int = 100) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM emails WHERE processed = 0 ORDER BY date LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 线程操作
    # ------------------------------------------------------------------

    def upsert_thread(self, thread: Dict):
        """插入或更新线程信息。"""
        tid = thread.get("id", "")
        if not tid:
            return

        def _json_field(val):
            if isinstance(val, (list, dict)):
                return json.dumps(val, ensure_ascii=False)
            return str(val) if val else ""

        with self.conn:
            self.conn.execute("""
                INSERT INTO threads
                (id, root_message_id, subject, start_date, end_date,
                 email_count, participant_count, tags, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    subject = excluded.subject,
                    email_count = excluded.email_count,
                    participant_count = excluded.participant_count
            """, (
                tid,
                thread.get("root_message_id", ""),
                thread.get("subject", ""),
                thread.get("start_date", ""),
                thread.get("end_date", ""),
                thread.get("email_count", 0),
                thread.get("participant_count", 0),
                _json_field(thread.get("tags", [])),
                time.time(),
            ))

    def update_thread_summary(self, thread_id: str, summary: Dict):
        """将 AI 摘要写回线程记录。"""
        def _j(v):
            return json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else str(v or "")

        with self.conn:
            self.conn.execute("""
                UPDATE threads SET
                    summary_zh = ?,
                    key_points = ?,
                    consensus = ?,
                    design_decisions = ?,
                    related_files = ?,
                    related_functions = ?,
                    tags = ?,
                    processed = 1
                WHERE id = ?
            """, (
                summary.get("summary", ""),
                _j(summary.get("key_points", [])),
                summary.get("consensus", ""),
                _j(summary.get("design_decisions", [])),
                _j(summary.get("related_files", [])),
                _j(summary.get("related_functions", [])),
                _j(summary.get("tags", [])),
                thread_id,
            ))

    def get_unprocessed_threads(self, limit: int = 50) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM threads WHERE processed = 0 ORDER BY start_date LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def get_thread_emails(self, thread_id: str) -> List[Dict]:
        rows = self.conn.execute(
            "SELECT * FROM emails WHERE thread_id = ? ORDER BY date",
            (thread_id,)
        ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 综合报告
    # ------------------------------------------------------------------

    def insert_report(self, topic: str, content: str,
                      source_thread_ids: List[str],
                      report_type: str = "cross_analysis"):
        with self.conn:
            self.conn.execute("""
                INSERT INTO knowledge_reports
                (topic, report_type, content, source_thread_ids, created_at)
                VALUES (?, ?, ?, ?, ?)
            """, (
                topic, report_type, content,
                json.dumps(source_thread_ids, ensure_ascii=False),
                time.time(),
            ))

    # ------------------------------------------------------------------
    # 采集任务
    # ------------------------------------------------------------------

    def create_job(self, keywords: str, date_from: str, date_to: str,
                   list_name: str = "all") -> int:
        now = time.time()
        with self.conn:
            cur = self.conn.execute("""
                INSERT INTO collect_jobs
                (keywords, date_from, date_to, list_name, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?)
            """, (keywords, date_from, date_to, list_name, now, now))
            return cur.lastrowid

    def update_job(self, job_id: int, **kwargs):
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [time.time(), job_id]
        with self.conn:
            self.conn.execute(
                f"UPDATE collect_jobs SET {sets}, updated_at = ? WHERE id = ?", vals
            )

    # ------------------------------------------------------------------
    # 全文搜索
    # ------------------------------------------------------------------

    def search_fts(self, query: str, limit: int = 50) -> List[Dict]:
        """FTS5 全文搜索邮件。"""
        rows = self.conn.execute("""
            SELECT e.* FROM email_fts f
            JOIN emails e ON e.message_id = f.message_id
            WHERE email_fts MATCH ?
            LIMIT ?
        """, (query, limit)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # 统计
    # ------------------------------------------------------------------

    def stats(self) -> Dict:
        email_count = self.conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
        thread_count = self.conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
        report_count = self.conn.execute("SELECT COUNT(*) FROM knowledge_reports").fetchone()[0]
        processed_threads = self.conn.execute(
            "SELECT COUNT(*) FROM threads WHERE processed = 1"
        ).fetchone()[0]
        return {
            "emails": email_count,
            "threads": thread_count,
            "reports": report_count,
            "processed_threads": processed_threads,
        }