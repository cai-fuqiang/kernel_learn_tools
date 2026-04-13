#!/usr/bin/env python3
"""测试 HTML 交互增强功能：100封邮件 + 多线程翻译

验证内容：
  - 搜索/过滤（按作者、关键词、优先级）
  - 侧边栏目录/大纲
  - 键盘快捷键 j/k/Enter
  - 暗色/亮色主题切换
  - 多线程并发翻译
"""
import sys
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
import threading

sys.path.insert(0, str(Path(__file__).parent))
from translate_context import (
    parse_commit_section, parse_diff_section, parse_emails,
    parse_analysis_checklist, should_translate, translate_body,
    generate_html, _translate_diff_comments, _split_body_and_diff,
    CachedTranslator,
)
from email_translator.translator import create_translator
from email_translator.translation_cache import TranslationCache

# ── 配置 ──
MAX_EMAILS = int(sys.argv[1]) if len(sys.argv) > 1 else 100
WORKERS = int(sys.argv[2]) if len(sys.argv) > 2 else 4
BACKEND = "google"
INPUT = Path("doc/example_output/v2/d07f09a1f99c_context_full.txt")

print(f"═══ HTML 交互增强测试 ═══")
print(f"  邮件数: {MAX_EMAILS}, 线程数: {WORKERS}, 后端: {BACKEND}")
print(f"  输入: {INPUT}")

text = INPUT.read_text(encoding="utf-8")
print(f"  大小: {len(text) // 1024} KB\n")

# ── 1. 解析 ──
print("[1/4] 解析文档结构...")
commit = parse_commit_section(text)
diff = parse_diff_section(text)
email_header, emails = parse_emails(text)
checklist = parse_analysis_checklist(text)
total_available = len(emails)
if MAX_EMAILS < len(emails):
    emails = emails[:MAX_EMAILS]
print(f"  Commit: {commit.get('subject', '?')}")
print(f"  Diff: {len(diff)} 字符")
print(f"  邮件: {len(emails)} 封 (共 {total_available} 封可用)")

# ── 2. 多线程翻译 ──
print(f"\n[2/4] 多线程翻译 ({WORKERS} 线程)...")
translator = create_translator(BACKEND)

# 启用缓存
cache = TranslationCache()
translator = CachedTranslator(translator, cache, BACKEND)
cache_size = cache.size()
if cache_size > 0:
    print(f"  翻译缓存: {cache_size} 条已缓存")

translated = {}

# 翻译 commit message
cm = commit.get("commit_message", "")
if cm and should_translate(cm):
    print("  翻译 commit message...")
    result = translator.translate_email({"subject": "", "body": cm})
    translated["__commit_message__"] = result.get("body_cn", "")

# 收集翻译任务
tasks = []
skipped = 0
for i, em in enumerate(emails):
    body = em.get("body", "")
    if not should_translate(body):
        skipped += 1
        continue
    tasks.append((i, em))

print(f"  需翻译: {len(tasks)} 封, 跳过: {skipped} 封")

t0 = time.time()
done = 0
lock = threading.Lock()

def _translate_one(idx_em):
    i, em = idx_em
    body = em.get("body", "")
    result = translate_body(translator, body)
    return i, em, result

with ThreadPoolExecutor(max_workers=WORKERS) as pool:
    futures = {pool.submit(_translate_one, t): t for t in tasks}
    for future in as_completed(futures):
        i, em, result = future.result()
        tag = em.get("tag", "")
        translated[f"email_{i}"] = result
        done += 1
        with lock:
            elapsed = time.time() - t0
            rate = done / elapsed if elapsed > 0 else 0
            print(f"  [{done}/{len(tasks)}] ({rate:.1f}/s) {tag} {em.get('subject', '')[:50]}")

t_translate = time.time() - t0
print(f"\n  翻译完成: {done} 封, 耗时 {t_translate:.1f}s")
stats = cache.stats()
if stats.get("total", 0) > 0:
    print(f"  缓存统计: 命中 {stats['hits']}, 未命中 {stats['misses']}, 命中率 {stats['hit_rate']}")

# ── 3. 翻译 diff 注释 ──
print(f"\n[3/4] 翻译 diff 注释 ({WORKERS} 线程)...")
t1 = time.time()
if diff:
    translated["__diff__"] = _translate_diff_comments(translator, diff)

diff_tasks = []
for i, em in enumerate(emails):
    _, em_diff = _split_body_and_diff(em.get("body", ""))
    if em_diff:
        diff_tasks.append((i, em_diff))

if diff_tasks:
    def _translate_diff_one(idx_diff):
        i, d = idx_diff
        return i, _translate_diff_comments(translator, d)

    with ThreadPoolExecutor(max_workers=WORKERS) as pool:
        futures = [pool.submit(_translate_diff_one, t) for t in diff_tasks]
        for future in as_completed(futures):
            i, result = future.result()
            translated[f"diff_{i}"] = result

t_diff = time.time() - t1
print(f"  diff 注释翻译完成: {len(diff_tasks) + (1 if diff else 0)} 个, 耗时 {t_diff:.1f}s")

# ── 4. 生成 HTML ──
print(f"\n[4/4] 生成 HTML...")
t2 = time.time()
output = generate_html(commit, diff, email_header, emails, checklist, translated)
t_render = time.time() - t2

out_path = Path(f"data/output/test_interactive_{MAX_EMAILS}.html")
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(output, encoding="utf-8")

# ── 验证 HTML 中包含新功能 ──
print(f"\n═══ 功能验证 ═══")
checks = {
    "工具栏 (toolbar)": 'class="toolbar"' in output,
    "搜索框 (search)": 'id="searchInput"' in output,
    "作者筛选 (authorFilter)": 'id="authorFilter"' in output,
    "类型筛选 (priorityFilter)": 'id="priorityFilter"' in output,
    "侧边栏 (sidebar)": 'id="sidebar"' in output,
    "目录列表 (tocList)": 'id="tocList"' in output,
    "主题切换 (themeToggle)": 'id="themeToggle"' in output,
    "亮色主题 CSS (light-theme)": "light-theme" in output,
    "键盘导航 JS (kbNavigate)": "kbNavigate" in output,
    "快捷键帮助 (kbd-hint)": 'id="kbdHint"' in output,
    "聚焦视图 (focusEmail)": "focusEmail" in output,
    "data-tag 属性": "data-tag=" in output,
    "data-body-preview 属性": "data-body-preview=" in output,
}
all_pass = True
for name, ok in checks.items():
    status = "✅" if ok else "❌"
    print(f"  {status} {name}")
    if not ok:
        all_pass = False

# 统计
email_nodes = output.count('class="email-node"')
thread_titles = output.count('class="thread-title"')
print(f"\n═══ 统计 ═══")
print(f"  邮件节点: {email_nodes}")
print(f"  线程数: {thread_titles}")
print(f"  文件大小: {len(output) // 1024} KB | {len(output.splitlines())} 行")
print(f"  翻译耗时: {t_translate:.1f}s")
print(f"  Diff注释: {t_diff:.1f}s")
print(f"  渲染耗时: {t_render:.2f}s")
print(f"  总耗时: {time.time() - t0:.1f}s")
print(f"\n  输出: {out_path}")

if all_pass:
    print(f"\n🎉 所有功能验证通过! 请在浏览器中打开 HTML 检查交互效果。")
else:
    print(f"\n⚠️  部分功能验证未通过，请检查!")
    sys.exit(1)