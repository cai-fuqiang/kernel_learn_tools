#!/usr/bin/env python3
"""一次性给 translate_context.py 打三个补丁"""
import re, textwrap

with open('translate_context.py', 'r') as f:
    code = f.read()

# ── 补丁1: 在 translate_body 前面插入辅助函数 ──

helpers = textwrap.dedent(r'''
def _is_code_or_data_line(line):
    """判断单行是否是代码/数据行，不应被合并或翻译。"""
    s = line.strip()
    if not s:
        return False
    if re.search(r'\w\+0x[0-9a-f]+(?:/0x[0-9a-f]+)?', s):
        return True
    if re.search(r'\{[.\-]+\}-\{\d+:\d+\}', s):
        return True
    if re.match(r'\s*\S+\.\w+\s+\|\s+\d+', s):
        return True
    if re.search(r'\d+\s+files?\s+changed', s):
        return True
    if re.match(r'\s*(struct\s+\w|#define\s|static\s|void\s|int\s|unsigned\s|'
                r'long\s|char\s|const\s|enum\s|typedef\s|return\s|if\s*\(|'
                r'for\s*\(|while\s*\()', s):
        return True
    if re.match(r'\s*(WARNING:|BUG:|Call Trace:|<TASK>|</TASK>|'
                r'RIP:|RSP:|RAX:|CPU:|PID:|Comm:|Workqueue:)', s):
        return True
    if re.match(r'\s*(kernel|include|arch|drivers|fs|mm|net|lib)/\S+', s):
        return True
    if re.match(r'\s*(0x[0-9a-f]{4,}|[0-9a-f]{12,})\b', s):
        return True
    if re.match(r'\s*->\s*#\d+', s):
        return True
    return False


def _merge_soft_linebreaks(text, placeholders):
    """合并段落内的软换行，保留代码/数据行不合并。"""
    if not text or '\n' not in text:
        return text
    ph_set = set(placeholders.keys())
    lines = text.split('\n')
    merged = []
    buf = []
    def _flush():
        if buf:
            merged.append(' '.join(buf))
            buf.clear()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            _flush(); merged.append(''); continue
        if any(ph in stripped for ph in ph_set):
            _flush(); merged.append(line); continue
        if stripped.startswith(('>', '+', '-', 'diff ', '@@', 'index ',
                                'Signed-off-by:', 'Reviewed-by:', 'Acked-by:',
                                'Tested-by:', 'Cc:', 'Link:', 'Fixes:',
                                'Reported-by:')):
            _flush(); merged.append(line); continue
        if _is_code_or_data_line(line):
            _flush(); merged.append(line); continue
        if re.match(r'^\s*(\d+[.)]\s|[*\u2022]\s)', stripped):
            _flush()
        buf.append(stripped)
    _flush()
    return '\n'.join(merged)


def _clean_translation_artifacts(text):
    """清理翻译引擎引入的孤立反引号。"""
    bt = chr(96)
    lines = text.split('\n')
    out = []
    for line in lines:
        s = line.strip()
        if s == bt:
            continue
        if s.endswith(bt) and len(s) > 1:
            line = line.rstrip()[:-1].rstrip()
        out.append(line)
    return '\n'.join(out)

''')

anchor = 'def translate_body(translator: BaseTranslator, body: str) -> str:\n'
code = code.replace(anchor, helpers + '\n' + anchor, 1)

# ── 补丁2: 段落判断加 _is_code_or_data_line + 占比跳过 ──

old = '''        # 全部是引用行 / diff行 / 签名行 \u2192 不翻译
        is_code_or_quote = all(
            l.lstrip().startswith(('>', '+', '-', 'diff ', '@@', 'index '))
            or l.strip().startswith((
                'Signed-off-by:', 'Reviewed-by:', 'Acked-by:',
                'Tested-by:', 'Cc:', 'Link:', 'Fixes:', 'Reported-by:',
            ))
            or not l.strip()
            for l in lines
        )
        if is_code_or_quote:
            result_parts.append(part)
            continue'''

new = '''        # 全部是引用行 / diff行 / 签名行 / 代码数据行 \u2192 不翻译
        is_code_or_quote = all(
            l.lstrip().startswith(('>', '+', '-', 'diff ', '@@', 'index '))
            or l.strip().startswith((
                'Signed-off-by:', 'Reviewed-by:', 'Acked-by:',
                'Tested-by:', 'Cc:', 'Link:', 'Fixes:', 'Reported-by:',
            ))
            or _is_code_or_data_line(l)
            or not l.strip()
            for l in lines
        )
        if is_code_or_quote:
            result_parts.append(part)
            continue

        # 段落中代码/数据行占多数 \u2192 整段不翻译
        if lines:
            code_count = sum(1 for l in lines if _is_code_or_data_line(l))
            if code_count >= len(lines) * 0.6:
                result_parts.append(part)
                continue'''

code = code.replace(old, new, 1)

# ── 补丁3: 翻译前合并软换行 ──

old3 = '        # \u2500\u2500 翻译该段落 \u2500\u2500\n        translated = translator.translate_email({"subject": "", "body": protected})'
new3 = '        # \u2500\u2500 合并段落内软换行 \u2500\u2500\n        merged = _merge_soft_linebreaks(protected, placeholders)\n\n        # \u2500\u2500 翻译该段落 \u2500\u2500\n        translated = translator.translate_email({"subject": "", "body": merged})'
code = code.replace(old3, new3, 1)

# ── 补丁4: 翻译后清理反引号 ──

old4 = '        result_parts.append(output)\n\n    return "".join(result_parts)'
new4 = '        output = _clean_translation_artifacts(output)\n        result_parts.append(output)\n\n    return "".join(result_parts)'
code = code.replace(old4, new4, 1)

with open('translate_context.py', 'w') as f:
    f.write(code)

print(f"Patched. Total lines: {len(code.splitlines())}")