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
import webbrowser
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

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
    tc = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
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
    total = conn.execute("SELECT COUNT(*) FROM threads").fetchone()[0]
    rows = conn.execute("""
        SELECT * FROM threads ORDER BY start_date DESC LIMIT ? OFFSET ?
    """, (per_page, offset)).fetchall()
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
        elif path == "/api/search":
            self._json_response(api_search(
                self.conn, qp("q", ""), int(qp("limit", "100"))
            ))
        elif path.startswith("/translated/"):
            # 静态文件服务：返回翻译 HTML 文件
            thread_id = path[len("/translated/"):]
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
</style>
</head>
<body>
<div class="app">
  <div class="sidebar">
    <h2>Kernel KB</h2>
    <div class="nav-item active" onclick="navigate('stats')">Dashboard</div>
    <div class="nav-item" onclick="navigate('emails')">Emails</div>
    <div class="nav-item" onclick="navigate('threads')">Threads</div>
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

<script>
var currentView='stats', emailPage=1, threadPage=1, emailSort='date', emailOrder='desc', emailSearch='';

function $(id){return document.getElementById(id);}
function esc(s){if(!s)return'';var d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function navigate(view){
  currentView=view;
  document.querySelectorAll('.nav-item').forEach(function(n,i){n.classList.toggle('active',['stats','emails','threads','search'][i]===view);});
  if(view==='stats')loadStats();
  else if(view==='emails'){emailPage=1;loadEmails();}
  else if(view==='threads'){threadPage=1;loadThreads();}
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
        '<div class="stat-card"><div class="num">'+d.score_high+'</div><div class="label">High Relevance</div></div>'+
        '<div class="stat-card"><div class="num">'+d.score_mid+'</div><div class="label">Medium</div></div>'+
        '<div class="stat-card"><div class="num">'+d.score_low+'</div><div class="label">Low / Unscored</div></div>'+
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

// ── Threads ──
function loadThreads(){
  $('main').innerHTML='<div class="loading">Loading...</div>';
  fetch('/api/threads?page='+threadPage+'&per_page=30').then(function(r){return r.json();}).then(function(d){
    var rows=d.threads.map(function(t){
      var transBtn='';
      if(t.translated_html_path){
        transBtn='<a href="/translated/'+encodeURIComponent(t.id)+'" target="_blank" style="display:inline-block;padding:3px 10px;font-size:11px;border:1px solid var(--accent);border-radius:4px;color:var(--accent);text-decoration:none;white-space:nowrap;">&#127760; 查看翻译</a>';
      }else{
        transBtn='<span style="color:var(--muted);font-size:11px;">未翻译</span>';
      }
      return '<tr>'+
        '<td>'+esc(t.subject)+'</td>'+
        '<td>'+t.email_count+'</td>'+
        '<td>'+t.participant_count+'</td>'+
        '<td style="white-space:nowrap">'+esc(t.start_date?t.start_date.substring(0,16):'')+'</td>'+
        '<td>'+transBtn+'</td>'+
      '</tr>';
    }).join('');
    $('main').innerHTML=
      '<div class="section-title">Threads ('+d.total+')</div>'+
      '<div class="table-wrap"><table>'+
        '<tr><th>Subject</th><th>Emails</th><th>Participants</th><th>Start Date</th><th>Translation</th></tr>'+
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
document.addEventListener('keydown',function(e){if(e.key==='Escape')closeModal();});

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