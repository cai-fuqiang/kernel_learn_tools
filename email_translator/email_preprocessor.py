"""
email_preprocessor.py — 邮件预处理：去噪 / 分级 / 引用去重

将 Lore 线程的原始邮件列表转换为高信息密度的处理后邮件列表，
供 pack_for_openclaw.py 打包时使用。

处理流水线：
  1. 分类：判定每封邮件的类型和价值等级
  2. 过滤：丢弃零价值邮件（tip-bot2 通知等）
  3. 去噪：PATCH 邮件去除 diff 只保留 commit message
  4. 引用去重：Re: 回复去除 > 引用块，仅保留新增内容
  5. 签名去除：去掉 "-- " 之后的邮件签名
"""
import re
from enum import IntEnum
from typing import Dict, List, Tuple


class EmailPriority(IntEnum):
    """邮件价值等级，数值越大预算越多"""
    DROP = 0       # 直接丢弃（tip-bot2 通知）
    LOW = 1        # 仅保留 subject + commit msg 摘要（PATCH 正文）
    MEDIUM = 2     # cover letter、含性能数据的测试报告
    HIGH = 3       # review 讨论（Re: 回复中含实质内容）


# 每个等级的字符预算上限
PRIORITY_BUDGET = {
    EmailPriority.DROP:   0,
    EmailPriority.LOW:    500,
    EmailPriority.MEDIUM: 2000,
    EmailPriority.HIGH:   5000,
}


def classify_email(em: Dict) -> EmailPriority:
    """
    判定一封邮件的价值等级。

    规则（按优先级）：
    - tip-bot2 通知 → DROP
    - PATCH 正文（含 diff --git 且非 cover letter）→ LOW
    - Cover letter [PATCH 00/N] → MEDIUM
    - 含性能数据/测试结果的邮件 → MEDIUM（提升）
    - Re: 讨论邮件 → HIGH
    """
    from_addr = em.get("from", "").lower()
    subject   = em.get("subject", "")
    body      = em.get("body", "")

    # ── 零价值：tip-bot2 自动合入通知 ──
    if "tip-bot2" in from_addr or "tip-bot" in from_addr:
        return EmailPriority.DROP

    # ── 零价值：自动生成的 merge/test 通知 ──
    if any(bot in from_addr for bot in ("kernel test robot", "lkp@intel")):
        # 但如果包含有用的编译警告，保留为 LOW
        if "warning:" in body.lower() or "error:" in body.lower():
            return EmailPriority.LOW
        return EmailPriority.DROP

    # ── 判断是否是 PATCH 正文 ──
    is_patch = bool(re.search(r'\[(?:RFC\s*)?(?:PATCH|RESEND)', subject, re.IGNORECASE))
    has_diff = "diff --git" in body or "\n--- a/" in body

    # Cover letter: [PATCH 00/N] 或 [PATCH v2 0/N]
    is_cover = bool(re.search(r'\[.*PATCH.*\s+0+/\d+\]', subject, re.IGNORECASE))

    if is_cover:
        return EmailPriority.MEDIUM

    if is_patch and has_diff:
        return EmailPriority.LOW

    # ── 含性能数据的邮件 → 提升为 MEDIUM ──
    perf_signals = ("benchmark", "regression", "perf stat", "hackbench",
                    "specrate", "spec cpu", "throughput", "latency",
                    "improvement", "schedstat")
    body_lower = body[:3000].lower()
    if any(s in body_lower for s in perf_signals):
        return EmailPriority.MEDIUM

    # ── 其余都是讨论邮件 → HIGH ──
    return EmailPriority.HIGH


def strip_patch_diff(body: str) -> str:
    """
    从 PATCH 邮件正文中去掉 diff 部分，只保留 commit message。

    PATCH 邮件的典型结构：
      <commit message>
      Signed-off-by: ...
      ---
      <diffstat>
      <diff --git ...>

    我们保留 "---" 之前的内容（commit message + Signed-off-by），
    去掉之后的 diff。
    """
    # 找到 "---\n" 分隔线（diff 之前的分隔）
    # 注意：可能有多个 ---，我们要找的是独立一行的 ---
    lines = body.split("\n")
    cut_idx = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "---" and i > 0:
            # 检查前面有 Signed-off-by 或 commit message 内容
            # 后面应该有 diff stat 或 diff --git
            remaining = "\n".join(lines[i+1:])
            if ("diff --git" in remaining or
                re.search(r'^\s+\S+.*\|\s+\d+', remaining, re.MULTILINE)):
                cut_idx = i
                break

    if cut_idx is not None:
        return "\n".join(lines[:cut_idx]).strip()
    return body


def strip_email_signature(body: str) -> str:
    """去除邮件签名（"-- " 标准签名分隔符之后的内容）"""
    # RFC 3676: 签名分隔符是 "-- " (两横杠+空格) 独占一行
    parts = re.split(r'\n-- \n', body, maxsplit=1)
    return parts[0].rstrip()


def strip_quoted_text(body: str) -> str:
    """
    去除 > 引用嵌套，保留回复者的新增内容。

    策略：
    - 连续引用块（>开头）如果超过3行 → 折叠为 "[...引用省略...]"
    - 紧邻回复内容上方的 1-3 行引用保留（提供上下文）
    - 单独的引用行保留（通常是行内回复的上下文）
    """
    lines = body.split("\n")
    result = []
    i = 0

    while i < len(lines):
        line = lines[i]

        # 检测引用块的开始
        if _is_quote_line(line):
            # 收集连续引用块
            quote_start = i
            while i < len(lines) and _is_quote_line(lines[i]):
                i += 1
            quote_end = i  # exclusive
            quote_len = quote_end - quote_start

            if quote_len <= 3:
                # 短引用：保留（通常是行内回复的上下文）
                result.extend(lines[quote_start:quote_end])
            else:
                # 长引用块：检查后面是否紧跟回复内容
                # 保留最后 2 行引用作为上下文
                if i < len(lines) and not _is_quote_line(lines[i]):
                    result.append(f"[...{quote_len - 2} 行引用已省略...]")
                    result.extend(lines[quote_end - 2:quote_end])
                else:
                    # 引用后面没有回复，整块折叠
                    result.append(f"[...{quote_len} 行引用已省略...]")
        else:
            result.append(line)
            i += 1

    return "\n".join(result)


def _is_quote_line(line: str) -> bool:
    """判断是否是引用行"""
    stripped = line.lstrip()
    return stripped.startswith(">")


def preprocess_emails(emails: List[Dict]) -> Tuple[List[Dict], dict]:
    """
    预处理邮件列表：分类 → 过滤 → 去噪 → 返回处理后的邮件列表。

    Returns:
        (processed_emails, stats) — 处理后的邮件列表 + 统计信息
    """
    stats = {
        "total": len(emails),
        "dropped": 0,
        "low": 0,
        "medium": 0,
        "high": 0,
    }

    processed = []
    for em in emails:
        priority = classify_email(em)

        if priority == EmailPriority.DROP:
            stats["dropped"] += 1
            continue

        # 复制一份避免修改原数据
        out = dict(em)
        body = out.get("body", "")
        out["_raw_body"] = body  # 保留原始 body（含 diff），供 HTML 展示用

        # 去签名
        body = strip_email_signature(body)

        # PATCH 正文去 diff
        if priority == EmailPriority.LOW:
            body = strip_patch_diff(body)
            stats["low"] += 1

        # 讨论邮件去引用嵌套
        if priority in (EmailPriority.HIGH, EmailPriority.MEDIUM):
            body = strip_quoted_text(body)
            if priority == EmailPriority.HIGH:
                stats["high"] += 1
            else:
                stats["medium"] += 1

        out["body"] = body.strip()
        out["_priority"] = int(priority)
        processed.append(out)

    return processed, stats