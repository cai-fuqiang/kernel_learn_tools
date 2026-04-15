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

_SCHEMA_VERSION = 2


class KnowledgeDB:
    """内核邮件知识库"""

    def __init__(self, db_path: str = None):
        if db_path is None:
            db_path = str(Path(__file__).parent.parent / "data" / "knowledge.db")
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
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
                    translated_html_path TEXT DEFAULT '',
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
                    last_search_time TEXT DEFAULT '',  -- 记录搜索进度，支持断点续传
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL
                );

                -- 采集队列：存储待下载的线程
                CREATE TABLE IF NOT EXISTS collect_queue (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id      INTEGER NOT NULL,
                    root_message_id TEXT NOT NULL,
                    subject     TEXT DEFAULT '',
                    source_url  TEXT DEFAULT '',
                    relevance_score REAL DEFAULT 0,
                    relevance_reason TEXT DEFAULT '',
                    status      TEXT DEFAULT 'pending',  -- pending, downloading, completed, failed
                    priority    INTEGER DEFAULT 0,       -- 优先级，用于排序
                    retry_count INTEGER DEFAULT 0,
                    created_at  REAL NOT NULL,
                    updated_at  REAL NOT NULL,
                    UNIQUE(job_id, root_message_id)
                );

                CREATE INDEX IF NOT EXISTS idx_collect_queue_status ON collect_queue(status);
                CREATE INDEX IF NOT EXISTS idx_collect_queue_job ON collect_queue(job_id);

                CREATE INDEX IF NOT EXISTS idx_emails_thread   ON emails(thread_id);
                CREATE INDEX IF NOT EXISTS idx_emails_date      ON emails(date);
                CREATE INDEX IF NOT EXISTS idx_emails_processed ON emails(processed);
                CREATE INDEX IF NOT EXISTS idx_threads_processed ON threads(processed);

                CREATE TABLE IF NOT EXISTS topics (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    name        TEXT UNIQUE NOT NULL,
                    display_name TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    keywords    TEXT DEFAULT '',
                    created_at  REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS thread_topics (
                    thread_id   TEXT NOT NULL,
                    topic_id    INTEGER NOT NULL,
                    confidence  REAL DEFAULT 1.0,
                    created_at  REAL NOT NULL,
                    PRIMARY KEY (thread_id, topic_id)
                );

                CREATE INDEX IF NOT EXISTS idx_thread_topics_topic ON thread_topics(topic_id);
            """)

            # FTS5 虚拟表（忽略已存在的错误）
            try:
                self.conn.execute("""
                    CREATE VIRTUAL TABLE IF NOT EXISTS email_fts
                    USING fts5(message_id, subject, body, summary)
                """)
            except sqlite3.OperationalError:
                pass  # 已存在

            # 兼容旧数据库：补加 translated_html_path 列
            try:
                self.conn.execute(
                    "ALTER TABLE threads ADD COLUMN translated_html_path TEXT DEFAULT ''"
                )
            except sqlite3.OperationalError:
                pass  # 列已存在

            # 兼容旧数据库：补加 hidden 列
            try:
                self.conn.execute(
                    "ALTER TABLE threads ADD COLUMN hidden INTEGER DEFAULT 0"
                )
            except sqlite3.OperationalError:
                pass  # 列已存在

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

    def get_untranslated_threads(self, limit: int = 100) -> List[Dict]:
        """获取未翻译的线程（translated_html_path 为空，排除 hidden）。"""
        rows = self.conn.execute(
            "SELECT * FROM threads WHERE hidden = 0 "
            "AND (translated_html_path IS NULL OR translated_html_path = '') "
            "ORDER BY start_date LIMIT ?",
            (limit,)
        ).fetchall()
        return [dict(r) for r in rows]

    def update_thread_translated_path(self, thread_id: str, html_path: str):
        """更新线程的翻译 HTML 文件路径。"""
        with self.conn:
            self.conn.execute(
                "UPDATE threads SET translated_html_path = ? WHERE id = ?",
                (html_path, thread_id),
            )

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
    # 采集队列操作
    # ------------------------------------------------------------------

    def add_to_queue(self, job_id: int, root_email: Dict, priority: int = 0) -> bool:
        """添加线程到下载队列"""
        root_mid = root_email.get("message_id", "")
        if not root_mid:
            return False
        
        with self.conn:
            try:
                self.conn.execute("""
                    INSERT OR IGNORE INTO collect_queue
                    (job_id, root_message_id, subject, source_url, 
                     relevance_score, relevance_reason, priority, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (
                    job_id, root_mid, root_email.get("subject", ""),
                    root_email.get("source_url", ""),
                    root_email.get("relevance_score", 0),
                    root_email.get("relevance_reason", ""),
                    priority, time.time(), time.time()
                ))
                return True
            except sqlite3.IntegrityError:
                return False

    def get_queue_items(self, job_id: int, status: str = "pending", limit: int = 100) -> List[Dict]:
        """获取队列中的项目"""
        rows = self.conn.execute("""
            SELECT * FROM collect_queue 
            WHERE job_id = ? AND status = ?
            ORDER BY priority DESC, created_at ASC
            LIMIT ?
        """, (job_id, status, limit)).fetchall()
        return [dict(r) for r in rows]

    def update_queue_status(self, queue_id: int, status: str, retry_count: int = None):
        """更新队列项目状态"""
        if retry_count is not None:
            with self.conn:
                self.conn.execute("""
                    UPDATE collect_queue 
                    SET status = ?, retry_count = ?, updated_at = ?
                    WHERE id = ?
                """, (status, retry_count, time.time(), queue_id))
        else:
            with self.conn:
                self.conn.execute("""
                    UPDATE collect_queue 
                    SET status = ?, updated_at = ?
                    WHERE id = ?
                """, (status, time.time(), queue_id))

    def get_queue_stats(self, job_id: int) -> Dict:
        """获取队列统计信息"""
        stats = {}
        for status in ["pending", "downloading", "completed", "failed"]:
            count = self.conn.execute("""
                SELECT COUNT(*) FROM collect_queue 
                WHERE job_id = ? AND status = ?
            """, (job_id, status)).fetchone()[0]
            stats[status] = count
        total = self.conn.execute("""
            SELECT COUNT(*) FROM collect_queue WHERE job_id = ?
        """, (job_id,)).fetchone()[0]
        stats["total"] = total
        return stats

    def get_failed_queue_items(self, job_id: int, max_retries: int = 3) -> List[Dict]:
        """获取失败但可重试的队列项目"""
        rows = self.conn.execute("""
            SELECT * FROM collect_queue 
            WHERE job_id = ? AND status = 'failed' AND retry_count < ?
            ORDER BY retry_count ASC, created_at ASC
        """, (job_id, max_retries)).fetchall()
        return [dict(r) for r in rows]

    def reset_queue_status(self, job_id: int, status: str = "pending"):
        """重置队列状态（用于断点续传）"""
        with self.conn:
            self.conn.execute("""
                UPDATE collect_queue 
                SET status = ?, updated_at = ?
                WHERE job_id = ? AND status IN ('downloading', 'failed')
            """, (status, time.time(), job_id))

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
    # 话题操作
    # ------------------------------------------------------------------

    def upsert_topic(self, name: str, display_name: str = "",
                     description: str = "", keywords: str = "") -> int:
        """创建或更新话题。返回 topic_id。"""
        with self.conn:
            row = self.conn.execute(
                "SELECT id FROM topics WHERE name = ?", (name,)
            ).fetchone()
            if row:
                self.conn.execute("""
                    UPDATE topics SET display_name = ?, description = ?, keywords = ?
                    WHERE name = ?
                """, (display_name or name, description, keywords, name))
                return row[0]
            else:
                cur = self.conn.execute("""
                    INSERT INTO topics (name, display_name, description, keywords, created_at)
                    VALUES (?, ?, ?, ?, ?)
                """, (name, display_name or name, description, keywords, time.time()))
                return cur.lastrowid

    def get_topics(self) -> List[Dict]:
        """获取所有话题，包含线程计数。"""
        rows = self.conn.execute("""
            SELECT t.*, COALESCE(c.cnt, 0) as thread_count
            FROM topics t
            LEFT JOIN (
                SELECT topic_id, COUNT(*) as cnt FROM thread_topics GROUP BY topic_id
            ) c ON c.topic_id = t.id
            ORDER BY c.cnt DESC, t.name
        """).fetchall()
        return [dict(r) for r in rows]

    def get_topic_by_id(self, topic_id: int) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM topics WHERE id = ?", (topic_id,)
        ).fetchone()
        return dict(row) if row else None

    def get_topic_by_name(self, name: str) -> Optional[Dict]:
        row = self.conn.execute(
            "SELECT * FROM topics WHERE name = ?", (name,)
        ).fetchone()
        return dict(row) if row else None

    def link_thread_topic(self, thread_id: str, topic_id: int,
                          confidence: float = 1.0):
        """关联线程到话题。"""
        with self.conn:
            self.conn.execute("""
                INSERT OR REPLACE INTO thread_topics
                (thread_id, topic_id, confidence, created_at)
                VALUES (?, ?, ?, ?)
            """, (thread_id, topic_id, confidence, time.time()))

    def get_topic_threads(self, topic_id: int, include_hidden: bool = False,
                          limit: int = 200) -> List[Dict]:
        """获取话题下的所有线程。"""
        hidden_clause = "" if include_hidden else "AND t.hidden = 0"
        rows = self.conn.execute(f"""
            SELECT t.* FROM threads t
            JOIN thread_topics tt ON tt.thread_id = t.id
            WHERE tt.topic_id = ? {hidden_clause}
            ORDER BY t.start_date DESC LIMIT ?
        """, (topic_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def get_thread_topic_ids(self, thread_id: str) -> List[int]:
        """获取线程关联的所有话题 ID。"""
        rows = self.conn.execute(
            "SELECT topic_id FROM thread_topics WHERE thread_id = ?",
            (thread_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def set_thread_hidden(self, thread_id: str, hidden: int = 1):
        """软删除/恢复线程。"""
        with self.conn:
            self.conn.execute(
                "UPDATE threads SET hidden = ? WHERE id = ?",
                (hidden, thread_id)
            )

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
        topic_count = self.conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
        hidden_threads = self.conn.execute(
            "SELECT COUNT(*) FROM threads WHERE hidden = 1"
        ).fetchone()[0]
        return {
            "emails": email_count,
            "threads": thread_count,
            "reports": report_count,
            "processed_threads": processed_threads,
            "topics": topic_count,
            "hidden_threads": hidden_threads,
        }