#!/usr/bin/env python3
"""kb_web.py — 知识库网页浏览器

从 knowledge.db 读取邮件/线程数据，启动本地 HTTP 服务展示。

功能:
  - 总览统计（邮件数、线程数、发件人排行、相关性分布）
  - 邮件列表（分页、搜索、排序）
  - 线程列表
  - 邮件详情（正文、引用关系）
  - 全文搜索

用法:
    python kb_web.py                    # 默认 8765 端口
    python kb_web.py --port 9000        # 指定端口
    python kb_web.py --host 0.0.0.0     # 允许外部访问
"""

import argparse
import json
import logging
import sqlite3
import sys
import threading
import time
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

sys.path.insert(0, str(Path(__file__).parent))

DB_PATH = str(Path(__file__).parent / "data" / "knowledge.db")

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)-5s %(message)s",
                    datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


# ======================================================================
# 数据库查询
# ======================================================================

def get_conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def api_stats(conn):
    ec = conn.execute("SELECT COUNT(*) FROM emails").fetchone()[0]
    tc = conn.execute("SELECT COUNT(*) FROM threads WHERE hidden = 0").fetchone()[0]
    pc = conn.execute("SELECT COUNT(*) FROM threads WHERE processed = 1 AND hidden = 0").fetchone()[0]
    try:
        tpc = conn.execute("SELECT COUNT(*) FROM topics").fetchone()[0]
    except Exception:
        tpc = 0
    dr = conn.execute(
        "SELECT MIN(date) as d1, MAX(date) as d2 FROM emails"
    ).fetchone()
    top = conn.execute("""
        SELECT from_name, COUNT(*) as cnt FROM emails
        GROUP BY from_name ORDER BY cnt DESC LIMIT 15
    """).fetchall()
    sd = conn.execute("""
        SELECT
          SUM(CASE WHEN relevance_score >= 0.8 THEN 1 ELSE 0 END) as hi,
          SUM(CASE WHEN relevance_score >= 0.4 AND relevance_score < 0.8 THEN 1 ELSE 0 END) as mid,
          SUM(CASE WHEN relevance_score < 0.4 THEN 1 ELSE 0 END) as lo
        FROM emails
    """).fetchone()
    return {
        "email_count": ec, "thread_count": tc,
        "processed_count": pc, "topic_count": tpc,
        "date_min": dr["d1"] or "", "date_max": dr["d2"] or "",
        "top_senders": [{"name": r["from_name"], "count": r["cnt"]} for r in top],
        "score_high": sd["hi"] or 0, "score_mid": sd["mid"] or 0, "score_low": sd["lo"] or 0,
    }


def api_emails(conn, page=1, per_page=50, search="", sort="date", order="desc"):
    offset = (page - 1) * per_page
    where, params = "1=1", []
    if search:
        where = "(subject LIKE ? OR from_name LIKE ? OR body LIKE ?)"
        sq = f"%{search}%"
        params = [sq, sq, sq]
    allowed = {"date": "date", "subject": "subject", "from": "from_name",
               "score": "relevance_score"}
    sc = allowed.get(sort, "date")
    od = "ASC" if order == "asc" else "DESC"
    total = conn.execute(f"SELECT COUNT(*) FROM emails WHERE {where}", params).fetchone()[0]
    rows = conn.execute(f"""
        SELECT id, message_id, subject, from_name, date,
               relevance_score, relevance_reason,
               substr(body, 1, 300) as preview
        FROM emails WHERE {where}
        ORDER BY {sc} {od} LIMIT ? OFFSET ?
    """, params + [per_page, offset]).fetchall()
    return {
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "emails": [dict(r) for r in rows],
    }


def api_email_detail(conn, email_id):
    row = conn.execute("SELECT * FROM emails WHERE id = ?", (email_id,)).fetchone()
    if not row:
        return None
    return dict(row)


def api_threads(conn, page=1, per_page=30):
    offset = (page - 1) * per_page
    total = conn.execute("SELECT COUNT(*) FROM threads WHERE hidden = 0").fetchone()[0]
    rows = conn.execute("""
        SELECT * FROM threads WHERE hidden = 0 ORDER BY start_date DESC LIMIT ? OFFSET ?
    """, (per_page, offset)).fetchall()
    return {
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "threads": [dict(r) for r in rows],
    }


def api_topics(conn):
    """获取所有话题及线程计数。"""
    rows = conn.execute("""
        SELECT t.*, COALESCE(c.cnt, 0) as thread_count
        FROM topics t
        LEFT JOIN (
            SELECT topic_id, COUNT(*) as cnt FROM thread_topics GROUP BY topic_id
        ) c ON c.topic_id = t.id
        ORDER BY c.cnt DESC, t.name
    """).fetchall()
    return {"topics": [dict(r) for r in rows]}


def api_topic_threads(conn, topic_id, page=1, per_page=30):
    """获取话题下的线程列表。"""
    offset = (page - 1) * per_page
    total = conn.execute("""
        SELECT COUNT(*) FROM threads t
        JOIN thread_topics tt ON tt.thread_id = t.id
        WHERE tt.topic_id = ? AND t.hidden = 0
    """, (topic_id,)).fetchone()[0]
    rows = conn.execute("""
        SELECT t.* FROM threads t
        JOIN thread_topics tt ON tt.thread_id = t.id
        WHERE tt.topic_id = ? AND t.hidden = 0
        ORDER BY t.start_date DESC LIMIT ? OFFSET ?
    """, (topic_id, per_page, offset)).fetchall()
    return {
        "total": total, "page": page, "per_page": per_page,
        "pages": max(1, (total + per_page - 1) // per_page),
        "threads": [dict(r) for r in rows],
    }


def api_search(conn, query, limit=100):
    """FTS + LIKE 双重搜索"""
    results = []
    try:
        rows = conn.execute("""
            SELECT e.id, e.message_id, e.subject, e.from_name, e.date,
                   e.relevance_score, substr(e.body, 1, 300) as preview
            FROM email_fts f
            JOIN emails e ON e.message_id = f.message_id
            WHERE email_fts MATCH ?
            LIMIT ?
        """, (query, limit)).fetchall()
        results = [dict(r) for r in rows]
    except Exception:
        pass
    if not results:
        sq = f"%{query}%"
        rows = conn.execute("""
            SELECT id, message_id, subject, from_name, date,
                   relevance_score, substr(body, 1, 300) as preview
            FROM emails
            WHERE subject LIKE ? OR body LIKE ?
            ORDER BY date DESC LIMIT ?
        """, (sq, sq, limit)).fetchall()
        results = [dict(r) for r in rows]
    return {"total": len(results), "results": results}


# ======================================================================
# 后台翻译管理器
# ======================================================================

class TranslateManager:
    """管理后台翻译任务：支持从网页触发单个/批量线程翻译。"""

    def __init__(self):
        self._lock = threading.Lock()
        self._task = None  # 当前翻译任务状态

    def status(self):
        with self._lock:
            if not self._task:
                return {"running": False}
            return dict(self._task)

    def start(self, thread_ids, backend, api_key="", api_provider="deepseek",
              model="", proxy=""):
        """启动后台翻译任务。"""
        with self._lock:
            if self._task and self._task.get("running"):
                return {"error": "已有翻译任务正在运行"}
            self._task = {
                "running": True,
                "total": len(thread_ids),
                "done": 0,
                "success": 0,
                "failed": 0,
                "current": "",
                "errors": [],
            }

        t = threading.Thread(
            target=self._run,
            args=(thread_ids, backend, api_key, api_provider, model, proxy),
            daemon=True,
        )
        t.start()
        return {"ok": True, "total": len(thread_ids)}

    def _run(self, thread_ids, backend, api_key, api_provider, model, proxy):
        from email_translator.config import OUTPUT_DIR
        from email_translator.knowledge_db import KnowledgeDB
        from email_translator.translator import create_translator
        from email_translator.translation_cache import TranslationCache
        from translate_context import (
            CachedTranslator, should_translate, translate_body_aligned,
            generate_html, _split_body_and_diff, _translate_diff_comments,
        )

        db = KnowledgeDB()
        output_dir = OUTPUT_DIR
        output_dir.mkdir(parents=True, exist_ok=True)

        # 创建翻译器
        try:
            if backend == "api":
                translator = create_translator(
                    "api", api_key=api_key, provider=api_provider,
                    model=model or None, proxy=proxy or None,
                )
            else:
                translator = create_translator(backend, proxy=proxy or None)
        except Exception as e:
            with self._lock:
                self._task["running"] = False
                self._task["errors"].append(f"创建翻译器失败: {e}")
            return

        cache = TranslationCache()
        translator = CachedTranslator(translator, cache, backend)

        def _safe_filename(s):
            import re
            return re.sub(r'[<>:"/\\|?*\s]', '_', s)[:120]

        def _db_email_to_translate_fmt(em):
            return {
                "from": em.get("from_name", "") or em.get("from_email", ""),
                "date": em.get("date", ""),
                "subject": em.get("subject", ""),
                "body": em.get("body", ""),
                "message_id": em.get("message_id", ""),
                "in_reply_to": em.get("in_reply_to", ""),
            }

        for tid in thread_ids:
            row = db.conn.execute(
                "SELECT * FROM threads WHERE id = ?", (tid,)
            ).fetchone()
            if not row:
                with self._lock:
                    self._task["done"] += 1
                    self._task["failed"] += 1
                continue

            thread = dict(row)
            subject = thread.get("subject", "(无主题)")

            with self._lock:
                self._task["current"] = subject[:60]

            try:
                emails_db = db.get_thread_emails(tid)
                if not emails_db:
                    with self._lock:
                        self._task["done"] += 1
                        self._task["failed"] += 1
                    continue

                emails = [_db_email_to_translate_fmt(em) for em in emails_db]
                translated = {}

                # 翻译正文
                for i, em in enumerate(emails):
                    body = em.get("body", "")
                    if should_translate(body):
                        translated[f"email_{i}"] = translate_body_aligned(
                            translator, body)

                # 翻译 diff 注释
                for i, em in enumerate(emails):
                    _, em_diff = _split_body_and_diff(em.get("body", ""))
                    if em_diff:
                        translated[f"diff_{i}"] = _translate_diff_comments(
                            translator, em_diff)

                # 生成 HTML
                commit = {"subject": subject,
                          "date": thread.get("start_date", "")}
                email_header = (f"原始 {len(emails)} 封 / "
                                f"过滤 0 封 / 保留 {len(emails)} 封")

                html = generate_html(
                    commit=commit, diff="",
                    email_header=email_header, emails=emails,
                    checklist="", translated_bodies=translated,
                    source_hash=f"kb-{tid}",
                )

                safe_name = _safe_filename(tid)
                out_file = output_dir / f"thread_{safe_name}_translated.html"
                out_file.write_text(html, encoding="utf-8")
                db.update_thread_translated_path(tid, str(out_file))

                with self._lock:
                    self._task["done"] += 1
                    self._task["success"] += 1

            except Exception as e:
                logger.error("翻译线程 %s 失败: %s", tid, e)
                with self._lock:
                    self._task["done"] += 1
                    self._task["failed"] += 1
                    if len(self._task["errors"]) < 10:
                        self._task["errors"].append(
                            f"{subject[:40]}: {str(e)[:80]}")

        with self._lock:
            self._task["running"] = False
            self._task["current"] = ""


_translate_mgr = TranslateManager()


# ======================================================================
# HTTP Handler
# ======================================================================

class KBHandler(BaseHTTPRequestHandler):
    conn = None

    def log_message(self, fmt, *args):
        pass  # 静默日志

    def _json_response(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _html_response(self, html_str):
        body = html_str.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        def qp(key, default=""):
            return qs.get(key, [default])[0]

        # API 路由
        if path == "/api/stats":
            self._json_response(api_stats(self.conn))
        elif path == "/api/emails":
            self._json_response(api_emails(
                self.conn,
                page=int(qp("page", "1")),
                per_page=int(qp("per_page", "50")),
                search=qp("search"),
                sort=qp("sort", "date"),
                order=qp("order", "desc"),
            ))
        elif path.startswith("/api/email/"):
            eid = path.split("/")[-1]
            data = api_email_detail(self.conn, int(eid))
            if data:
                self._json_response(data)
            else:
                self._json_response({"error": "not found"}, 404)
        elif path == "/api/threads":
            self._json_response(api_threads(
                self.conn,
                page=int(qp("page", "1")),
                per_page=int(qp("per_page", "30")),
            ))
        elif path == "/api/topics":
            self._json_response(api_topics(self.conn))
        elif path.startswith("/api/topics/") and path.endswith("/threads"):
            # /api/topics/123/threads
            topic_id = path.split("/")[3]
            self._json_response(api_topic_threads(
                self.conn, int(topic_id),
                page=int(qp("page", "1")),
                per_page=int(qp("per_page", "30")),
            ))
        elif path == "/api/search":
            self._json_response(api_search(
                self.conn, qp("q", ""), int(qp("limit", "100"))
            ))
        elif path == "/api/translate/status":
            self._json_response(_translate_mgr.status())
        elif path.startswith("/translated/"):
            # 静态文件服务：返回翻译 HTML 文件
            thread_id = unquote(path[len("/translated/"):])
            self._serve_translated_html(thread_id)
        else:
            # 所有其他路径返回 SPA HTML
            self._html_response(PAGE_HTML)

    def _serve_translated_html(self, thread_id):
        """根据 thread_id 查找并返回翻译 HTML 文件。"""
        row = self.conn.execute(
            "SELECT translated_html_path FROM threads WHERE id = ?",
            (thread_id,)
        ).fetchone()
        if not row or not row["translated_html_path"]:
            self.send_error(404, "翻译文件不存在")
            return
        html_path = Path(row["translated_html_path"])
        if not html_path.exists():
            self.send_error(404, f"翻译文件未找到: {html_path.name}")
            return
        self._html_response(html_path.read_text(encoding="utf-8"))

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        content_len = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_len) if content_len else b""

        if path == "/api/translate":
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                self._json_response({"error": "无效 JSON"}, 400)
                return

            thread_ids = data.get("thread_ids", [])
            backend = data.get("backend", "google")
            api_key = data.get("api_key", "")
            api_provider = data.get("api_provider", "deepseek")
            model = data.get("model", "")
            proxy = data.get("proxy", "")

            if not thread_ids:
                self._json_response({"error": "请指定要翻译的线程"}, 400)
                return
            if backend == "api" and not api_key:
                self._json_response({"error": "API 模式需要提供 api_key"}, 400)
                return

            result = _translate_mgr.start(
                thread_ids, backend, api_key=api_key,
                api_provider=api_provider, model=model, proxy=proxy,
            )
            status_code = 200 if "ok" in result else 409
            self._json_response(result, status_code)
        elif path == "/api/topics":
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                self._json_response({"error": "无效 JSON"}, 400)
                return
            name = data.get("name", "").strip()
            if not name:
                self._json_response({"error": "话题名不能为空"}, 400)
                return
            from email_translator.knowledge_db import KnowledgeDB
            kdb = KnowledgeDB()
            topic_id = kdb.upsert_topic(
                name=name,
                display_name=data.get("display_name", ""),
                description=data.get("description", ""),
            )
            self._json_response({"ok": True, "id": topic_id, "name": name})
        elif path == "/api/thread/hide":
            try:
                data = json.loads(body.decode("utf-8")) if body else {}
            except json.JSONDecodeError:
                self._json_response({"error": "无效 JSON"}, 400)
                return
            thread_id = data.get("thread_id", "")
            hidden = data.get("hidden", 1)
            if not thread_id:
                self._json_response({"error": "缺少 thread_id"}, 400)
                return
            self.conn.execute(
                "UPDATE threads SET hidden = ? WHERE id = ?",
                (hidden, thread_id)
            )
            self.conn.commit()
            self._json_response({"ok": True})
        else:
            self._json_response({"error": "not found"}, 404)


# ======================================================================
# 前端 HTML（自包含 SPA）
# ======================================================================

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kernel Knowledge Base</title>
<style>
:root {
  --bg:#0d1117; --surface:#161b22; --surface2:#1c2333; --border:#30363d;
  --text:#e6edf3; --muted:#8b949e; --accent:#58a6ff; --accent2:#1f6feb;
  --green:#3fb950; --orange:#d29922; --red:#f85149; --purple:#a371f7;
}
*{margin:0;padding:0;box-sizing:border-box;}
body{font-family:-apple-system,'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--text);line-height:1.6;}
a{color:var(--accent);text-decoration:none;} a:hover{text-decoration:underline;}

/* Layout */
.app{display:flex;min-height:100vh;}
.sidebar{width:220px;background:var(--surface);border-right:1px solid var(--border);padding:16px 0;flex-shrink:0;position:fixed;top:0;left:0;bottom:0;overflow-y:auto;}
.main{flex:1;margin-left:220px;padding:24px 32px;min-width:0;}
.sidebar h2{padding:12px 20px;font-size:15px;color:var(--accent);border-bottom:1px solid var(--border);margin-bottom:8px;}
.sidebar .nav-item{display:block;padding:10px 20px;font-size:13px;color:var(--muted);cursor:pointer;border-left:3px solid transparent;transition:all .15s;}
.sidebar .nav-item:hover{background:var(--surface2);color:var(--text);}
.sidebar .nav-item.active{color:var(--accent);border-left-color:var(--accent);background:rgba(88,166,255,.08);}

/* Stats */
.stats-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:14px;margin-bottom:24px;}
.stat-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center;}
.stat-card .num{font-size:2em;font-weight:700;color:var(--accent);}
.stat-card .label{font-size:12px;color:var(--muted);margin-top:2px;}

/* Top senders bar chart */
.bar-chart{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;margin-bottom:24px;}
.bar-chart h3{font-size:14px;margin-bottom:12px;color:var(--muted);}
.bar-row{display:flex;align-items:center;gap:10px;margin-bottom:6px;font-size:12px;}
.bar-row .name{width:180px;text-align:right;color:var(--muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.bar-row .bar{height:18px;background:var(--accent2);border-radius:3px;transition:width .5s;min-width:2px;}
.bar-row .cnt{color:var(--muted);min-width:30px;}

/* Toolbar */
.toolbar{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px;align-items:center;}
.toolbar input[type=text]{flex:1;min-width:200px;padding:8px 12px;font-size:13px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--text);outline:none;}
.toolbar input:focus{border-color:var(--accent);}
.toolbar select,.toolbar button{padding:7px 12px;font-size:12px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--muted);cursor:pointer;}
.toolbar button:hover{background:var(--border);color:var(--text);}

/* Table */
.table-wrap{overflow-x:auto;}
table{width:100%;border-collapse:collapse;font-size:13px;}
th{text-align:left;padding:10px 12px;border-bottom:2px solid var(--border);color:var(--muted);font-weight:600;cursor:pointer;user-select:none;white-space:nowrap;}
th:hover{color:var(--accent);}
td{padding:10px 12px;border-bottom:1px solid var(--border);vertical-align:top;}
tr:hover{background:var(--surface2);}
.subject-cell{max-width:400px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;cursor:pointer;color:var(--accent);}
.subject-cell:hover{text-decoration:underline;}
.score-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;}
.score-hi{background:rgba(63,185,80,.15);color:var(--green);}
.score-mid{background:rgba(210,153,34,.15);color:var(--orange);}
.score-lo{background:rgba(139,148,158,.15);color:var(--muted);}

/* Pagination */
.pager{display:flex;gap:6px;justify-content:center;margin-top:16px;align-items:center;}
.pager button{padding:6px 12px;font-size:12px;border:1px solid var(--border);border-radius:6px;background:var(--surface);color:var(--muted);cursor:pointer;}
.pager button:hover:not(:disabled){background:var(--accent);color:#fff;border-color:var(--accent);}
.pager button:disabled{opacity:.4;cursor:default;}
.pager .info{font-size:12px;color:var(--muted);margin:0 8px;}

/* Detail modal */
.modal-overlay{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.65);z-index:300;display:none;align-items:flex-start;justify-content:center;padding-top:40px;}
.modal-overlay.active{display:flex;}
.modal{background:var(--surface);border:1px solid var(--border);border-radius:12px;width:90%;max-width:900px;max-height:85vh;overflow-y:auto;padding:24px;position:relative;}
.modal .close-btn{position:absolute;top:12px;right:16px;background:none;border:none;color:var(--muted);font-size:22px;cursor:pointer;}
.modal .close-btn:hover{color:var(--text);}
.modal h2{font-size:16px;margin-bottom:16px;padding-right:40px;}
.meta-grid{display:grid;grid-template-columns:auto 1fr;gap:6px 16px;font-size:13px;margin-bottom:16px;}
.meta-grid .k{color:var(--muted);font-weight:600;}
.email-body{background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:16px;font-family:'SF Mono',Consolas,monospace;font-size:12px;line-height:1.7;white-space:pre-wrap;word-break:break-word;max-height:50vh;overflow-y:auto;}

/* Section title */
.section-title{font-size:18px;font-weight:700;margin-bottom:16px;padding-bottom:8px;border-bottom:1px solid var(--border);}

/* loading */
.loading{text-align:center;padding:40px;color:var(--muted);}

@media(max-width:768px){
  .sidebar{display:none;}
  .main{margin-left:0;padding:16px;}
  .stats-grid{grid-template-columns:repeat(2,1fr);}
}

/* Translate dialog */
.trans-dialog{position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.65);z-index:400;display:none;align-items:center;justify-content:center;}
.trans-dialog.active{display:flex;}
.trans-box{background:var(--surface);border:1px solid var(--border);border-radius:12px;width:420px;max-width:95vw;padding:24px;}
.trans-box h3{font-size:16px;margin-bottom:16px;}
.trans-field{margin-bottom:14px;}
.trans-field label{display:block;font-size:12px;color:var(--muted);margin-bottom:4px;font-weight:600;}
.trans-field select,.trans-field input{width:100%;padding:8px 10px;font-size:13px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);outline:none;}
.trans-field select:focus,.trans-field input:focus{border-color:var(--accent);}
.trans-field .hint{font-size:11px;color:var(--muted);margin-top:3px;}
.trans-btns{display:flex;gap:10px;justify-content:flex-end;margin-top:18px;}
.trans-btns button{padding:8px 20px;font-size:13px;border-radius:6px;cursor:pointer;border:1px solid var(--border);}
.btn-cancel{background:var(--surface);color:var(--muted);}
.btn-cancel:hover{background:var(--border);color:var(--text);}
.btn-start{background:var(--accent2);color:#fff;border-color:var(--accent2);}
.btn-start:hover{background:var(--accent);}
.btn-start:disabled{opacity:.5;cursor:default;}
.trans-progress{margin-top:14px;display:none;}
.trans-progress .bar-bg{height:8px;background:var(--bg);border-radius:4px;overflow:hidden;}
.trans-progress .bar-fg{height:100%;background:var(--accent);border-radius:4px;transition:width .3s;}
.trans-progress .info{font-size:12px;color:var(--muted);margin-top:6px;}
.trans-progress .errors{font-size:11px;color:var(--red);margin-top:6px;max-height:80px;overflow-y:auto;}
.btn-translate{display:inline-block;padding:3px 10px;font-size:11px;border:1px solid var(--orange);border-radius:4px;color:var(--orange);cursor:pointer;white-space:nowrap;background:none;}
.btn-translate:hover{background:rgba(210,153,34,.15);}

/* Topic cards */
.topic-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:14px;margin-bottom:24px;}
.topic-card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:16px;cursor:pointer;transition:border-color .2s;}
.topic-card:hover{border-color:var(--accent);}
.topic-card .t-name{font-size:15px;font-weight:700;color:var(--accent);margin-bottom:4px;}
.topic-card .t-count{font-size:24px;font-weight:700;color:var(--text);}
.topic-card .t-label{font-size:12px;color:var(--muted);}
.topic-card .t-desc{font-size:12px;color:var(--muted);margin-top:6px;line-height:1.4;}

/* Summary card in thread list */
.summary-line{font-size:12px;color:var(--muted);max-width:500px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.tags-line{margin-top:4px;}
.tags-line .tag{display:inline-block;padding:1px 6px;margin-right:3px;font-size:10px;border-radius:8px;background:rgba(88,166,255,.1);color:var(--accent);border:1px solid rgba(88,166,255,.2);}

/* Delete button */
.btn-delete{display:inline-block;padding:3px 8px;font-size:11px;border:1px solid var(--red);border-radius:4px;color:var(--red);cursor:pointer;white-space:nowrap;background:none;margin-left:6px;}
.btn-delete:hover{background:rgba(248,81,73,.15);}
</style>
</head>
<body>
<div class="app">
  <div class="sidebar">
    <h2>Kernel KB</h2>
    <div class="nav-item active" onclick="navigate('stats')">Dashboard</div>
    <div class="nav-item" onclick="navigate('topics')">Topics</div>
    <div class="nav-item" onclick="navigate('threads')">Threads</div>
    <div class="nav-item" onclick="navigate('emails')">Emails</div>
    <div class="nav-item" onclick="navigate('search')">Search</div>
  </div>
  <div class="main" id="main"><div class="loading">Loading...</div></div>
</div>

<!-- Detail modal -->
<div class="modal-overlay" id="modalOverlay" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal">
    <button class="close-btn" onclick="closeModal()">&times;</button>
    <div id="modalContent"></div>
  </div>
</div>

<!-- Translate dialog -->
<div class="trans-dialog" id="transDialog" onclick="if(event.target===this)closeTransDialog()">
  <div class="trans-box">
    <h3 id="transTitle">&#127760; 翻译线程</h3>
    <div class="trans-field">
      <label>翻译后端</label>
      <select id="transBackend" onchange="toggleApiFields()">
        <option value="google">Google 翻译 (免费)</option>
        <option value="youdao">有道翻译 (免费)</option>
        <option value="api">AI 翻译 (需要 API Key)</option>
      </select>
    </div>
    <div id="apiFields" style="display:none">
      <div class="trans-field">
        <label>API Key</label>
        <input type="password" id="transApiKey" placeholder="sk-xxxxxxxx">
      </div>
      <div class="trans-field">
        <label>服务商</label>
        <select id="transProvider">
          <option value="deepseek">DeepSeek</option>
          <option value="siliconflow">硅基流动 (SiliconFlow)</option>
          <option value="aliyun">阿里百炼</option>
          <option value="kimi">Kimi (Moonshot)</option>
          <option value="openai">OpenAI</option>
        </select>
      </div>
      <div class="trans-field">
        <label>模型 (留空用默认)</label>
        <input type="text" id="transModel" placeholder="留空使用默认模型">
      </div>
    </div>
    <div class="trans-field">
      <label>代理 (可选)</label>
      <input type="text" id="transProxy" placeholder="如 127.0.0.1:7897">
    </div>
    <div class="trans-progress" id="transProgress">
      <div class="bar-bg"><div class="bar-fg" id="transBar" style="width:0%"></div></div>
      <div class="info" id="transInfo"></div>
      <div class="errors" id="transErrors"></div>
    </div>
    <div class="trans-btns">
      <button class="btn-cancel" onclick="closeTransDialog()">取消</button>
      <button class="btn-start" id="transStartBtn" onclick="startTranslate()">开始翻译</button>
    </div>
  </div>
</div>

<script>
var currentView='stats', emailPage=1, threadPage=1, topicThreadPage=1, emailSort='date', emailOrder='desc', emailSearch='', currentTopicId=0, currentTopicName='';

function $(id){return document.getElementById(id);}
function esc(s){if(!s)return'';var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function parseTags(s){if(!s)return[];try{var r=JSON.parse(s);return Array.isArray(r)?r:[];}catch(e){return s?[s]:[];}}
function navigate(view){
  currentView=view;
  var navs=['stats','topics','threads','emails','search'];
  document.querySelectorAll('.nav-item').forEach(function(n,i){n.classList.toggle('active',navs[i]===view);});
  if(view==='stats')loadStats();
  else if(view==='topics')loadTopics();
  else if(view==='threads'){threadPage=1;loadThreads();}
  else if(view==='emails'){emailPage=1;loadEmails();}
  else if(view==='search')showSearch();
}

function scoreBadge(s){
  if(s>=0.8)return '<span class="score-badge score-hi">'+s.toFixed(1)+'</span>';
  if(s>=0.4)return '<span class="score-badge score-mid">'+s.toFixed(1)+'</span>';
  return '<span class="score-badge score-lo">'+s.toFixed(1)+'</span>';
}

// ── Stats ──
function loadStats(){
  $('main').innerHTML='<div class="loading">Loading...</div>';
  fetch('/api/stats').then(function(r){return r.json();}).then(function(d){
    var maxCnt=d.top_senders.length?d.top_senders[0].count:1;
    var bars=d.top_senders.map(function(s){
      var w=Math.max(2,Math.round(s.count/maxCnt*300));
      return '<div class="bar-row"><span class="name">'+esc(s.name)+'</span><span class="bar" style="width:'+w+'px"></span><span class="cnt">'+s.count+'</span></div>';
    }).join('');
    $('main').innerHTML=
      '<div class="section-title">Dashboard</div>'+
      '<div class="stats-grid">'+
        '<div class="stat-card"><div class="num">'+d.email_count+'</div><div class="label">Emails</div></div>'+
        '<div class="stat-card"><div class="num">'+d.thread_count+'</div><div class="label">Threads</div></div>'+
        '<div class="stat-card" style="cursor:pointer" onclick="navigate(\'topics\')"><div class="num">'+d.topic_count+'</div><div class="label">Topics</div></div>'+
        '<div class="stat-card"><div class="num">'+d.processed_count+'</div><div class="label">Summarized</div></div>'+
        '<div class="stat-card"><div class="num">'+d.score_high+'</div><div class="label">High Relevance</div></div>'+
        '<div class="stat-card"><div class="num" style="font-size:12px;color:var(--muted);padding-top:8px">'+esc(d.date_min?d.date_min.substring(0,16):'')+'<br>~<br>'+esc(d.date_max?d.date_max.substring(0,16):'')+'</div><div class="label">Date Range</div></div>'+
      '</div>'+
      '<div class="bar-chart"><h3>Top Senders</h3>'+bars+'</div>';
  });
}

// ── Emails ──
function loadEmails(){
  $('main').innerHTML='<div class="loading">Loading...</div>';
  var url='/api/emails?page='+emailPage+'&per_page=50&sort='+emailSort+'&order='+emailOrder;
  if(emailSearch)url+='&search='+encodeURIComponent(emailSearch);
  fetch(url).then(function(r){return r.json();}).then(function(d){
    var rows=d.emails.map(function(e){
      return '<tr>'+
        '<td class="subject-cell" onclick="showEmail('+e.id+')">'+esc(e.subject)+'</td>'+
        '<td>'+esc(e.from_name)+'</td>'+
        '<td style="white-space:nowrap">'+esc(e.date?e.date.substring(0,16):'')+'</td>'+
        '<td>'+scoreBadge(e.relevance_score||0)+'</td>'+
      '</tr>';
    }).join('');
    var sortIcon=function(col){return emailSort===col?(emailOrder==='asc'?' &#9650;':' &#9660;'):'';};
    $('main').innerHTML=
      '<div class="section-title">Emails ('+d.total+')</div>'+
      '<div class="toolbar">'+
        '<input type="text" id="emailSearchInput" placeholder="Search subject / sender / body..." value="'+esc(emailSearch)+'" onkeydown="if(event.key===\'Enter\'){emailSearch=this.value;emailPage=1;loadEmails();}">'+
        '<button onclick="emailSearch=$(\'emailSearchInput\').value;emailPage=1;loadEmails();">Search</button>'+
        '<button onclick="emailSearch=\'\';emailPage=1;loadEmails();">Clear</button>'+
      '</div>'+
      '<div class="table-wrap"><table>'+
        '<tr><th onclick="toggleSort(\'subject\')">Subject'+sortIcon('subject')+'</th>'+
        '<th onclick="toggleSort(\'from\')">From'+sortIcon('from')+'</th>'+
        '<th onclick="toggleSort(\'date\')">Date'+sortIcon('date')+'</th>'+
        '<th onclick="toggleSort(\'score\')">Score'+sortIcon('score')+'</th></tr>'+
        rows+
      '</table></div>'+
      pagerHtml(d.page,d.pages,'emailPage','loadEmails');
  });
}
function toggleSort(col){
  if(emailSort===col)emailOrder=emailOrder==='asc'?'desc':'asc';
  else{emailSort=col;emailOrder='desc';}
  emailPage=1;loadEmails();
}

// ── Topics ──
function loadTopics(){
  $('main').innerHTML='<div class="loading">Loading...</div>';
  fetch('/api/topics').then(function(r){return r.json();}).then(function(d){
    var cards=d.topics.map(function(t){
      return '<div class="topic-card" onclick="viewTopic('+t.id+',\''+esc(t.name)+'\')">'+
        '<div class="t-name">'+esc(t.display_name||t.name)+'</div>'+
        '<div class="t-count">'+t.thread_count+'</div>'+
        '<div class="t-label">threads</div>'+
        (t.description?'<div class="t-desc">'+esc(t.description)+'</div>':'')+
      '</div>';
    }).join('');
    if(!d.topics.length)cards='<div class="loading">暂无话题。运行 batch_process.py --summarize 后自动生成。</div>';
    $('main').innerHTML=
      '<div class="section-title">Topics ('+d.topics.length+')</div>'+
      '<div class="topic-grid">'+cards+'</div>';
  });
}
function viewTopic(id,name){
  currentTopicId=id;
  currentTopicName=name;
  topicThreadPage=1;
  loadTopicThreads(id,name);
}
function loadCurrentTopicThreads(){loadTopicThreads(currentTopicId,currentTopicName);}
function loadTopicThreads(id,name){
  $('main').innerHTML='<div class="loading">Loading...</div>';
  fetch('/api/topics/'+id+'/threads?page='+topicThreadPage+'&per_page=30').then(function(r){return r.json();}).then(function(d){
    var rows=d.threads.map(function(t){
      var tags=parseTags(t.tags);
      var tagsHtml=tags.map(function(tg){return '<span class="tag">'+esc(tg)+'</span>';}).join('');
      var summary=(t.summary_zh||'').substring(0,100);
      var transBtn='';
      var safeId=encodeURIComponent(t.id);
      if(t.translated_html_path){
        transBtn='<a href="/translated/'+safeId+'" target="_blank" style="display:inline-block;padding:3px 10px;font-size:11px;border:1px solid var(--accent);border-radius:4px;color:var(--accent);text-decoration:none;white-space:nowrap;">&#127760; 查看翻译</a>';
      }
      return '<tr>'+
        '<td>'+esc(t.subject)+'<br><span class="summary-line">'+esc(summary)+'</span>'+(tagsHtml?'<div class="tags-line">'+tagsHtml+'</div>':'')+'</td>'+
        '<td>'+t.email_count+'</td>'+
        '<td style="white-space:nowrap">'+esc(t.start_date?t.start_date.substring(0,10):'')+'</td>'+
        '<td>'+transBtn+
          '<span class="btn-delete" onclick="hideThread(\''+safeId+'\')">&#128465;</span>'+
        '</td>'+
      '</tr>';
    }).join('');
    $('main').innerHTML=
      '<div class="section-title"><span style="cursor:pointer;color:var(--accent)" onclick="loadTopics()">Topics</span> / '+esc(name)+' ('+d.total+')</div>'+
      '<div class="table-wrap"><table>'+
        '<tr><th>Subject / Summary</th><th>Emails</th><th>Date</th><th>Actions</th></tr>'+
        rows+
      '</table></div>'+
      pagerHtml(d.page,d.pages,'topicThreadPage','loadCurrentTopicThreads');
  });
}
function hideThread(encodedId){
  if(!confirm('确认隐藏此线程？'))return;
  fetch('/api/thread/hide',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({thread_id:decodeURIComponent(encodedId),hidden:1})
  }).then(function(){
    if(currentTopicId)loadTopicThreads(currentTopicId,'');
    else loadThreads();
  });
}

// ── Threads ──
function loadThreads(){
  $('main').innerHTML='<div class="loading">Loading...</div>';
  fetch('/api/threads?page='+threadPage+'&per_page=30').then(function(r){return r.json();}).then(function(d){
    var untranslatedIds=[];
    var rows=d.threads.map(function(t){
      var tags=parseTags(t.tags);
      var tagsHtml=tags.map(function(tg){return '<span class="tag">'+esc(tg)+'</span>';}).join('');
      var summary=(t.summary_zh||'').substring(0,100);
      var transBtn='';
      var safeId=encodeURIComponent(t.id);
      if(t.translated_html_path){
        transBtn='<a href="/translated/'+safeId+'" target="_blank" style="display:inline-block;padding:3px 10px;font-size:11px;border:1px solid var(--accent);border-radius:4px;color:var(--accent);text-decoration:none;white-space:nowrap;">&#127760; 查看翻译</a> '+
          '<span class="btn-translate" onclick="openTransDialogEnc(\''+safeId+'\')">重新翻译</span>';
      }else{
        untranslatedIds.push(t.id);
        transBtn='<span class="btn-translate" onclick="openTransDialogEnc(\''+safeId+'\')">&#9654; 翻译</span>';
      }
      transBtn+='<span class="btn-delete" onclick="hideThread(\''+safeId+'\')">&#128465;</span>';
      return '<tr>'+
        '<td>'+esc(t.subject)+(summary?'<br><span class="summary-line">'+esc(summary)+'</span>':'')+(tagsHtml?'<div class="tags-line">'+tagsHtml+'</div>':'')+'</td>'+
        '<td>'+t.email_count+'</td>'+
        '<td style="white-space:nowrap">'+esc(t.start_date?t.start_date.substring(0,16):'')+'</td>'+
        '<td>'+transBtn+'</td>'+
      '</tr>';
    }).join('');
    var batchBtn='';
    if(untranslatedIds.length>0){
      batchBtn='<button style="padding:6px 16px;font-size:12px;border:1px solid var(--orange);border-radius:6px;background:rgba(210,153,34,.1);color:var(--orange);cursor:pointer;margin-left:12px;" '+
        'onclick="openTransDialogAll()">&#9654; 翻译本页全部未翻译 ('+untranslatedIds.length+')</button>';
      window._pageUntranslatedIds=untranslatedIds;
    }
    $('main').innerHTML=
      '<div class="section-title">Threads ('+d.total+')'+batchBtn+'</div>'+
      '<div class="table-wrap"><table>'+
        '<tr><th>Subject / Summary</th><th>Emails</th><th>Date</th><th>Actions</th></tr>'+
        rows+
      '</table></div>'+
      pagerHtml(d.page,d.pages,'threadPage','loadThreads');
  });
}

// ── Search ──
function showSearch(){
  $('main').innerHTML=
    '<div class="section-title">Full-Text Search</div>'+
    '<div class="toolbar">'+
      '<input type="text" id="ftsInput" placeholder="Enter search query (e.g. fair sleeper, latency nice)..." onkeydown="if(event.key===\'Enter\')doSearch();">'+
      '<button onclick="doSearch()">Search</button>'+
    '</div>'+
    '<div id="searchResults"></div>';
  setTimeout(function(){$('ftsInput').focus();},100);
}
function doSearch(){
  var q=$('ftsInput').value.trim();
  if(!q)return;
  $('searchResults').innerHTML='<div class="loading">Searching...</div>';
  fetch('/api/search?q='+encodeURIComponent(q)+'&limit=100').then(function(r){return r.json();}).then(function(d){
    if(!d.results.length){$('searchResults').innerHTML='<div class="loading">No results found</div>';return;}
    var rows=d.results.map(function(e){
      return '<tr>'+
        '<td class="subject-cell" onclick="showEmail('+e.id+')">'+esc(e.subject)+'</td>'+
        '<td>'+esc(e.from_name)+'</td>'+
        '<td style="white-space:nowrap">'+esc(e.date?e.date.substring(0,16):'')+'</td>'+
        '<td>'+scoreBadge(e.relevance_score||0)+'</td>'+
      '</tr>';
    }).join('');
    $('searchResults').innerHTML=
      '<p style="color:var(--muted);font-size:13px;margin-bottom:12px;">Found '+d.total+' results</p>'+
      '<div class="table-wrap"><table>'+
      '<tr><th>Subject</th><th>From</th><th>Date</th><th>Score</th></tr>'+
      rows+'</table></div>';
  });
}

// ── Email detail modal ──
function showEmail(id){
  $('modalContent').innerHTML='<div class="loading">Loading...</div>';
  $('modalOverlay').classList.add('active');
  fetch('/api/email/'+id).then(function(r){return r.json();}).then(function(e){
    $('modalContent').innerHTML=
      '<h2>'+esc(e.subject)+'</h2>'+
      '<div class="meta-grid">'+
        '<span class="k">From</span><span>'+esc(e.from_email || e.from_name)+'</span>'+
        '<span class="k">Date</span><span>'+esc(e.date)+'</span>'+
        '<span class="k">Message-ID</span><span style="font-size:11px;word-break:break-all">'+esc(e.message_id)+'</span>'+
        '<span class="k">In-Reply-To</span><span style="font-size:11px;word-break:break-all">'+esc(e.in_reply_to||'(none)')+'</span>'+
        '<span class="k">Relevance</span><span>'+scoreBadge(e.relevance_score||0)+' '+esc(e.relevance_reason||'')+'</span>'+
      '</div>'+
      '<div class="email-body">'+esc(e.body)+'</div>';
  });
}
function closeModal(){$('modalOverlay').classList.remove('active');}
document.addEventListener('keydown',function(e){if(e.key==='Escape'){closeModal();closeTransDialog();}});

// ── Translate dialog ──
var _transIds=[];
var _transPollTimer=null;

function openTransDialogEnc(encodedId){
  openTransDialog([decodeURIComponent(encodedId)]);
}

function toggleApiFields(){
  var v=$('transBackend').value;
  $('apiFields').style.display=v==='api'?'block':'none';
}

function openTransDialog(ids){
  _transIds=ids;
  var title=ids.length===1?'翻译 1 个线程':'翻译 '+ids.length+' 个线程';
  $('transTitle').textContent='\u{1F310} '+title;
  $('transProgress').style.display='none';
  $('transStartBtn').disabled=false;
  $('transStartBtn').textContent='开始翻译';
  $('transErrors').innerHTML='';
  $('transDialog').classList.add('active');
}

function openTransDialogAll(){
  if(window._pageUntranslatedIds && window._pageUntranslatedIds.length>0){
    openTransDialog(window._pageUntranslatedIds);
  }
}

function closeTransDialog(){
  $('transDialog').classList.remove('active');
  if(_transPollTimer){clearInterval(_transPollTimer);_transPollTimer=null;}
}

function startTranslate(){
  var backend=$('transBackend').value;
  var body={thread_ids:_transIds,backend:backend};
  if(backend==='api'){
    body.api_key=$('transApiKey').value.trim();
    body.api_provider=$('transProvider').value;
    body.model=$('transModel').value.trim();
    if(!body.api_key){alert('请输入 API Key');return;}
  }
  var proxy=$('transProxy').value.trim();
  if(proxy)body.proxy=proxy;

  $('transStartBtn').disabled=true;
  $('transStartBtn').textContent='提交中...';

  fetch('/api/translate',{
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)
  }).then(function(r){return r.json();}).then(function(d){
    if(d.error){
      alert(d.error);
      $('transStartBtn').disabled=false;
      $('transStartBtn').textContent='开始翻译';
      return;
    }
    $('transStartBtn').textContent='翻译中...';
    $('transProgress').style.display='block';
    $('transBar').style.width='0%';
    $('transInfo').textContent='正在翻译 0/'+d.total+'...';
    _transPollTimer=setInterval(pollTranslateStatus,2000);
  });
}

function pollTranslateStatus(){
  fetch('/api/translate/status').then(function(r){return r.json();}).then(function(s){
    if(!s.running && !s.total){return;}
    var pct=s.total?Math.round(s.done/s.total*100):0;
    $('transBar').style.width=pct+'%';
    var info='进度: '+s.done+'/'+s.total+' (成功 '+s.success+', 失败 '+s.failed+')';
    if(s.current)info+=' | 当前: '+s.current;
    $('transInfo').textContent=info;
    if(s.errors && s.errors.length>0){
      $('transErrors').innerHTML=s.errors.map(function(e){return '<div>'+esc(e)+'</div>';}).join('');
    }
    if(!s.running){
      clearInterval(_transPollTimer);_transPollTimer=null;
      $('transStartBtn').textContent='完成!';
      setTimeout(function(){
        $('transStartBtn').disabled=false;
        $('transStartBtn').textContent='开始翻译';
        if(currentView==='threads')loadThreads();
      },1500);
    }
  });
}

// ── Pagination helper ──
function pagerHtml(page,pages,pageVar,fn){
  return '<div class="pager">'+
    '<button onclick="'+pageVar+'=1;'+fn+'()" '+(page<=1?'disabled':'')+'>&#171;</button>'+
    '<button onclick="'+pageVar+'--;'+fn+'()" '+(page<=1?'disabled':'')+'>&#8249; Prev</button>'+
    '<span class="info">Page '+page+' / '+pages+'</span>'+
    '<button onclick="'+pageVar+'++;'+fn+'()" '+(page>=pages?'disabled':'')+'>Next &#8250;</button>'+
    '<button onclick="'+pageVar+'='+pages+';'+fn+'()" '+(page>=pages?'disabled':'')+'>&#187;</button>'+
  '</div>';
}

// init
loadStats();
</script>
</body>
</html>"""


# ======================================================================
# 启动
# ======================================================================

def main():
    parser = argparse.ArgumentParser(description="知识库网页浏览器")
    parser.add_argument("--host", default="127.0.0.1", help="绑定地址")
    parser.add_argument("--port", type=int, default=8765, help="端口号")
    parser.add_argument("--no-open", action="store_true", help="不自动打开浏览器")
    args = parser.parse_args()

    conn = get_conn()
    stats = api_stats(conn)
    logger.info("知识库: %d emails, %d threads", stats["email_count"], stats["thread_count"])

    KBHandler.conn = conn
    server = HTTPServer((args.host, args.port), KBHandler)
    url = f"http://{args.host}:{args.port}"
    logger.info("Server running at %s", url)

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
        server.server_close()


if __name__ == "__main__":
    main()