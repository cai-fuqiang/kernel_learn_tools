#!/usr/bin/env python3
"""测试聚焦视图功能的完整性"""
import re
import sys
sys.path.insert(0, '.')

from translate_context import (
    parse_commit_section, parse_diff_section, parse_emails,
    parse_analysis_checklist, generate_html, _build_thread_tree,
)


def test_focus_view():
    print("=" * 60)
    print("聚焦视图功能完整测试")
    print("=" * 60)

    # 1. 读取并解析测试数据
    print("\n[1] 读取测试数据...")
    text = open('doc/example_output/v2/d07f09a1f99c_context_full.txt').read()
    commit = parse_commit_section(text)
    diff = parse_diff_section(text)
    email_header, emails = parse_emails(text)
    checklist = parse_analysis_checklist(text)
    print(f"  邮件: {len(emails)} 封")

    # 2. 构建线程树验证
    print("\n[2] 构建线程树...")
    threads = _build_thread_tree(emails)
    print(f"  线程: {len(threads)} 个")
    max_depth = 0
    def _count_depth(node, d=0):
        nonlocal max_depth
        max_depth = max(max_depth, d)
        for c in node.children:
            _count_depth(c, d + 1)
    for t in threads:
        for r in t['roots']:
            _count_depth(r)
    print(f"  最大嵌套深度: {max_depth}")

    # 3. 生成 HTML（不翻译）
    print("\n[3] 生成 HTML...")
    html = generate_html(commit, diff, email_header, emails, checklist, {})
    print(f"  HTML 大小: {len(html)} 字符 ({len(html)//1024} KB)")

    errors = []

    # 4. 验证 HTML 结构
    print("\n[4] 验证 HTML 结构...")

    # 4.1 focus-bar 存在
    if '<div class="focus-bar" id="focusBar">' in html:
        print("  ✅ focus-bar 存在")
    else:
        errors.append("focus-bar 不存在")
        print("  ❌ focus-bar 不存在")

    # 4.2 focusContainer 存在
    if '<div id="focusContainer"></div>' in html:
        print("  ✅ focusContainer 存在")
    else:
        errors.append("focusContainer 不存在")
        print("  ❌ focusContainer 不存在")

    # 4.3 page-content 包裹
    if '<div class="page-content">' in html:
        print("  ✅ page-content 容器存在")
    else:
        errors.append("page-content 容器不存在")
        print("  ❌ page-content 容器不存在")

    # 4.4 page-content 正确闭合
    if '<!-- .page-content -->' in html:
        print("  ✅ page-content 正确闭合")
    else:
        errors.append("page-content 未正确闭合")
        print("  ❌ page-content 未正确闭合")

    # 4.5 返回按钮
    if '← 返回全部' in html:
        print("  ✅ 返回按钮存在")
    else:
        errors.append("返回按钮不存在")
        print("  ❌ 返回按钮不存在")

    # 5. 验证邮件节点
    print("\n[5] 验证邮件节点...")

    # 5.1 email-node 数量
    node_ids = re.findall(r'<div class="email-node" id="(email-node-[^"]+)"', html)
    print(f"  email-node 节点数: {len(node_ids)}")
    if len(node_ids) == len(emails):
        print(f"  ✅ 节点数 ({len(node_ids)}) 等于邮件数 ({len(emails)})")
    else:
        errors.append(f"节点数 ({len(node_ids)}) != 邮件数 ({len(emails)})")
        print(f"  ❌ 节点数 ({len(node_ids)}) != 邮件数 ({len(emails)})")

    # 5.2 节点 ID 唯一
    if len(set(node_ids)) == len(node_ids):
        print(f"  ✅ 所有节点 ID 唯一")
    else:
        dup = [x for x in node_ids if node_ids.count(x) > 1]
        errors.append(f"存在重复节点 ID: {set(dup)}")
        print(f"  ❌ 存在重复节点 ID: {set(dup)}")

    # 5.3 data-author 和 data-subject 属性
    data_authors = re.findall(r'data-author="([^"]*)"', html)
    data_subjects = re.findall(r'data-subject="([^"]*)"', html)
    if len(data_authors) == len(node_ids):
        print(f"  ✅ 所有节点都有 data-author 属性")
    else:
        errors.append("部分节点缺少 data-author 属性")
        print(f"  ❌ data-author 数量 ({len(data_authors)}) != 节点数 ({len(node_ids)})")
    if len(data_subjects) == len(node_ids):
        print(f"  ✅ 所有节点都有 data-subject 属性")
    else:
        errors.append("部分节点缺少 data-subject 属性")
        print(f"  ❌ data-subject 数量 ({len(data_subjects)}) != 节点数 ({len(node_ids)})")

    # 6. 验证聚焦按钮
    print("\n[6] 验证聚焦按钮...")
    focus_btns = re.findall(r'onclick="focusEmail\(\'(email-node-[^\']+)\'\)"', html)
    print(f"  聚焦按钮数: {len(focus_btns)}")
    if len(focus_btns) == len(emails):
        print(f"  ✅ 按钮数 ({len(focus_btns)}) 等于邮件数 ({len(emails)})")
    else:
        errors.append(f"按钮数 ({len(focus_btns)}) != 邮件数 ({len(emails)})")
        print(f"  ❌ 按钮数 ({len(focus_btns)}) != 邮件数 ({len(emails)})")

    # 6.1 每个按钮引用的 ID 都存在
    missing_refs = [btn for btn in focus_btns if btn not in node_ids]
    if not missing_refs:
        print(f"  ✅ 所有按钮引用的节点 ID 都存在")
    else:
        errors.append(f"按钮引用了不存在的节点: {missing_refs[:5]}")
        print(f"  ❌ 按钮引用了不存在的节点: {missing_refs[:5]}")

    # 7. 验证 JavaScript
    print("\n[7] 验证 JavaScript...")

    js_checks = [
        ('function focusEmail(nodeId)', 'focusEmail 函数'),
        ('function unfocusEmail()', 'unfocusEmail 函数'),
        ('getElementById(nodeId)', '节点查找'),
        ('cloneNode(true)', '节点克隆'),
        ('focusContainer', '聚焦容器操作'),
        ("classList.add('focusing')", '聚焦状态切换'),
        ("classList.remove('focusing')", '取消聚焦状态'),
        ('_focusPrevScroll', '滚动位置恢复'),
    ]
    for pattern, desc in js_checks:
        if pattern in html:
            print(f"  ✅ {desc}")
        else:
            errors.append(f"JS: {desc} 缺失")
            print(f"  ❌ {desc} 缺失")

    # 8. 验证 CSS
    print("\n[8] 验证 CSS...")

    css_checks = [
        ('.focus-btn', '聚焦按钮样式'),
        ('.focus-bar', '聚焦栏样式'),
        ('.back-btn', '返回按钮样式'),
        ('#focusContainer', '聚焦容器样式'),
        ('body.focusing .page-content', '聚焦时隐藏主内容'),
    ]
    for pattern, desc in css_checks:
        if pattern in html:
            print(f"  ✅ {desc}")
        else:
            errors.append(f"CSS: {desc} 缺失")
            print(f"  ❌ {desc} 缺失")

    # 9. 验证 HTML 合法性（简单检查）
    print("\n[9] 基本 HTML 结构检查...")
    if html.strip().startswith('<!DOCTYPE html>'):
        print("  ✅ DOCTYPE 声明")
    else:
        errors.append("缺少 DOCTYPE 声明")
        print("  ❌ 缺少 DOCTYPE 声明")

    if '</html>' in html:
        print("  ✅ 有 </html> 结束标签")
    else:
        errors.append("缺少 </html> 结束标签")
        print("  ❌ 缺少 </html> 结束标签")

    if '</script>' in html:
        print("  ✅ 有 </script> 结束标签")
    else:
        errors.append("缺少 </script> 结束标签")
        print("  ❌ 缺少 </script> 结束标签")

    # 10. 保存测试输出
    out_path = 'data/output/test_focus_view.html'
    with open(out_path, 'w') as f:
        f.write(html)
    print(f"\n[10] 测试 HTML 已保存到: {out_path}")

    # 总结
    print("\n" + "=" * 60)
    if errors:
        print(f"❌ 测试失败: {len(errors)} 个错误")
        for e in errors:
            print(f"   - {e}")
        return False
    else:
        print("✅ 所有测试通过！")
        print(f"   - {len(emails)} 封邮件，{len(threads)} 个线程")
        print(f"   - 最大嵌套深度: {max_depth} 层")
        print(f"   - {len(node_ids)} 个可聚焦节点")
        print(f"   - HTML 大小: {len(html)//1024} KB")
        return True


if __name__ == '__main__':
    ok = test_focus_view()
    sys.exit(0 if ok else 1)