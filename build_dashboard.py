#!/usr/bin/env python3
"""build_dashboard.py — 扫描 data/ 目录，生成自包含 Dashboard HTML

用法:
    python build_dashboard.py              # 扫描 data/ 并生成 dashboard.html
    python build_dashboard.py -o /tmp/d.html  # 指定输出路径
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from email_translator.config import OUTPUT_DIR, EMAILS_DIR

# ─── 数据扫描与元数据提取 ──────────────────────────────────────────────────────

def scan_output_dir(output_dir: Path) -> list:
    """扫描 data/output/ 下所有产物文件，返回 (stem, ext, path, size_kb) 列表"""
    results = []
    if not output_dir.exists():
        return results
    for f in sorted(output_dir.iterdir()):
        if f.is_file() and f.name != "dashboard.html":
            results.append({
                "stem": f.stem,
                "ext": f.suffix,
                "path": str(f),
                "relpath": str(f.relative_to(f.parent.parent.parent)) if len(f.parts) > 3 else f.name,
                "size_kb": f.stat().st_size // 1024,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
            })
    return results


def scan_emails_dir(emails_dir: Path) -> list:
    """扫描 data/emails/ 下原始邮件 JSON"""
    results = []
    if not emails_dir.exists():
        return results
    for f in sorted(emails_dir.iterdir()):
        if f.is_file() and f.suffix == ".json":
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                email_count = len(data) if isinstance(data, list) else 0
            except Exception:
                email_count = 0
            results.append({
                "stem": f.stem,
                "path": str(f),
                "relpath": str(f.relative_to(f.parent.parent.parent)) if len(f.parts) > 3 else f.name,
                "size_kb": f.stat().st_size // 1024,
                "email_count": email_count,
                "mtime": datetime.fromtimestamp(f.stat().st_mtime).isoformat(timespec="seconds"),
            })
    return results


def extract_metadata_from_html(html_path: Path) -> dict:
    """从 _translated.html 文件头部快速提取元数据（不解析全文）"""
    meta = {}
    try:
        # 只读前 8KB
        head = html_path.read_text(encoding="utf-8", errors="ignore")[:8192]
        # title
        m = re.search(r"<title>(.*?)</title>", head)
        if m:
            meta["subject"] = m.group(1).strip()
        # source-hash
        m = re.search(r'content="([a-f0-9]+)"', head)
        if m:
            meta["source_hash"] = m.group(1)
        # 统计 email-node 数量 (需要读更多)
        full = html_path.read_text(encoding="utf-8", errors="ignore")
        meta["email_count"] = full.count('class="email-node"')
        meta["thread_count"] = full.count('class="thread-title"')
        # 提取所有作者
        authors = set(re.findall(r'data-author="([^"]+)"', full))
        meta["authors"] = sorted(authors)
        meta["participant_count"] = len(authors)
    except Exception:
        pass
    return meta


def extract_metadata_from_context(txt_path: Path) -> dict:
    """从 context_full.txt 头部提取 commit 元数据"""
    meta = {}
    try:
        head = txt_path.read_text(encoding="utf-8", errors="ignore")[:4096]
        for line in head.splitlines():
            if line.startswith("Commit:"):
                meta["commit_hash"] = line.split(":", 1)[1].strip()[:12]
            elif line.startswith("Author:"):
                meta["author"] = line.split(":", 1)[1].strip()
            elif line.startswith("Date:"):
                meta["date"] = line.split(":", 1)[1].strip()
            elif line.startswith("Subject:"):
                meta["subject"] = line.split(":", 1)[1].strip()
            elif line.startswith("Subsystem:"):
                meta["subsystem"] = line.split(":", 1)[1].strip()
            elif line.startswith("Patchset:"):
                meta["patchset"] = line.split(":", 1)[1].strip()
        # 邮件统计
        m = re.search(r"原始 (\d+) 封", head)
        if m:
            meta["raw_email_count"] = int(m.group(1))
        m = re.search(r"保留 (\d+) 封", head)
        if m:
            meta["kept_email_count"] = int(m.group(1))
    except Exception:
        pass
    return meta


def _guess_commit_hash(stem: str) -> str:
    """从文件名推断 commit hash（如 d07f09a1f99c_translated → d07f09a1f99c）"""
    m = re.match(r"([0-9a-f]{8,12})", stem)
    return m.group(1) if m else ""


def correlate_artifacts(output_files: list, email_files: list) -> list:
    """关联同一 commit 的多种产物，返回结构化报告列表"""
    # 按 commit hash 分组
    groups = {}  # hash -> {artifacts}

    for f in output_files:
        h = _guess_commit_hash(f["stem"])
        if not h:
            # 非 commit 产物（test_ 开头等），也保留
            h = f["stem"]
        if h not in groups:
            groups[h] = {
                "commit_hash": h if re.match(r"[0-9a-f]{8,12}$", h) else "",
                "artifacts": {},
                "meta": {},
            }
        if f["ext"] == ".html":
            if "translated" in f["stem"]:
                groups[h]["artifacts"]["translated_html"] = f
            else:
                groups[h]["artifacts"].setdefault("other_html", [])
                groups[h]["artifacts"]["other_html"].append(f)
        elif f["ext"] == ".txt":
            groups[h]["artifacts"]["context_txt"] = f

    for f in email_files:
        # 尝试用时间戳或主题关联
        # email JSON 文件名通常包含主题，不好直接用 hash 关联
        # 放入一个特殊 key
        best_match = None
        for h, g in groups.items():
            ctx = g["artifacts"].get("context_txt")
            if ctx:
                # 简单匹配：email JSON 的 stem 子串出现在 context 的 meta 中
                ctx_meta = g.get("meta", {})
                if ctx_meta.get("subject", "").replace(" ", "_")[:30] in f["stem"]:
                    best_match = h
                    break
        if not best_match:
            # 单独成组
            key = f"email_{f['stem'][:30]}"
            groups[key] = {
                "commit_hash": "",
                "artifacts": {"emails_json": f},
                "meta": {},
            }
        else:
            groups[best_match]["artifacts"]["emails_json"] = f

    # 提取详细元数据
    for h, g in groups.items():
        arts = g["artifacts"]
        if "context_txt" in arts:
            p = Path(arts["context_txt"]["path"])
            g["meta"].update(extract_metadata_from_context(p))
        if "translated_html" in arts:
            p = Path(arts["translated_html"]["path"])
            g["meta"].update(extract_metadata_from_html(p))
        if "emails_json" in arts:
            g["meta"].setdefault("email_count", arts["emails_json"].get("email_count", 0))

    # 构建最终列表
    reports = []
    for h, g in groups.items():
        arts = g["artifacts"]
        meta = g["meta"]

        # 确定状态
        if "translated_html" in arts:
            status = "translated"
            status_icon = "\U0001f4dd"
        elif "context_txt" in arts:
            status = "packed"
            status_icon = "\U0001f4e6"
        elif "emails_json" in arts:
            status = "raw"
            status_icon = "\U0001f4e7"
        else:
            status = "other"
            status_icon = "\U0001f4c4"

        # 主要显示文件
        primary = (
            arts.get("translated_html")
            or arts.get("context_txt")
            or arts.get("emails_json")
            or (arts.get("other_html", [None])[0] if arts.get("other_html") else None)
        )

        reports.append({
            "id": h,
            "commit_hash": g["commit_hash"],
            "subject": meta.get("subject", h),
            "author": meta.get("author", ""),
            "date": meta.get("date", ""),
            "subsystem": meta.get("subsystem", ""),
            "status": status,
            "status_icon": status_icon,
            "email_count": meta.get("email_count", meta.get("kept_email_count", 0)),
            "thread_count": meta.get("thread_count", 0),
            "participant_count": meta.get("participant_count", 0),
            "authors": meta.get("authors", []),
            "artifacts": {k: v if not isinstance(v, list) else v for k, v in arts.items()},
            "primary_path": primary["relpath"] if primary else "",
            "primary_size_kb": primary["size_kb"] if primary else 0,
            "mtime": primary["mtime"] if primary else "",
        })

    # 按修改时间倒序
    reports.sort(key=lambda r: r["mtime"], reverse=True)
    return reports


# ─── Dashboard HTML 模板 ───────────────────────────────────────────────────────

DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Kernel Email Dashboard</title>
<style>
:root {
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --text-muted: #8b949e; --accent: #58a6ff;
  --red: #f85149; --orange: #d29922; --green: #3fb950; --gray: #6c757d;
  --card-hover: #1c2333;
}
body.light-theme {
  --bg: #fff; --surface: #f6f8fa; --border: #d0d7de;
  --text: #1f2328; --text-muted: #656d76; --accent: #0969da;
  --red: #cf222e; --orange: #bf8700; --green: #1a7f37; --gray: #6c757d;
  --card-hover: #eef2f7;
}
* { margin:0; padding:0; box-sizing:border-box; }
body {
  font-family: -apple-system,'Segoe UI',Roboto,sans-serif;
  background: var(--bg); color: var(--text);
  line-height: 1.6; max-width: 1200px; margin: 0 auto; padding: 24px;
  transition: background .3s, color .3s;
}
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

/* ── 顶部 ── */
.header { margin-bottom: 20px; }
.header h1 { font-size: 1.6em; margin-bottom: 4px; }
.header .subtitle { color: var(--text-muted); font-size: 13px; }

/* ── 统计栏 ── */
.stats-bar {
  display: flex; gap: 16px; flex-wrap: wrap;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 20px; margin-bottom: 16px;
}
.stat-item { text-align: center; min-width: 80px; }
.stat-item .num { font-size: 1.8em; font-weight: 700; color: var(--accent); }
.stat-item .label { font-size: 12px; color: var(--text-muted); }

/* ── 工具栏 ── */
.toolbar {
  display: flex; gap: 10px; flex-wrap: wrap; align-items: center;
  margin-bottom: 16px;
}
.toolbar input[type=text] {
  flex: 1; min-width: 200px; padding: 8px 12px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--surface); color: var(--text); outline: none;
}
.toolbar input:focus { border-color: var(--accent); }
.toolbar select {
  padding: 7px 10px; font-size: 12px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--surface); color: var(--text); cursor: pointer;
}
.toolbar button {
  padding: 7px 14px; font-size: 12px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--surface); color: var(--text-muted);
  cursor: pointer; white-space: nowrap;
}
.toolbar button:hover { background: var(--border); color: var(--text); }

/* ── 卡片网格 ── */
.grid {
  display: grid; grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
  gap: 16px;
}
.card {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px; transition: all .2s;
  cursor: default; position: relative;
}
.card:hover { background: var(--card-hover); border-color: var(--accent); transform: translateY(-2px); box-shadow: 0 4px 12px rgba(0,0,0,.3); }
.card .card-head { display: flex; align-items: flex-start; gap: 10px; margin-bottom: 10px; }
.card .hash {
  font-family: 'SF Mono',Consolas,monospace; font-size: 12px;
  background: rgba(88,166,255,.12); color: var(--accent);
  padding: 2px 8px; border-radius: 4px; flex-shrink: 0;
}
.card .subject {
  font-size: 14px; font-weight: 600; line-height: 1.4;
  display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
  overflow: hidden;
}
.card .meta-row {
  display: flex; gap: 12px; flex-wrap: wrap; font-size: 12px;
  color: var(--text-muted); margin-bottom: 8px;
}
.card .subsys-tag {
  display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 11px; color: #fff; font-weight: 500;
}
.card .stats-row {
  display: flex; gap: 14px; font-size: 12px; color: var(--text-muted);
  margin-bottom: 10px;
}
.card .stats-row span { display: flex; align-items: center; gap: 3px; }
.card .status-badge {
  position: absolute; top: 12px; right: 12px;
  font-size: 11px; padding: 2px 8px; border-radius: 10px;
  font-weight: 500;
}
.status-translated { background: rgba(63,185,80,.15); color: var(--green); }
.status-packed { background: rgba(210,153,34,.15); color: var(--orange); }
.status-raw { background: rgba(88,166,255,.15); color: var(--accent); }
.status-other { background: rgba(108,117,125,.15); color: var(--gray); }
.card .actions {
  display: flex; gap: 8px; padding-top: 10px;
  border-top: 1px solid var(--border);
}
.card .actions a, .card .actions button {
  flex: 1; text-align: center; padding: 6px 0; font-size: 12px;
  border: 1px solid var(--border); border-radius: 6px;
  background: transparent; color: var(--text-muted); cursor: pointer;
  text-decoration: none; transition: all .15s;
}
.card .actions a:hover, .card .actions button:hover {
  background: var(--accent); color: #fff; border-color: var(--accent);
}
.no-results {
  text-align: center; padding: 60px 20px; color: var(--text-muted);
  font-size: 14px; display: none;
}
.no-results.active { display: block; }

/* ── 预览模态框 ── */
.modal-overlay {
  position: fixed; top:0; left:0; right:0; bottom:0;
  background: rgba(0,0,0,.6); z-index: 300; display: none;
  align-items: center; justify-content: center;
}
.modal-overlay.active { display: flex; }
.modal {
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; width: 90%; max-width: 700px; max-height: 80vh;
  overflow-y: auto; padding: 24px; position: relative;
}
.modal h3 { margin-bottom: 12px; font-size: 16px; }
.modal .close-modal {
  position: absolute; top: 12px; right: 16px;
  background: none; border: none; color: var(--text-muted); font-size: 20px; cursor: pointer;
}
.modal .close-modal:hover { color: var(--text); }
.modal .preview-item {
  padding: 8px 0; border-bottom: 1px solid var(--border); font-size: 13px;
}
.modal .preview-item:last-child { border-bottom: none; }

/* ── 响应式 ── */
@media (max-width: 768px) {
  .grid { grid-template-columns: 1fr; }
  .stats-bar { gap: 10px; }
  body { padding: 12px; }
}

/* ── 生成时间 ── */
.footer { text-align: center; padding: 24px 0 8px; font-size: 12px; color: var(--text-muted); }
</style>
</head>
<body>

<div class="header">
  <h1>&#128231; Kernel Email Dashboard</h1>
  <div class="subtitle">多 Commit 邮件缓存可视化阅读器 &mdash; 自动扫描 data/ 目录</div>
</div>

<div class="stats-bar" id="statsBar">
  <div class="stat-item"><div class="num" id="statReports">0</div><div class="label">报告总数</div></div>
  <div class="stat-item"><div class="num" id="statEmails">0</div><div class="label">总邮件数</div></div>
  <div class="stat-item"><div class="num" id="statTranslated">0</div><div class="label">已翻译</div></div>
  <div class="stat-item"><div class="num" id="statSubsystems">0</div><div class="label">子系统</div></div>
</div>

<div class="toolbar">
  <input type="text" id="searchInput" placeholder="搜索 commit hash / 主题 / 作者..." oninput="filterCards()">
  <select id="statusFilter" onchange="filterCards()">
    <option value="">全部状态</option>
    <option value="translated">已翻译</option>
    <option value="packed">已打包</option>
    <option value="raw">原始</option>
  </select>
  <select id="subsysFilter" onchange="filterCards()">
    <option value="">全部子系统</option>
  </select>
  <select id="sortBy" onchange="sortCards()">
    <option value="mtime">最近修改</option>
    <option value="emails">邮件数</option>
    <option value="size">文件大小</option>
    <option value="subject">名称</option>
  </select>
  <button onclick="toggleTheme()">&#9728;/&#9790; 切换主题</button>
</div>

<div class="grid" id="cardGrid"></div>
<div class="no-results" id="noResults">没有找到匹配的报告</div>

<!-- 预览模态框 -->
<div class="modal-overlay" id="modalOverlay" onclick="closeModal(event)">
  <div class="modal" id="modal">
    <button class="close-modal" onclick="closeModal()">&times;</button>
    <h3 id="modalTitle">预览</h3>
    <div id="modalBody"></div>
  </div>
</div>

<div class="footer">
  Dashboard 生成时间: <GENERATED_TIME> &mdash; 由 build_dashboard.py 自动生成
</div>

<script>
var REPORTS = <REPORTS_JSON>;

var SUBSYS_COLORS = {
  'sched': '#58a6ff', 'mm': '#f0883e', 'fs': '#a371f7', 'net': '#3fb950',
  'drivers': '#d29922', 'kernel': '#f85149', 'arch': '#79c0ff', 'block': '#db61a2',
};
function subsysColor(s) {
  if (!s) return '#6c757d';
  var key = s.toLowerCase().split('/')[0].split(':')[0];
  return SUBSYS_COLORS[key] || '#6c757d';
}

function renderCards(data) {
  var grid = document.getElementById('cardGrid');
  grid.innerHTML = '';
  if (!data.length) {
    document.getElementById('noResults').classList.add('active');
    return;
  }
  document.getElementById('noResults').classList.remove('active');

  data.forEach(function(r) {
    var card = document.createElement('div');
    card.className = 'card';
    card.setAttribute('data-id', r.id);
    card.setAttribute('data-status', r.status);
    card.setAttribute('data-subsys', (r.subsystem||'').toLowerCase());
    card.setAttribute('data-emails', r.email_count);
    card.setAttribute('data-size', r.primary_size_kb);
    card.setAttribute('data-mtime', r.mtime);
    card.setAttribute('data-subject', (r.subject||'').toLowerCase());
    card.setAttribute('data-search', [r.commit_hash, r.subject, r.author, r.subsystem].join(' ').toLowerCase());

    var hashHtml = r.commit_hash ? '<span class="hash">' + esc(r.commit_hash) + '</span>' : '';
    var subsysHtml = r.subsystem ? '<span class="subsys-tag" style="background:' + subsysColor(r.subsystem) + '">' + esc(r.subsystem) + '</span>' : '';
    var statusCls = 'status-' + r.status;

    // 操作按钮
    var actionsHtml = '';
    if (r.primary_path) {
      actionsHtml += '<a href="' + esc(r.primary_path) + '" target="_blank">&#128196; 打开报告</a>';
    }
    actionsHtml += '<button onclick="showPreview(\'' + esc(r.id) + '\')">&#128270; 预览</button>';
    var arts = r.artifacts || {};
    if (arts.context_txt) {
      actionsHtml += '<a href="' + esc(arts.context_txt.relpath || '') + '" target="_blank">&#128196; 原始数据</a>';
    }

    card.innerHTML =
      '<span class="status-badge ' + statusCls + '">' + r.status_icon + ' ' + esc(r.status) + '</span>' +
      '<div class="card-head">' + hashHtml + '<div class="subject">' + esc(r.subject) + '</div></div>' +
      '<div class="meta-row">' +
        (r.author ? '<span>&#128100; ' + esc(r.author) + '</span>' : '') +
        (r.date ? '<span>&#128197; ' + esc(r.date) + '</span>' : '') +
        subsysHtml +
      '</div>' +
      '<div class="stats-row">' +
        '<span>&#128231; ' + r.email_count + ' 封</span>' +
        '<span>&#128172; ' + r.thread_count + ' 线程</span>' +
        '<span>&#128101; ' + r.participant_count + ' 人</span>' +
        '<span>&#128190; ' + r.primary_size_kb + ' KB</span>' +
      '</div>' +
      '<div class="actions">' + actionsHtml + '</div>';

    grid.appendChild(card);
  });
}

function esc(s) { if (!s) return ''; var d = document.createElement('div'); d.textContent = s; return d.innerHTML; }

function filterCards() {
  var q = (document.getElementById('searchInput').value || '').toLowerCase().trim();
  var st = document.getElementById('statusFilter').value;
  var ss = document.getElementById('subsysFilter').value.toLowerCase();
  var cards = document.querySelectorAll('.card');
  var visible = 0;
  cards.forEach(function(c) {
    var show = true;
    if (q && c.getAttribute('data-search').indexOf(q) < 0) show = false;
    if (st && c.getAttribute('data-status') !== st) show = false;
    if (ss && c.getAttribute('data-subsys').indexOf(ss) < 0) show = false;
    c.style.display = show ? '' : 'none';
    if (show) visible++;
  });
  document.getElementById('noResults').classList.toggle('active', visible === 0 && (q || st || ss));
}

function sortCards() {
  var key = document.getElementById('sortBy').value;
  var grid = document.getElementById('cardGrid');
  var cards = Array.from(grid.children);
  cards.sort(function(a, b) {
    if (key === 'mtime') return (b.getAttribute('data-mtime')||'').localeCompare(a.getAttribute('data-mtime')||'');
    if (key === 'emails') return parseInt(b.getAttribute('data-emails')||0) - parseInt(a.getAttribute('data-emails')||0);
    if (key === 'size') return parseInt(b.getAttribute('data-size')||0) - parseInt(a.getAttribute('data-size')||0);
    if (key === 'subject') return (a.getAttribute('data-subject')||'').localeCompare(b.getAttribute('data-subject')||'');
    return 0;
  });
  cards.forEach(function(c) { grid.appendChild(c); });
}

function showPreview(id) {
  var r = REPORTS.find(function(x) { return x.id === id; });
  if (!r) return;
  document.getElementById('modalTitle').textContent = r.subject || r.id;
  var body = '<div class="preview-item"><b>Commit:</b> ' + esc(r.commit_hash) + '</div>';
  body += '<div class="preview-item"><b>作者:</b> ' + esc(r.author) + '</div>';
  body += '<div class="preview-item"><b>日期:</b> ' + esc(r.date) + '</div>';
  body += '<div class="preview-item"><b>子系统:</b> ' + esc(r.subsystem) + '</div>';
  body += '<div class="preview-item"><b>状态:</b> ' + r.status_icon + ' ' + esc(r.status) + '</div>';
  body += '<div class="preview-item"><b>邮件:</b> ' + r.email_count + ' 封, ' + r.thread_count + ' 线程, ' + r.participant_count + ' 参与者</div>';
  if (r.authors && r.authors.length) {
    body += '<div class="preview-item"><b>参与者:</b> ' + r.authors.map(esc).join(', ') + '</div>';
  }
  body += '<div class="preview-item"><b>文件大小:</b> ' + r.primary_size_kb + ' KB</div>';
  body += '<div class="preview-item"><b>修改时间:</b> ' + esc(r.mtime) + '</div>';
  // 产物列表
  var arts = r.artifacts || {};
  body += '<div class="preview-item"><b>产物文件:</b><br>';
  for (var k in arts) {
    if (arts[k] && arts[k].relpath) {
      body += '&nbsp;&nbsp;' + esc(k) + ': <a href="' + esc(arts[k].relpath) + '" target="_blank">' + esc(arts[k].relpath) + '</a> (' + (arts[k].size_kb||0) + ' KB)<br>';
    }
  }
  body += '</div>';
  document.getElementById('modalBody').innerHTML = body;
  document.getElementById('modalOverlay').classList.add('active');
}

function closeModal(e) {
  if (e && e.target !== document.getElementById('modalOverlay')) return;
  document.getElementById('modalOverlay').classList.remove('active');
}

function toggleTheme() {
  document.body.classList.toggle('light-theme');
  localStorage.setItem('dashboard-theme', document.body.classList.contains('light-theme') ? 'light' : 'dark');
}

// 初始化
(function() {
  if (localStorage.getItem('dashboard-theme') === 'light') document.body.classList.add('light-theme');

  // 统计
  var totalEmails = 0, translated = 0, subsystems = {};
  REPORTS.forEach(function(r) {
    totalEmails += r.email_count || 0;
    if (r.status === 'translated') translated++;
    if (r.subsystem) subsystems[r.subsystem] = 1;
  });
  document.getElementById('statReports').textContent = REPORTS.length;
  document.getElementById('statEmails').textContent = totalEmails;
  document.getElementById('statTranslated').textContent = translated;
  document.getElementById('statSubsystems').textContent = Object.keys(subsystems).length;

  // 子系统筛选选项
  var sel = document.getElementById('subsysFilter');
  Object.keys(subsystems).sort().forEach(function(s) {
    var opt = document.createElement('option');
    opt.value = s.toLowerCase();
    opt.textContent = s;
    sel.appendChild(opt);
  });

  renderCards(REPORTS);
})();
</script>
</body>
</html>"""


# ─── 生成 Dashboard ────────────────────────────────────────────────────────────

def generate_dashboard(output_dir: Path = None, emails_dir: Path = None,
                       out_path: Path = None) -> Path:
    """扫描数据目录并生成 dashboard.html，返回输出路径"""
    output_dir = output_dir or OUTPUT_DIR
    emails_dir = emails_dir or EMAILS_DIR
    out_path = out_path or (output_dir / "dashboard.html")

    print(f"[Dashboard] 扫描 {output_dir} ...")
    output_files = scan_output_dir(output_dir)
    print(f"  产物文件: {len(output_files)}")

    print(f"[Dashboard] 扫描 {emails_dir} ...")
    email_files = scan_emails_dir(emails_dir)
    print(f"  邮件 JSON: {len(email_files)}")

    print(f"[Dashboard] 关联产物...")
    reports = correlate_artifacts(output_files, email_files)
    print(f"  报告数: {len(reports)}")

    # 序列化为 JSON（清理不可序列化的内容）
    reports_clean = []
    for r in reports:
        rc = dict(r)
        # artifacts 中只保留 relpath 和 size_kb
        arts = {}
        for k, v in rc.get("artifacts", {}).items():
            if isinstance(v, dict):
                arts[k] = {"relpath": v.get("relpath", ""), "size_kb": v.get("size_kb", 0)}
            elif isinstance(v, list):
                arts[k] = [{"relpath": x.get("relpath", ""), "size_kb": x.get("size_kb", 0)} for x in v]
        rc["artifacts"] = arts
        reports_clean.append(rc)

    reports_json = json.dumps(reports_clean, ensure_ascii=False, indent=2)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    html = DASHBOARD_TEMPLATE
    html = html.replace("<REPORTS_JSON>", reports_json)
    html = html.replace("<GENERATED_TIME>", now_str)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")
    print(f"[Dashboard] 已生成: {out_path}  ({len(html) // 1024} KB)")
    return out_path


# ─── CLI 入口 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="生成 Kernel Email Dashboard")
    parser.add_argument("-o", "--output", help="输出路径（默认 data/output/dashboard.html）")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="产物目录")
    parser.add_argument("--emails-dir", default=str(EMAILS_DIR), help="邮件 JSON 目录")
    args = parser.parse_args()

    out = Path(args.output) if args.output else None
    generate_dashboard(
        output_dir=Path(args.output_dir),
        emails_dir=Path(args.emails_dir),
        out_path=out,
    )


if __name__ == "__main__":
    main()