#!/usr/bin/env python3
"""诊断 HTML 输出的已知问题"""
import re

text = open('data/output/d07f09a1f99c_context_full.txt').read()
html = open('data/output/d07f09a1f99c_translated.html').read()

# 1. 乱码: Unicode 替换字符
repl_count = text.count('\ufffd')
print(f"=== 1. 乱码问题 ===")
print(f"  context_full 中 U+FFFD 替换字符数: {repl_count}")

# 2. PATCH 代码: 检查 PATCH 摘要邮件的 body 是否包含 diff 代码
m = re.findall(r'^## \[([^\]]+)\]\s*(.+)$', text, re.MULTILINE)
patch_emails = [(tag, subj) for tag, subj in m if 'PATCH摘要' in tag and not subj.startswith('Re:')]
print(f"\n=== 2. PATCH 代码展示 ===")
print(f"  PATCH摘要邮件(非Re:): {len(patch_emails)} 封")

# 检查几封 PATCH 邮件的 body 中是否有 diff 内容
blocks = re.split(r'\n(?=## \[)', text)
for block in blocks[:]:
    hm = re.match(r'## \[PATCH摘要\]\s*(\[(?:RFC\]\[)?PATCH \d+/15\].*)', block)
    if hm and not hm.group(1).startswith('Re:'):
        subj = hm.group(1)[:60]
        has_diff = '---\n' in block or 'diff --git' in block or '+++ ' in block
        body_len = len(block)
        print(f"  {subj}  body={body_len}  has_diff={has_diff}")

# 3. 线程排序: HTML 中线程标题的顺序
print(f"\n=== 3. 线程排序（HTML中出现顺序）===")
thread_titles = re.findall(r'class="thread-title">(.+?)<', html)
for i, t in enumerate(thread_titles):
    # 去掉 HTML 实体
    t = t.replace('&', '&').replace('<', '<').replace('>', '>')
    t = re.sub(r'<[^>]+>', '', t).strip()
    print(f"  [{i+1}] {t[:70]}")

# 4. 游离邮件: 检查 "fix pick_eevdf" 相关邮件
print(f"\n=== 4. 游离邮件分析 ===")
subjects = [(tag, subj) for tag, subj in m]
fix_pick = [(t, s) for t, s in subjects if 'fix pick_eevdf' in s]
print(f"  含 'fix pick_eevdf' 的邮件: {len(fix_pick)} 封")
for t, s in fix_pick:
    depth = len(re.findall(r'Re:', s, re.IGNORECASE))
    base = re.sub(r'^(Re:\s*)+', '', s, flags=re.IGNORECASE).strip()
    print(f"    [{t}] depth={depth} base='{base[:60]}'")

# 检查 base_subject 分组
bases = set()
for t, s in fix_pick:
    base = re.sub(r'^(Re:\s*)+', '', s, flags=re.IGNORECASE).strip()
    bases.add(base)
print(f"  分组为 {len(bases)} 个不同的 base_subject:")
for b in sorted(bases):
    count = sum(1 for t, s in fix_pick if re.sub(r'^(Re:\s*)+', '', s, flags=re.IGNORECASE).strip() == b)
    print(f"    [{count}封] '{b[:70]}'")