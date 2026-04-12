#!/usr/bin/env python3
"""Apply patches to translate_context.py"""

with open('translate_context.py', 'r') as f:
    lines = f.readlines()

# Patch 1: Insert new rules before "return False" (line 183, 0-indexed=182)
# Find the exact line
target_line = None
for i, line in enumerate(lines):
    if line.strip() == 'return False' and i > 130 and i < 200:
        # Check context: previous line should have "alpha_count < len(s) * 0.3"
        if 'alpha_count' in lines[i-2]:
            target_line = i
            break

if target_line is None:
    print("ERROR: Could not find 'return False' in _is_code_or_data_line()")
    exit(1)

new_rules = r'''
    # ── 以下为新增内核常见输出格式 ──

    # dmesg 输出行：[    0.000000] 或 [12345.678901] 开头
    if re.match(r'\s*\[\s*\d+\.\d+\]', s):
        return True

    # ftrace 输出行：CPU#0 或 funcname-PID 格式，如 "sched_switch-1234"
    if re.match(r'\s*\S+-\d+\s+\[?\d+\]?', s) and ('|' in s or '=>' in s or '<-' in s):
        return True
    # ftrace 函数图格式：含 | 和缩进的函数调用
    if re.match(r'\s*[\d.]+\s*(us|ms|ns)\s*\|', s):
        return True

    # perf report / perf stat 输出
    # "  5.23%  swapper  [kernel.kallsyms]  [k] intel_idle"
    if re.match(r'\s*\d+\.\d+%\s+\S+', s):
        return True
    # perf stat 格式: "1,234,567,890  cycles  (66.67%)"
    if re.match(r'\s*[\d,]+\s+\S+\s+\([\d.]+%\)', s):
        return True

    # 内核 oops/panic 寄存器和栈帧
    # "RBX: 0000000000000000 RCX: ffffffff81234567"
    if re.match(r'\s*(R[A-Z]{1,2}|CR[0-4]|DR[0-7]|FS|GS|CS|SS|EFLAGS):', s):
        return True
    # 栈帧行 "[<ffffffff81234567>] function_name+0x12/0x34"
    if re.match(r'\s*\[<[0-9a-f]+>\]', s):
        return True
    # "? function_name+0x12/0x34 [module]"
    if re.match(r'\s*\?\s+\S+\+0x[0-9a-f]+', s):
        return True

    # lockdep 输出
    if re.match(r'\s*(->|<-)#\d+', s) or re.search(r'lock_type:\s*\d+', s):
        return True

    # sysfs / procfs 路径行
    if re.match(r'\s*/proc/|/sys/', s):
        return True

    # 配置选项行：CONFIG_XXX=y/m/n
    if re.match(r'\s*CONFIG_[A-Z0-9_]+=', s):
        return True

    # Git 日志行：commit hash 行
    if re.match(r'\s*commit [0-9a-f]{7,40}\b', s):
        return True
    # Git shortlog 格式："Author Name (N):"
    if re.match(r'\s*\S.*\(\d+\):$', s):
        return True

    # 邮件引用 + 代码混合行："> +    some_code();"
    if re.match(r'\s*>\s*[+\-]', s):
        return True

'''

# Insert new rules before the "return False" line
lines.insert(target_line, new_rules)
print(f"Patch 1 applied: inserted new rules before line {target_line + 1}")

# Patch 2: Change threshold from 0.6 to 0.5
patched2 = False
for i, line in enumerate(lines):
    if 'code_count >= len(lines) * 0.6' in line:
        lines[i] = line.replace(
            'code_count >= len(lines) * 0.6',
            'code_count >= len(lines) * 0.5  # 从 0.6 降低到 0.5，更保守'
        )
        print(f"Patch 2 applied: threshold changed at line {i + 1}")
        patched2 = True
        break

if not patched2:
    print("ERROR: Could not find threshold 0.6!")
    exit(1)

with open('translate_context.py', 'w') as f:
    f.writelines(lines)

print("All patches applied successfully!")