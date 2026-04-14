#!/usr/bin/env python3
"""Apply code detection enhancements to translate_context.py"""
import re

filepath = "translate_context.py"
with open(filepath, "r") as f:
    content = f.read()

# === Part 1: Add new patterns before "return False" at end of _is_code_or_data_line ===
OLD_TAIL = '''    # 邮件引用 + 代码混合行："> +    some_code();"
    if re.match(r'\\s*>\\s*[+\\-]', s):
        return True

    return False'''

NEW_TAIL = '''    # 邮件引用 + 代码混合行："> +    some_code();"
    if re.match(r'\\s*>\\s*[+\\-]', s):
        return True

    # 纯分隔线 (>=4 个连续相同符号)
    _sep_end = '$'  # end-of-line anchor
    if re.match(r'\\s*[=~#*]{4,}\\s*' + _sep_end, s):
        return True
    if re.match(r'\\s*-{4,}\\s*' + _sep_end, s):
        return True
    # lockdep 依赖链: "-> #4 (&rq->__lock){-.-.}-{2:2}:"
    if re.match(r'\\s*->\\s*#\\d+\\s+\\(', s):
        return True
    # 缩进的函数调用栈: "       _raw_spin_lock_nested+0x44/0x5c"
    if re.match(r'\\s{4,}\\S+\\+0x[0-9a-f]+', s):
        return True
    # 锁场景表格: "CPU0"
    if re.match(r'\\s*CPU\\d+\\s*' + _sep_end, s):
        return True
    # "lock(&xxx)" 行
    if re.match(r'\\s*lock\\(', s):
        return True
    # lockdep 标题
    if re.match(r'\\s*(Chain exists of|Possible unsafe|other info that might)', s):
        return True
    # "N locks held by process/PID:"
    if re.match(r'\\s*\\d+\\s+locks?\\s+held\\s+by\\s+', s):
        return True
    # "#0: ffff..." 格式的锁列表
    if re.match(r'\\s*#\\d+:\\s+[0-9a-f]', s):
        return True
    # Hardware name / Not tainted 行
    if re.match(r'\\s*(Hardware name:|Not tainted)', s):
        return True
    # ftrace 事件行: "bprint: ... tid="
    if re.match(r'\\s*\\w+:\\s+(prev_comm|next_comm|tid=|eligible=)', s):
        return True
    # 命令行示例: "./program ..."
    if re.match(r'\\s*\\./', s):
        return True
    # EEVDF 模拟输出: "t=数字 V=数字"
    if re.match(r'\\s*t=\\d+\\s+V=\\d+', s):
        return True

    return False'''

assert OLD_TAIL in content, "Cannot find OLD_TAIL in file"
content = content.replace(OLD_TAIL, NEW_TAIL, 1)

# === Part 2: Add code-line placeholder protection in translate_body ===
OLD_PH = '''            r"^(Signed-off-by|Reviewed-by|Acked-by|Tested-by|Cc|Link):.*$",
            _ph, protected, flags=re.MULTILINE,
        )

        # 如果保护后只剩占位符，不翻译'''

NEW_PH = '''            r"^(Signed-off-by|Reviewed-by|Acked-by|Tested-by|Cc|Link):.*$",
            _ph, protected, flags=re.MULTILINE,
        )

        # 保护段落内的代码/数据行（逐行检查并占位）
        _plines = []
        for _ln in protected.split('\\n'):
            if _ln.strip() and not any(k in _ln for k in placeholders) and _is_code_or_data_line(_ln):
                key = "XYZPH%04dEND" % counter[0]
                counter[0] += 1
                placeholders[key] = _ln
                _plines.append(key)
            else:
                _plines.append(_ln)
        protected = '\\n'.join(_plines)

        # 如果保护后只剩占位符，不翻译'''

assert OLD_PH in content, "Cannot find OLD_PH in file"
content = content.replace(OLD_PH, NEW_PH, 1)

with open(filepath, "w") as f:
    f.write(content)

# Verify
lines = content.split('\n')
print(f"Done. File now has {len(lines)} lines.")

# Quick syntax check
import py_compile
try:
    py_compile.compile(filepath, doraise=True)
    print("Syntax OK")
except py_compile.PyCompileError as e:
    print(f"Syntax ERROR: {e}")
    import sys
    sys.exit(1)