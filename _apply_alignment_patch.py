#!/usr/bin/env python3
"""Apply alignment improvements to translate_context.py (TODO 5)"""

filepath = "translate_context.py"
with open(filepath, "r") as f:
    content = f.read()

# === Part 1: Add consecutive match bonus in DP ===

OLD_DP = '''    SKIP_EN_PENALTY = -0.15  # 跳过可翻译EN段落的惩罚

    # dp[ti+1][ci+1] 表示前 ti 个可翻译EN和前 ci 个CN段落的最优匹配
    dp = [[0.0] * (n_cn + 1) for _ in range(n_te + 1)]
    choice = [[None] * (n_cn + 1) for _ in range(n_te + 1)]

    for ti in range(n_te):
        ei = trans_en[ti]
        for ci in range(n_cn):
            # 选项1: 不匹配 CN[ci]（跳过CN段落，无惩罚）
            if dp[ti + 1][ci] >= dp[ti + 1][ci + 1]:
                dp[ti + 1][ci + 1] = dp[ti + 1][ci]
                choice[ti + 1][ci + 1] = ('skip_cn', ti, ci)

            # 选项2: 不匹配 trans_en[ti]（跳过EN段落，有惩罚）
            skip_score = dp[ti][ci + 1] + SKIP_EN_PENALTY
            if skip_score > dp[ti + 1][ci + 1]:
                dp[ti + 1][ci + 1] = skip_score
                choice[ti + 1][ci + 1] = ('skip_en', ti, ci)

            # 选项3: 匹配 CN[ci] 与 trans_en[ti]
            sc = scores.get((ci, ei), 0.0)
            if sc > 0 and dp[ti][ci] + sc > dp[ti + 1][ci + 1]:
                dp[ti + 1][ci + 1] = dp[ti][ci] + sc
                choice[ti + 1][ci + 1] = ('match', ti, ci)'''

NEW_DP = '''    SKIP_EN_PENALTY = -0.15  # 跳过可翻译EN段落的惩罚
    CONSECUTIVE_BONUS = 0.08  # 连续匹配奖励，鼓励相邻段落连续配对

    # dp[ti+1][ci+1] 表示前 ti 个可翻译EN和前 ci 个CN段落的最优匹配
    dp = [[0.0] * (n_cn + 1) for _ in range(n_te + 1)]
    choice = [[None] * (n_cn + 1) for _ in range(n_te + 1)]

    for ti in range(n_te):
        ei = trans_en[ti]
        for ci in range(n_cn):
            # 选项1: 不匹配 CN[ci]（跳过CN段落，无惩罚）
            if dp[ti + 1][ci] >= dp[ti + 1][ci + 1]:
                dp[ti + 1][ci + 1] = dp[ti + 1][ci]
                choice[ti + 1][ci + 1] = ('skip_cn', ti, ci)

            # 选项2: 不匹配 trans_en[ti]（跳过EN段落，有惩罚）
            skip_score = dp[ti][ci + 1] + SKIP_EN_PENALTY
            if skip_score > dp[ti + 1][ci + 1]:
                dp[ti + 1][ci + 1] = skip_score
                choice[ti + 1][ci + 1] = ('skip_en', ti, ci)

            # 选项3: 匹配 CN[ci] 与 trans_en[ti]
            sc = scores.get((ci, ei), 0.0)
            if sc > 0:
                # 连续匹配奖励：如果上一步 (ti-1, ci-1) 也是 match，给额外加分
                bonus = 0.0
                if ti > 0 and ci > 0 and choice[ti][ci] is not None and choice[ti][ci][0] == 'match':
                    bonus = CONSECUTIVE_BONUS
                total = dp[ti][ci] + sc + bonus
                if total > dp[ti + 1][ci + 1]:
                    dp[ti + 1][ci + 1] = total
                    choice[ti + 1][ci + 1] = ('match', ti, ci)'''

assert OLD_DP in content, f"Cannot find OLD_DP"
content = content.replace(OLD_DP, NEW_DP, 1)

# === Part 2: Add fallback for alignment failure (too many unpaired) ===

OLD_RESULT = '''    # 构建结果
    result = []
    for ei in range(n_en):
        if en_untrans[ei]:
            result.append(('untrans',))
        else:
            ti_idx = trans_en.index(ei)
            if ti_idx in matches:
                result.append(('pair', matches[ti_idx]))
            else:
                result.append(('unpaired',))

    return result'''

NEW_RESULT = '''    # 构建结果
    result = []
    for ei in range(n_en):
        if en_untrans[ei]:
            result.append(('untrans',))
        else:
            ti_idx = trans_en.index(ei)
            if ti_idx in matches:
                result.append(('pair', matches[ti_idx]))
            else:
                result.append(('unpaired',))

    # Fallback: 如果匹配率过低（>50% unpaired），降级为简单顺序配对
    paired_count = sum(1 for r in result if r[0] == 'pair')
    unpaired_count = sum(1 for r in result if r[0] == 'unpaired')
    if unpaired_count > 0 and paired_count < unpaired_count:
        # 降级：按顺序将可翻译 CN 段落依次配对到可翻译 EN 段落
        trans_cn = [ci for ci in range(n_cn) if not cn_untrans[ci]]
        result = []
        cn_iter = iter(trans_cn)
        for ei in range(n_en):
            if en_untrans[ei]:
                result.append(('untrans',))
            else:
                try:
                    ci = next(cn_iter)
                    result.append(('pair', ci))
                except StopIteration:
                    result.append(('unpaired',))

    return result'''

assert OLD_RESULT in content, f"Cannot find OLD_RESULT"
content = content.replace(OLD_RESULT, NEW_RESULT, 1)

# === Part 3: In similarity calculation, support N:M by checking merged EN paragraphs ===
# We add merged-paragraph similarity in the scores computation loop.

OLD_SCORES = '''    # 计算可翻译 EN 与可翻译 CN 的相似度矩阵
    scores = {}
    for ei in trans_en:
        for ci in range(n_cn):
            if cn_untrans[ci]:
                continue  # CN 侧引用/签名段落不参与配对
            sc = _translation_similarity(paras_cn[ci], paras_en[ei])
            if sc >= 0.15:
                scores[(ci, ei)] = sc'''

NEW_SCORES = '''    # 计算可翻译 EN 与可翻译 CN 的相似度矩阵
    scores = {}
    for ei in trans_en:
        for ci in range(n_cn):
            if cn_untrans[ci]:
                continue  # CN 侧引用/签名段落不参与配对
            sc = _translation_similarity(paras_cn[ci], paras_en[ei])
            if sc >= 0.15:
                scores[(ci, ei)] = sc

    # N:M 匹配支持：尝试合并相邻 EN 段落后与 CN 段落匹配
    # 这处理翻译引擎将多个EN段落合并为一个CN段落的情况
    for idx in range(len(trans_en) - 1):
        ei1 = trans_en[idx]
        ei2 = trans_en[idx + 1]
        merged_en = paras_en[ei1] + '\\n' + paras_en[ei2]
        for ci in range(n_cn):
            if cn_untrans[ci]:
                continue
            sc = _translation_similarity(paras_cn[ci], merged_en)
            # 如果合并后相似度显著高于单独匹配，记录到 scores
            sc_single = max(scores.get((ci, ei1), 0), scores.get((ci, ei2), 0))
            if sc >= 0.4 and sc > sc_single + 0.1:
                # 将较高分赋给第一个 EN，第二个 EN 留空让 DP 自然跳过
                scores[(ci, ei1)] = max(scores.get((ci, ei1), 0), sc)'''

assert OLD_SCORES in content, f"Cannot find OLD_SCORES"
content = content.replace(OLD_SCORES, NEW_SCORES, 1)

with open(filepath, "w") as f:
    f.write(content)

lines = content.split('\\n')
print(f"Done. File now has {len(lines)} lines.")

import py_compile
try:
    py_compile.compile(filepath, doraise=True)
    print("Syntax OK")
except py_compile.PyCompileError as e:
    print(f"Syntax ERROR: {e}")
    import sys
    sys.exit(1)