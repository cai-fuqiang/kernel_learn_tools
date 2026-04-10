#!/usr/bin/env python3
"""测试翻译段落对齐 + diff注释翻译：支持控制测试邮件数量"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from translate_context import (
    parse_commit_section, parse_diff_section, parse_emails,
    parse_analysis_checklist, should_translate, translate_body,
    generate_html, _build_thread_tree, _split_paragraphs,
    _is_untranslatable, _render_bilingual_body,
    _translate_diff_comments, _split_body_and_diff,
)
from email_translator.translator import create_translator

# 命令行参数：测试邮件数量，默认20
MAX_EMAILS = int(sys.argv[1]) if len(sys.argv) > 1 else 20

INPUT = Path("doc/example_output/v2/d07f09a1f99c_context_full.txt")

print(f"读取: {INPUT}")
text = INPUT.read_text(encoding="utf-8")
print(f"  大小: {len(text) // 1024} KB")

# 解析
print("\n[1/4] 解析文档结构...")
commit = parse_commit_section(text)
diff = parse_diff_section(text)
email_header, emails = parse_emails(text)
checklist = parse_analysis_checklist(text)
print(f"  Commit: {commit.get('subject', '?')}")
print(f"  Diff: {len(diff)} 字符")
if MAX_EMAILS < len(emails):
    emails = emails[:MAX_EMAILS]
print(f"  邮件: {len(emails)} 封 (测试前 {MAX_EMAILS} 封)")

# 翻译
total_emails = len(emails)
print(f"\n[2/4] 翻译全部 {total_emails} 封邮件 (backend: google)...")
translator = create_translator("google")

translated = {}
cm = commit.get("commit_message", "")
if cm and should_translate(cm):
    print("  翻译 commit message...")
    result = translator.translate_email({"subject": "", "body": cm})
    translated["__commit_message__"] = result.get("body_cn", "")

done, skipped = 0, 0
for i, em in enumerate(emails):
    tag = em.get("tag", "")
    body = em.get("body", "")
    if not should_translate(body):
        skipped += 1
        continue
    print(f"  [{i+1}/{len(emails)}] {tag} {em.get('subject', '')[:50]}")
    translated[f"email_{i}"] = translate_body(translator, body)
    done += 1
    if done % 5 == 0:
        time.sleep(1)

print(f"  翻译完成: {done} 封, 跳过: {skipped} 封")

# 翻译 diff 注释
print(f"\n[2.5/4] 翻译 diff 注释...")
if diff:
    translated["__diff__"] = _translate_diff_comments(translator, diff)
    print(f"  全局 diff 注释翻译完成")
diff_done = 0
for i, em in enumerate(emails):
    _, em_diff = _split_body_and_diff(em.get("body", ""))
    if em_diff:
        translated[f"diff_{i}"] = _translate_diff_comments(translator, em_diff)
        diff_done += 1
print(f"  邮件内 diff 注释翻译完成: {diff_done} 个")

# 对齐检查
print(f"\n[3/4] 检查段落对齐...")
alignment_issues = []
for i, em in enumerate(emails):
    body_cn = translated.get(f"email_{i}", "")
    body_orig = em.get("body", "")
    if not body_cn or body_cn == body_orig:
        continue

    # 模拟 _render_bilingual_body 中的分离逻辑
    from translate_context import _split_body_and_diff
    text_cn, _ = _split_body_and_diff(body_cn)
    text_orig, _ = _split_body_and_diff(body_orig)

    paras_cn = _split_paragraphs(text_cn)
    paras_en = _split_paragraphs(text_orig)

    # 统计可翻译段落数
    translatable_en = [p for p in paras_en if not _is_untranslatable(p)]
    translatable_cn = []
    cn_idx = 0
    for en in paras_en:
        if _is_untranslatable(en):
            # 跳过锚点
            if cn_idx < len(paras_cn):
                # 模糊匹配
                from translate_context import _fuzzy_match_untranslatable
                for la in range(min(3, len(paras_cn) - cn_idx)):
                    ci = cn_idx + la
                    if ci < len(paras_cn) and _fuzzy_match_untranslatable(paras_cn[ci], en):
                        cn_idx = ci + 1
                        break
                else:
                    if cn_idx < len(paras_cn) and _is_untranslatable(paras_cn[cn_idx]):
                        cn_idx += 1
        else:
            while cn_idx < len(paras_cn) and _is_untranslatable(paras_cn[cn_idx]):
                cn_idx += 1
            if cn_idx < len(paras_cn):
                translatable_cn.append(paras_cn[cn_idx])
                cn_idx += 1
            else:
                translatable_cn.append("")

    if len(translatable_en) != len(translatable_cn):
        alignment_issues.append({
            "email": i,
            "subject": em.get("subject", "")[:60],
            "en_translatable": len(translatable_en),
            "cn_matched": len(translatable_cn),
            "en_total": len(paras_en),
            "cn_total": len(paras_cn),
        })

if alignment_issues:
    print(f"  发现 {len(alignment_issues)} 封邮件有潜在对齐差异:")
    for issue in alignment_issues:
        print(f"    邮件 #{issue['email']}: {issue['subject']}")
        print(f"      EN段落: {issue['en_total']} (可翻译: {issue['en_translatable']})")
        print(f"      CN匹配: {issue['cn_matched']} / CN总段落: {issue['cn_total']}")
else:
    print("  所有邮件段落对齐正常!")

# 生成 HTML
print(f"\n[4/4] 生成 HTML...")
output = generate_html(commit, diff, email_header, emails, checklist, translated)

out_path = Path(f"data/output/test_alignment_{MAX_EMAILS}.html")
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(output, encoding="utf-8")
print(f"  已保存: {out_path}")
print(f"  大小: {len(output) // 1024} KB | {len(output.splitlines())} 行")
print("\n完成! 请在浏览器中打开 HTML 文件检查对齐效果。")