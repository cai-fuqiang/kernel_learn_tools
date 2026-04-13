#!/usr/bin/env python3
"""batch_process.py — 批量处理知识库邮件：摘要生成 + 反哺知识库 + 跨线程综合分析

功能：
  1. 遍历知识库中未处理的线程，AI 生成结构化摘要
  2. 摘要（key_points/consensus/tags 等）写回知识库
  3. 跨线程综合分析：时间线、核心矛盾、主题索引
  4. 可选：全文搜索查询知识库

用法:
    # 生成单线程摘要并反哺
    python batch_process.py \
      --summarize --api-key sk-xxx --api-provider deepseek

    # 跨线程综合分析
    python batch_process.py \
      --cross-analysis --topic "scheduler latency QoS 2017-2018" \
      --api-key sk-xxx

    # 查询知识库
    python batch_process.py --query "SCHED_DEADLINE 设计决策"

    # 查看知识库统计
    python batch_process.py --stats
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from email_translator.knowledge_db import KnowledgeDB

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ======================================================================
# 单线程摘要
# ======================================================================

_SUMMARY_PROMPT = """你是一个 Linux 内核专家。请对以下内核邮件讨论线程生成**结构化中文摘要**。

要求以 JSON 格式输出，包含以下字段：
{{
  "summary": "200字以内的中文摘要",
  "key_points": ["要点1", "要点2", ...],
  "consensus": "讨论达成的共识或结论（无则留空）",
  "design_decisions": ["设计决策1", ...],
  "related_files": ["涉及的源码文件路径"],
  "related_functions": ["涉及的函数名"],
  "tags": ["标签1", "标签2", ...]
}}

只输出 JSON，不要其他内容。

--- 邮件线程 ---
主题: {subject}
参与者: {participants}
时间范围: {date_range}
邮件数: {email_count}

{emails_text}
"""


def build_thread_context(thread: Dict, emails: List[Dict]) -> str:
    """构建线程上下文文本。"""
    parts = []
    for i, em in enumerate(emails[:15], 1):  # 最多取前15封
        sender = em.get("from_email", "") or em.get("from_name", "")
        date = em.get("date", "")
        subject = em.get("subject", "")
        body = em.get("body", "")[:1000]
        parts.append(f"[邮件 {i}] {sender} ({date})\n主题: {subject}\n{body}\n")
    if len(emails) > 15:
        parts.append(f"... 还有 {len(emails) - 15} 封邮件省略\n")
    return "\n".join(parts)


def summarize_thread(thread: Dict, emails: List[Dict], api_caller) -> Dict:
    """用 AI 对单个线程生成结构化摘要。"""
    emails_text = build_thread_context(thread, emails)

    # 提取参与者
    participants = set()
    for em in emails:
        name = em.get("from_name", "") or em.get("from_email", "")
        if name:
            participants.add(name)

    prompt = _SUMMARY_PROMPT.format(
        subject=thread.get("subject", ""),
        participants=", ".join(list(participants)[:10]),
        date_range=f"{thread.get('start_date', '')} ~ {thread.get('end_date', '')}",
        email_count=thread.get("email_count", len(emails)),
        emails_text=emails_text,
    )

    text, error = api_caller._call(prompt)
    if error:
        logger.warning("AI 摘要失败: %s (thread=%s)", error, thread.get("id", ""))
        return {"summary": f"生成失败: {error}", "key_points": [], "tags": []}

    # 解析 JSON
    text = text.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        logger.warning("AI 返回非 JSON，使用原始文本")
        return {"summary": text[:500], "key_points": [], "tags": []}


# ======================================================================
# 跨线程综合分析
# ======================================================================

_CROSS_ANALYSIS_PROMPT = """你是一个 Linux 内核专家。基于以下 {count} 个邮件线程的摘要，进行**跨线程综合分析**。

请用中文输出，包含：
1. **时间线梳理**：这些讨论的演进脉络
2. **核心矛盾和争议**：哪些设计决策有分歧
3. **关键共识**：社区达成了哪些一致意见
4. **主题索引**：按技术子话题分组归类
5. **重要人物**：哪些开发者在这个领域最活跃
6. **代码影响**：涉及的主要源码文件和函数

--- 线程摘要列表 ---
{summaries_text}
"""


def cross_analysis(threads_with_summary: List[Dict], topic: str,
                   api_caller) -> str:
    """对一批已有摘要的线程做跨线程综合分析。"""
    parts = []
    for i, t in enumerate(threads_with_summary, 1):
        summary = t.get("summary_zh", "") or "(无摘要)"
        tags = t.get("tags", "")
        parts.append(
            f"[{i}] {t.get('subject', '')}\n"
            f"    时间: {t.get('start_date', '')} ~ {t.get('end_date', '')}\n"
            f"    邮件数: {t.get('email_count', 0)}\n"
            f"    标签: {tags}\n"
            f"    摘要: {summary}\n"
        )

    prompt = _CROSS_ANALYSIS_PROMPT.format(
        count=len(threads_with_summary),
        summaries_text="\n".join(parts),
    )

    text, error = api_caller._call(prompt)
    if error:
        return f"综合分析失败: {error}"
    return text.strip()


# ======================================================================
# 查询
# ======================================================================

def query_knowledge(db: KnowledgeDB, query: str):
    """全文搜索知识库。"""
    results = db.search_fts(query, limit=20)
    if not results:
        print(f"未找到与 \"{query}\" 相关的结果。")
        return

    print(f"\n找到 {len(results)} 条结果：\n")
    for i, r in enumerate(results, 1):
        print(f"  [{i}] {r.get('subject', '')}")
        print(f"      From: {r.get('from_email', '')}  Date: {r.get('date', '')}")
        body_preview = (r.get("body", "") or "")[:150].replace("\n", " ")
        print(f"      {body_preview}...")
        print()


# ======================================================================
# 主流程
# ======================================================================

def run_summarize(args, db: KnowledgeDB):
    """批量生成单线程摘要并反哺知识库。"""
    from email_translator.translator import APITranslator
    api_caller = APITranslator(
        api_key=args.api_key,
        provider=args.api_provider,
        model=args.model or "",
        timeout=120,
    )

    threads = db.get_unprocessed_threads(limit=args.batch_size)
    if not threads:
        logger.info("没有未处理的线程。")
        return

    logger.info("开始处理 %d 个线程的摘要...", len(threads))

    for i, thread in enumerate(threads, 1):
        tid = thread["id"]
        logger.info("  [%d/%d] %s", i, len(threads), thread.get("subject", "")[:60])

        # 获取线程下的所有邮件
        emails = db.get_thread_emails(tid)
        if not emails:
            logger.warning("    线程无邮件，跳过")
            continue

        # AI 生成摘要
        summary = summarize_thread(thread, emails, api_caller)

        # 写回知识库
        db.update_thread_summary(tid, summary)

        logger.info("    摘要: %s", (summary.get("summary", ""))[:80])
        logger.info("    标签: %s", summary.get("tags", []))

        time.sleep(0.5)  # 限速

    logger.info("摘要生成完成！")


def run_cross_analysis(args, db: KnowledgeDB):
    """跨线程综合分析。"""
    from email_translator.translator import APITranslator
    api_caller = APITranslator(
        api_key=args.api_key,
        provider=args.api_provider,
        model=args.model or "",
        timeout=180,
    )

    # 获取所有已处理的线程
    rows = db.conn.execute(
        "SELECT * FROM threads WHERE processed = 1 ORDER BY start_date"
    ).fetchall()
    threads = [dict(r) for r in rows]

    if not threads:
        logger.info("没有已处理的线程，请先运行 --summarize。")
        return

    topic = args.topic or "kernel scheduler"
    logger.info("开始跨线程综合分析: %d 个线程, topic=%s", len(threads), topic)

    # 分批处理（每批最多 50 个线程，避免 token 超限）
    batch_size = 50
    all_reports = []

    for start in range(0, len(threads), batch_size):
        batch = threads[start:start + batch_size]
        logger.info("  分析第 %d-%d 个线程...", start + 1, start + len(batch))

        report = cross_analysis(batch, topic, api_caller)
        all_reports.append(report)

        # 存入知识库
        source_ids = [t["id"] for t in batch]
        db.insert_report(
            topic=topic,
            content=report,
            source_thread_ids=source_ids,
            report_type="cross_analysis",
        )

        logger.info("  报告已存入知识库 (%d 字符)", len(report))
        time.sleep(1)

    # 如果有多批，再做一次汇总
    if len(all_reports) > 1:
        logger.info("  汇总 %d 个分批报告...", len(all_reports))
        final_prompt = (
            f"请将以下 {len(all_reports)} 份分析报告合并为一份完整的综合报告，"
            f"去重并按时间线组织：\n\n" +
            "\n\n---\n\n".join(all_reports)
        )
        final_text, error = api_caller._call(final_prompt)
        if not error:
            db.insert_report(
                topic=topic,
                content=final_text,
                source_thread_ids=[t["id"] for t in threads],
                report_type="final_summary",
            )
            logger.info("  最终综合报告已存入知识库 (%d 字符)", len(final_text))

    logger.info("跨线程综合分析完成！")


# ======================================================================
# HTML 导出
# ======================================================================

_KB_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{title}</title>
<style>
:root {{
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --text-muted: #8b949e; --accent: #58a6ff;
  --green: #3fb950; --orange: #d29922; --red: #f85149;
}}
body.light-theme {{
  --bg: #ffffff; --surface: #f6f8fa; --border: #d0d7de;
  --text: #1f2328; --text-muted: #656d76; --accent: #0969da;
  --green: #1a7f37; --orange: #bf8700; --red: #cf222e;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text);
  line-height: 1.6; max-width: 1200px; margin: 0 auto; padding: 24px;
  transition: background 0.3s, color 0.3s;
}}
.toolbar {{
  position: sticky; top: 0; z-index: 90;
  background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 10px 16px; margin: -24px -24px 20px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  box-shadow: 0 1px 4px rgba(0,0,0,0.2);
}}
.toolbar input {{
  flex: 1; min-width: 200px; max-width: 500px; padding: 7px 12px;
  font-size: 14px; border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text); outline: none;
}}
.toolbar input:focus {{ border-color: var(--accent); }}
.toolbar select {{
  padding: 6px 10px; font-size: 13px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--bg); color: var(--text); cursor: pointer;
}}
.toolbar .btn {{
  background: none; border: 1px solid var(--border); border-radius: 6px;
  padding: 6px 12px; font-size: 13px; color: var(--text-muted); cursor: pointer;
}}
.toolbar .btn:hover {{ background: var(--border); color: var(--text); }}

.stats-bar {{
  display: flex; gap: 20px; flex-wrap: wrap;
  padding: 12px 16px; margin-bottom: 20px;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
}}
.stats-bar .stat {{
  text-align: center;
}}
.stats-bar .stat .num {{
  font-size: 24px; font-weight: 700; color: var(--accent);
}}
.stats-bar .stat .label {{
  font-size: 12px; color: var(--text-muted);
}}

h2 {{
  font-size: 18px; margin: 24px 0 12px; padding-bottom: 8px;
  border-bottom: 1px solid var(--border); color: var(--accent);
}}

.report-section {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 16px 20px; margin-bottom: 20px;
  white-space: pre-wrap; font-size: 14px; line-height: 1.7;
}}
.report-section summary {{
  cursor: pointer; font-weight: 600; font-size: 15px;
  padding: 4px 0; color: var(--accent);
}}

.thread-card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 14px 18px; margin-bottom: 12px;
  transition: border-color 0.2s;
}}
.thread-card:hover {{ border-color: var(--accent); }}
.thread-card .title {{
  font-size: 15px; font-weight: 600; margin-bottom: 6px;
  color: var(--text);
}}
.thread-card .meta {{
  font-size: 12px; color: var(--text-muted); margin-bottom: 8px;
  display: flex; gap: 16px; flex-wrap: wrap;
}}
.thread-card .summary-text {{
  font-size: 13px; color: var(--text); margin-bottom: 8px;
  line-height: 1.6;
}}
.thread-card .key-points {{
  font-size: 13px; color: var(--text-muted); margin-bottom: 8px;
}}
.thread-card .key-points li {{ margin-left: 18px; margin-bottom: 2px; }}
.tag {{
  display: inline-block; padding: 2px 8px; margin: 2px 3px 2px 0;
  font-size: 11px; border-radius: 12px;
  background: color-mix(in srgb, var(--accent) 15%, transparent);
  color: var(--accent); border: 1px solid color-mix(in srgb, var(--accent) 30%, transparent);
}}
.consensus {{
  font-size: 13px; padding: 8px 12px; margin-top: 6px;
  background: color-mix(in srgb, var(--green) 10%, transparent);
  border-left: 3px solid var(--green); border-radius: 4px;
  color: var(--text);
}}

.empty-state {{
  text-align: center; padding: 60px 20px;
  color: var(--text-muted); font-size: 16px;
}}

.hidden {{ display: none !important; }}
</style>
</head>
<body>

<!-- 工具栏 -->
<div class="toolbar">
  <strong style="font-size:15px;">&#128218; 内核邮件知识库</strong>
  <input type="text" id="searchBox" placeholder="搜索线程、摘要、标签..." oninput="filterCards()">
  <select id="tagFilter" onchange="filterCards()">
    <option value="">所有标签</option>
    {tag_options}
  </select>
  <select id="sortSelect" onchange="sortCards()">
    <option value="date-desc">时间 新→旧</option>
    <option value="date-asc">时间 旧→新</option>
    <option value="emails-desc">邮件数 多→少</option>
  </select>
  <button class="btn" onclick="toggleTheme()">&#127763; 切换主题</button>
</div>

<!-- 统计 -->
<div class="stats-bar">
  <div class="stat"><div class="num">{total_emails}</div><div class="label">邮件</div></div>
  <div class="stat"><div class="num">{total_threads}</div><div class="label">线程</div></div>
  <div class="stat"><div class="num">{processed_threads}</div><div class="label">已摘要</div></div>
  <div class="stat"><div class="num">{total_reports}</div><div class="label">综合报告</div></div>
</div>

<!-- 综合报告 -->
{reports_html}

<!-- 线程列表 -->
<h2 id="threads-heading">邮件线程 ({total_threads})</h2>
<div id="threadList">
{threads_html}
</div>

<div class="empty-state hidden" id="emptyState">没有匹配的线程</div>

<script>
// 主题切换
function toggleTheme() {{
  document.body.classList.toggle('light-theme');
  localStorage.setItem('kb-theme', document.body.classList.contains('light-theme') ? 'light' : 'dark');
}}
if (localStorage.getItem('kb-theme') === 'light') document.body.classList.add('light-theme');

// 搜索和过滤
function filterCards() {{
  const q = document.getElementById('searchBox').value.toLowerCase();
  const tag = document.getElementById('tagFilter').value.toLowerCase();
  const cards = document.querySelectorAll('.thread-card');
  let visible = 0;
  cards.forEach(c => {{
    const text = c.getAttribute('data-search').toLowerCase();
    const tags = c.getAttribute('data-tags').toLowerCase();
    const matchQ = !q || text.includes(q);
    const matchTag = !tag || tags.includes(tag);
    c.classList.toggle('hidden', !(matchQ && matchTag));
    if (matchQ && matchTag) visible++;
  }});
  document.getElementById('emptyState').classList.toggle('hidden', visible > 0);
}}

// 排序
function sortCards() {{
  const mode = document.getElementById('sortSelect').value;
  const list = document.getElementById('threadList');
  const cards = Array.from(list.querySelectorAll('.thread-card'));
  cards.sort((a, b) => {{
    if (mode === 'date-desc') return b.getAttribute('data-date').localeCompare(a.getAttribute('data-date'));
    if (mode === 'date-asc') return a.getAttribute('data-date').localeCompare(b.getAttribute('data-date'));
    if (mode === 'emails-desc') return parseInt(b.getAttribute('data-count')) - parseInt(a.getAttribute('data-count'));
    return 0;
  }});
  cards.forEach(c => list.appendChild(c));
}}
</script>
</body>
</html>"""


def _escape(s):
    """HTML 转义。"""
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def _parse_json_field(val):
    """安全解析 JSON 字段。"""
    if not val:
        return []
    if isinstance(val, list):
        return val
    try:
        parsed = json.loads(val)
        return parsed if isinstance(parsed, list) else [str(parsed)]
    except (json.JSONDecodeError, TypeError):
        return [str(val)] if val else []


def export_html(db: KnowledgeDB, output_path: str = None):
    """将知识库导出为单文件自包含 HTML。"""
    from email_translator.config import OUTPUT_DIR

    if output_path is None:
        output_path = str(OUTPUT_DIR / "knowledge_base.html")

    stats = db.stats()

    # 获取所有线程
    threads = [dict(r) for r in db.conn.execute(
        "SELECT * FROM threads ORDER BY start_date DESC"
    ).fetchall()]

    # 获取综合报告
    reports = [dict(r) for r in db.conn.execute(
        "SELECT * FROM knowledge_reports ORDER BY created_at DESC"
    ).fetchall()]

    # 收集所有标签
    all_tags = set()
    for t in threads:
        for tag in _parse_json_field(t.get("tags", "")):
            tag = tag.strip()
            if tag:
                all_tags.add(tag)
    tag_options = "\n".join(
        f'    <option value="{_escape(t)}">{_escape(t)}</option>' for t in sorted(all_tags)
    )

    # 生成综合报告 HTML
    reports_html_parts = []
    if reports:
        reports_html_parts.append('<h2>综合分析报告</h2>')
        for r in reports:
            rtype = _escape(r.get("report_type", ""))
            topic = _escape(r.get("topic", ""))
            content = _escape(r.get("content", ""))
            reports_html_parts.append(
                f'<details class="report-section" open>\n'
                f'  <summary>[{rtype}] {topic}</summary>\n'
                f'  <div style="margin-top:10px;">{content}</div>\n'
                f'</details>'
            )
    reports_html = "\n".join(reports_html_parts)

    # 生成线程卡片 HTML
    threads_html_parts = []
    for t in threads:
        subject = _escape(t.get("subject", "(无主题)"))
        start = t.get("start_date", "")
        end = t.get("end_date", "")
        count = t.get("email_count", 0)
        participants = t.get("participant_count", 0)
        summary = _escape(t.get("summary_zh", ""))
        consensus = _escape(t.get("consensus", ""))
        tags = _parse_json_field(t.get("tags", ""))
        key_points = _parse_json_field(t.get("key_points", ""))
        design_decisions = _parse_json_field(t.get("design_decisions", ""))

        tags_str = " ".join(tags)
        search_text = f"{subject} {summary} {consensus} {tags_str} {' '.join(key_points)}"

        tags_html = "".join(f'<span class="tag">{_escape(tg)}</span>' for tg in tags if tg)

        kp_html = ""
        if key_points:
            items = "".join(f"<li>{_escape(kp)}</li>" for kp in key_points if kp)
            if items:
                kp_html = f'<div class="key-points"><strong>关键要点:</strong><ul>{items}</ul></div>'

        dd_html = ""
        if design_decisions:
            items = "".join(f"<li>{_escape(dd)}</li>" for dd in design_decisions if dd)
            if items:
                dd_html = f'<div class="key-points"><strong>设计决策:</strong><ul>{items}</ul></div>'

        consensus_html = ""
        if consensus:
            consensus_html = f'<div class="consensus"><strong>共识:</strong> {consensus}</div>'

        summary_html = ""
        if summary:
            summary_html = f'<div class="summary-text">{summary}</div>'

        date_display = start[:10] if start else ""
        if end and end != start:
            date_display += f" ~ {end[:10]}"

        threads_html_parts.append(
            f'<div class="thread-card" data-search="{_escape(search_text)}" '
            f'data-tags="{_escape(tags_str)}" data-date="{_escape(start)}" data-count="{count}">\n'
            f'  <div class="title">{subject}</div>\n'
            f'  <div class="meta">\n'
            f'    <span>&#128197; {date_display}</span>\n'
            f'    <span>&#9993; {count} 封邮件</span>\n'
            f'    <span>&#128101; {participants} 人参与</span>\n'
            f'  </div>\n'
            f'  {summary_html}\n'
            f'  {kp_html}\n'
            f'  {dd_html}\n'
            f'  {consensus_html}\n'
            f'  <div style="margin-top:8px;">{tags_html}</div>\n'
            f'</div>'
        )

    threads_html = "\n".join(threads_html_parts) if threads_html_parts else (
        '<div class="empty-state">知识库中暂无线程数据。请先运行 batch_collect.py 采集邮件。</div>'
    )

    # 渲染模板
    html = _KB_HTML_TEMPLATE.format(
        title="内核邮件知识库",
        tag_options=tag_options,
        total_emails=stats["emails"],
        total_threads=stats["threads"],
        processed_threads=stats["processed_threads"],
        total_reports=stats["reports"],
        reports_html=reports_html,
        threads_html=threads_html,
    )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    Path(output_path).write_text(html, encoding="utf-8")
    logger.info("知识库 HTML 已导出: %s (%d 字符)", output_path, len(html))
    return output_path


def show_stats(db: KnowledgeDB):
    """显示知识库统计。"""
    s = db.stats()
    print("\n" + "="*50)
    print("  内核邮件知识库统计")
    print("="*50)
    print(f"  邮件总数:       {s['emails']}")
    print(f"  线程总数:       {s['threads']}")
    print(f"  已处理线程:     {s['processed_threads']}")
    print(f"  综合报告:       {s['reports']}")
    print("="*50 + "\n")

    # 显示最近的线程
    rows = db.conn.execute(
        "SELECT subject, email_count, tags, summary_zh FROM threads "
        "WHERE processed = 1 ORDER BY start_date DESC LIMIT 10"
    ).fetchall()
    if rows:
        print("  最近处理的线程:")
        for r in rows:
            summary = (r["summary_zh"] or "")[:60]
            print(f"    - {r['subject'][:50]}  [{r['email_count']}封]")
            if summary:
                print(f"      摘要: {summary}...")
            if r["tags"]:
                print(f"      标签: {r['tags']}")
        print()

    # 显示综合报告
    rows = db.conn.execute(
        "SELECT topic, report_type, length(content) as size, created_at "
        "FROM knowledge_reports ORDER BY created_at DESC LIMIT 5"
    ).fetchall()
    if rows:
        print("  综合报告:")
        for r in rows:
            print(f"    - [{r['report_type']}] {r['topic']} ({r['size']} 字符)")
        print()


def main():
    parser = argparse.ArgumentParser(
        description="批量处理知识库邮件：摘要生成 + 反哺 + 跨线程综合分析"
    )

    # 操作模式
    parser.add_argument("--summarize", action="store_true",
                        help="对未处理的线程生成 AI 摘要并反哺知识库")
    parser.add_argument("--cross-analysis", action="store_true",
                        help="对已处理的线程做跨线程综合分析")
    parser.add_argument("--query", default="",
                        help="全文搜索知识库")
    parser.add_argument("--stats", action="store_true",
                        help="显示知识库统计")
    parser.add_argument("--export-html", action="store_true",
                        help="导出知识库为静态 HTML 页面")
    parser.add_argument("--output", default="",
                        help="HTML 导出路径 (默认 data/output/knowledge_base.html)")

    # AI 参数
    parser.add_argument("--api-key", default="", help="API 密钥")
    parser.add_argument("--api-provider", default="deepseek",
                        help="API 服务商 (默认 deepseek)")
    parser.add_argument("--model", default="", help="模型名 (留空用默认)")

    # 处理参数
    parser.add_argument("--topic", default="",
                        help="综合分析的主题描述 (--cross-analysis 时使用)")
    parser.add_argument("--batch-size", type=int, default=50,
                        help="每批处理的线程数 (默认 50)")

    args = parser.parse_args()

    db = KnowledgeDB()

    # 读取 config.json 补充 API 配置
    if not args.api_key:
        config_path = Path(__file__).parent / "config.json"
        if config_path.exists():
            cfg = json.loads(config_path.read_text())
            args.api_key = cfg.get("api_key", "")
            args.api_provider = cfg.get("api_provider", args.api_provider)
            args.model = args.model or cfg.get("model", "")

    if args.stats:
        show_stats(db)
    elif args.export_html:
        out = export_html(db, args.output or None)
        print(f"知识库 HTML 已导出: {out}")
    elif args.query:
        query_knowledge(db, args.query)
    elif args.summarize:
        if not args.api_key:
            logger.error("--summarize 需要 --api-key 或 config.json 中的 api_key")
            sys.exit(1)
        run_summarize(args, db)
    elif args.cross_analysis:
        if not args.api_key:
            logger.error("--cross-analysis 需要 --api-key 或 config.json 中的 api_key")
            sys.exit(1)
        run_cross_analysis(args, db)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()