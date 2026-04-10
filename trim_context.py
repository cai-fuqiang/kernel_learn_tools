#!/usr/bin/env python3
"""trim_context.py — 从完整版 context 裁剪出精简版

将 pack_for_openclaw.py 生成的完整版 context_full.txt 按字符数裁剪，
生成适合喂给 AI 模型的精简版。

裁剪策略：
  1. COMMIT 信息 + diff + 相关 commit → 完整保留（头部）
  2. 引导问题 → 完整保留（尾部）
  3. 邮件线程 → 按优先级从低到高裁剪（先砍 [PATCH摘要]，再砍 [概述/数据]）

用法:
    # 默认 100K 字符
    python trim_context.py data/output/xxxx_context_full.txt

    # 指定大小（字符数）
    python trim_context.py data/output/xxxx_context_full.txt --max-size 200000

    # 指定输出路径
    python trim_context.py data/output/xxxx_context_full.txt -o /tmp/trimmed.txt
"""
import argparse
import re
import sys
from pathlib import Path

DEFAULT_MAX_SIZE = 100_000

# 邮件区段的优先级标记 → 裁剪顺序（数值越小越先被砍）
TRIM_ORDER = {
    "[PATCH摘要]": 1,
    "[概述/数据]": 2,
    "[讨论]":      3,
}


def parse_sections(text: str):
    """将完整 context 拆分为三段：头部、邮件列表、尾部

    Returns:
        (header, emails, footer)
        - header: commit 信息 + diff + 相关 commit + 邮件线程标题行
        - emails: [(priority_order, full_block_text), ...]
        - footer: 引导问题
    """
    SEP = "=" * 70

    # 找到邮件线程区段的起始位置
    email_section_match = re.search(
        r'^(={70}\n# Lore 邮件线程.*?\n={70}\n(?:# \[HIGH:.*\n)?)',
        text, re.MULTILINE
    )

    if not email_section_match:
        # 没有邮件区段 → 整体不裁剪
        return text, [], ""

    email_section_start = email_section_match.start()
    email_header = email_section_match.group(0)
    header = text[:email_section_start] + email_header

    rest = text[email_section_match.end():]

    # 找到分析清单区段（尾部）
    footer_match = re.search(
        r'\n(={70}\n# (?:分析任务清单|请基于以上内容).*)',
        rest, re.DOTALL
    )

    if footer_match:
        email_body = rest[:footer_match.start()]
        footer = footer_match.group(1)
    else:
        email_body = rest
        footer = ""

    # 拆分邮件为独立块（每封以 "## [标记] " 开头）
    email_blocks = re.split(r'\n(?=## \[)', email_body)
    emails = []
    for block in email_blocks:
        block = block.strip()
        if not block:
            continue

        # 提取优先级标记
        tag_match = re.match(r'## \[([^\]]+)\]', block)
        if tag_match:
            tag = f"[{tag_match.group(1)}]"
            order = TRIM_ORDER.get(tag, 3)
        else:
            order = 3  # 未识别 → 当高优先级保留

        emails.append((order, block))

    return header, emails, footer


def trim_context(text: str, max_size: int) -> tuple:
    """裁剪 context 到 max_size 字符以内

    Returns:
        (trimmed_text, stats)
    """
    if len(text) <= max_size:
        return text, {
            "original_size": len(text),
            "trimmed_size": len(text),
            "emails_kept": "全部",
            "emails_dropped": 0,
            "trimmed": False,
        }

    header, emails, footer = parse_sections(text)

    if not emails:
        # 没有邮件区段 → 粗暴截断
        return text[:max_size], {
            "original_size": len(text),
            "trimmed_size": max_size,
            "emails_kept": "N/A",
            "emails_dropped": 0,
            "trimmed": True,
        }

    # 计算固定部分占用
    fixed_size = len(header) + len(footer) + 200  # 200 字符余量
    budget = max_size - fixed_size

    if budget <= 0:
        # 头部+尾部就超了 → 只输出头部+尾部
        return header + "\n" + footer, {
            "original_size": len(text),
            "trimmed_size": len(header) + len(footer),
            "emails_kept": 0,
            "emails_dropped": len(emails),
            "trimmed": True,
        }

    # 按优先级从高到低排序（order 大的优先保留）
    emails_sorted = sorted(emails, key=lambda e: -e[0])

    # 两轮策略：
    # 第一轮：先尝试全部放入，每封邮件按分级预算截断
    # 第二轮：如果还超，从低优先级开始整封丢弃

    # 分级每封邮件的字符预算
    PER_EMAIL_BUDGET = {
        1: 500,    # [PATCH摘要]
        2: 2000,   # [概述/数据]
        3: 5000,   # [讨论]
    }

    # 第一轮：按分级预算截断每封邮件
    trimmed_emails = []
    for order, block in emails_sorted:
        limit = PER_EMAIL_BUDGET.get(order, 5000)
        if len(block) > limit:
            # 截断邮件正文
            block = block[:limit] + "\n... [正文已截断]"
        trimmed_emails.append((order, block))

    # 按原始顺序恢复（先 HIGH 后 LOW）
    trimmed_emails.sort(key=lambda e: -e[0])

    # 逐封放入，预算不够从低优先级开始丢弃
    kept = []
    used = 0
    dropped = 0

    for order, block in trimmed_emails:
        block_size = len(block) + 2  # +2 for newlines
        if used + block_size <= budget:
            kept.append(block)
            used += block_size
        else:
            dropped += 1

    # 组装结果
    result_parts = [header]
    result_parts.extend(f"\n{block}" for block in kept)

    if dropped > 0:
        result_parts.append(
            f"\n\n... [已展示 {len(kept)}/{len(emails)} 封邮件"
            f"，{dropped} 封因字符限制被省略]"
        )

    result_parts.append(f"\n{footer}")
    result = "\n".join(result_parts)

    return result, {
        "original_size": len(text),
        "trimmed_size": len(result),
        "emails_total": len(emails),
        "emails_kept": len(kept),
        "emails_dropped": dropped,
        "trimmed": True,
    }


def main():
    parser = argparse.ArgumentParser(
        description="从完整版 context 裁剪出精简版"
    )
    parser.add_argument("input", help="输入的完整版 context 文件路径")
    parser.add_argument("--max-size", type=int, default=DEFAULT_MAX_SIZE,
                        help=f"目标字符数上限（默认 {DEFAULT_MAX_SIZE}）")
    parser.add_argument("-o", "--output", help="输出文件路径（默认自动生成）")
    args = parser.parse_args()

    # 读取完整版
    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在: {input_path}")
        sys.exit(1)

    text = input_path.read_text(encoding="utf-8")
    print(f"读取: {input_path}")
    print(f"原始大小: {len(text) // 1024} KB ({len(text)} 字符)")

    # 裁剪
    result, stats = trim_context(text, args.max_size)

    # 输出路径
    if args.output:
        out_path = Path(args.output)
    else:
        # xxx_context_full.txt → xxx_context_trimmed.txt
        stem = input_path.stem.replace("_full", "")
        out_path = input_path.parent / f"{stem}_trimmed.txt"

    out_path.write_text(result, encoding="utf-8")
    trimmed_kb = len(result) // 1024

    print()
    if not stats["trimmed"]:
        print(f"无需裁剪: 原始大小 {len(text) // 1024} KB 未超过 {args.max_size // 1024} KB 上限")
        print(f"已复制到: {out_path}")
    else:
        print(f"裁剪完成:")
        print(f"  {len(text) // 1024} KB → {trimmed_kb} KB")
        print(f"  邮件: {stats['emails_kept']}/{stats['emails_total']} 封保留"
              f"，{stats['emails_dropped']} 封被省略")
        print(f"已保存: {out_path}")

    print(f"\n  openclaw chat --file {out_path}\n")


if __name__ == "__main__":
    main()