"""测试：只保留段落对齐视图，移除双栏面板"""
import re
from translate_context import (
    _split_body_and_diff, _html_email_node, _render_bilingual_body,
    _render_bilingual_commit, _esc, ThreadNode,
)

# 测试1: _render_bilingual_body 只返回 para-grid，不含 bilingual 面板
text_cn = "这是中文翻译。\n\n第二段翻译。"
text_orig = "This is english.\n\nSecond paragraph."
html = _render_bilingual_body(text_cn, text_orig)
assert "para-grid" in html, "FAIL: missing para-grid"
assert "bilingual" not in html, "FAIL: bilingual panel should be removed"
assert "bi-panel" not in html, "FAIL: bi-panel should be removed"
assert "bi-toggle" not in html, "FAIL: bi-toggle should be removed"
print("PASS: test1 - bilingual body only has para-grid")

# 测试2: _render_bilingual_commit 也只返回 para-grid
cm_html = _render_bilingual_commit("中文提交信息", "English commit message")
assert "para-grid" in cm_html, "FAIL: commit missing para-grid"
assert "bilingual" not in cm_html, "FAIL: commit still has bilingual panel"
print("PASS: test2 - bilingual commit only has para-grid")

# 测试3: _html_email_node 生成的 HTML 不含 bilingual 面板
body_with_diff = (
    "This is a review comment.\n\n"
    "```diff\n"
    "--- a/test.c\n"
    "+++ b/test.c\n"
    "@@ -1,3 +1,4 @@\n"
    " int main() {\n"
    "+    return 0;\n"
    " }\n"
    "```\n"
)
email = {
    "tag": "[讨论]",
    "subject": "Re: Test patch",
    "from": "Alice <alice@test.com>",
    "date": "2026-01-01",
    "message_id": "<test@id>",
    "in_reply_to": "",
    "body": body_with_diff,
}
node = ThreadNode(email, depth=0)
idx_map = {id(email): 0}
translated_cn = "这是一个审查评论。\n\n```diff\n--- a/test.c\n+++ b/test.c\n@@ -1,3 +1,4 @@\n int main() {\n+    return 0;\n }\n```\n"
translated_bodies = {"email_0": translated_cn}

html_output = _html_email_node(node, idx_map, translated_bodies, is_root=True)
assert "bilingual" not in html_output, "FAIL: email node still has bilingual panel"
assert "bi-panel" not in html_output, "FAIL: email node still has bi-panel"
assert "para-grid" in html_output, "FAIL: email node missing para-grid"
assert "diff-block" in html_output, "FAIL: email node missing diff-block"

# 验证 diff 不在 para-grid 中
grid_match = re.search(r'class="para-grid">(.*?)</div>\s*(?:<details|<div)', html_output, re.DOTALL)
if grid_match:
    grid_html = grid_match.group(1)
    assert "return 0" not in grid_html, "FAIL: para-grid still contains diff code"
    print("PASS: test3a - para-grid has no diff code")

diff_match = re.search(r'class="diff-block".*?</details>', html_output, re.DOTALL)
assert diff_match, "FAIL: no diff-block found"
assert "return 0" in diff_match.group(0), "FAIL: diff-block missing diff code"
print("PASS: test3b - diff-block has diff code")

print("\n=== ALL TESTS PASSED ===")