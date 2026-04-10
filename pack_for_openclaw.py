#!/usr/bin/env python3
"""pack_for_openclaw.py - 贪心预取：将 commit + 完整 patchset + 邮件线程打包给 OpenClaw

策略：规则驱动，尽可能多地把原材料抓齐，一次性喂给 AI，一次出结果。

抓取顺序（优先级从高到低）：
  1. commit 本身的 diff
  2. Lore 直链 → 完整邮件线程（含所有 review 回复）
  3. 同系列相关 commit（同作者 + 时间窗口 + 相同文件）
  4. 本地已保存的邮件 JSON（降级策略）

用法:
    # 最简单（离线，只有 commit + diff）
    python pack_for_openclaw.py <hash> --repo /path/to/linux

    # 完整模式（抓 Lore 线程 + 同系列 commit）
    python pack_for_openclaw.py <hash> --repo /path/to/linux --full

    # 使用本地已保存的邮件 JSON
    python pack_for_openclaw.py <hash> --repo /path/to/linux --full --email-json data/emails/xxx.json

    # 指定输出路径
    python pack_for_openclaw.py <hash> --repo /path/to/linux --full --output /tmp/ctx.txt
"""
import argparse
import json
import logging
import os
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)

sys.path.insert(0, str(Path(__file__).parent))
from email_translator.commit_analyzer import CommitAnalyzer
from email_translator.lore_thread_fetcher import LoreThreadFetcher
from email_translator.email_preprocessor import (
    preprocess_emails, EmailPriority, PRIORITY_BUDGET,
)
from email_translator.config import OUTPUT_DIR

KERNEL_REPO = "/Users/wangfuqiang49/workspace/kernel/extension_file_system/upstream/linux-master"

# 默认裁剪阈值（字符数），仅 trim_context.py 使用
# pack_for_openclaw.py 现在生成不截断的完整版
DEFAULT_TRIM_SIZE = 100_000


# ───────────────────────────────────────────────────────────────────────────────
# 抓取函数
# ───────────────────────────────────────────────────────────────────────────────

def get_full_diff(repo_path: str, commit_hash: str) -> str:
    r = subprocess.run(
        ["git", "show", commit_hash],
        cwd=repo_path, capture_output=True, text=True, timeout=30
    )
    return r.stdout if r.returncode == 0 else ""


def fetch_lore_thread(lore_url: str, email_json: str = "") -> list:
    """从 Lore 直链下载完整线程，失败时从本地 JSON 降级加载

    Args:
        lore_url:   Lore 邮件线程 URL
        email_json: 本地邮件 JSON 文件路径（降级策略）
    """
    # 先尝试在线抓取
    try:
        fetcher = LoreThreadFetcher(timeout=20, max_retries=2)
        emails = fetcher.fetch_by_url(lore_url)
        if emails:
            return emails
    except Exception as e:
        logger.warning("Lore 线程抓取异常: %s", e)

    # 降级到本地 JSON
    if email_json and os.path.exists(email_json):
        logger.info("降级: 从本地 JSON 加载邮件: %s", email_json)
        try:
            with open(email_json, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.warning("加载本地 JSON 失败: %s", e)

    # 自动搜索 data/emails/ 目录下的匹配文件
    emails_dir = Path(__file__).parent / "data" / "emails"
    if emails_dir.is_dir():
        for f in sorted(emails_dir.glob("*.json"), reverse=True):
            try:
                with open(f, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                if data and isinstance(data, list):
                    logger.info("降级: 自动发现本地邮件缓存: %s", f.name)
                    return data
            except Exception:
                continue

    return []


def get_related_commits(commit_info, repo_path: str) -> list:
    """获取同系列相关 commit 的 diff 列表"""
    ca = CommitAnalyzer(repo_path=repo_path)
    hashes = ca.find_related_commits(commit_info, extra_days=7)
    related = []
    for h in hashes[:10]:  # 最多10个
        diff = get_full_diff(repo_path, h)
        if diff:
            # 只取 subject 和 diff 部分
            lines = diff.splitlines()
            subject = next((l for l in lines if l.startswith("    ") and l.strip()), "")
            diff_start = diff.find("\ndiff --git")
            diff_body = diff[diff_start:].strip() if diff_start != -1 else ""
            related.append({
                "hash": h[:12],
                "subject": subject.strip(),
                "diff": diff_body[:2000]
            })
    return related


# ───────────────────────────────────────────────────────────────────────────────
# 打包函数
# ───────────────────────────────────────────────────────────────────────────────

def build_context(commit_info, diff: str, lore_emails: list, related_commits: list) -> str:
    SEP = "=" * 70
    lines = []

    # ── 1. Commit 信息 ────────────────────────────────────────────────
    lines += [
        SEP,
        "# COMMIT 信息",
        SEP,
        f"Hash    : {commit_info.hash}",
        f"Subject : {commit_info.subject}",
        f"Author  : {commit_info.author} <{commit_info.author_email}>",
        f"Date    : {commit_info.date.strftime('%Y-%m-%d')}",
        f"Files   : {', '.join(commit_info.files)}",
        f"Subsys  : {', '.join(commit_info.subsystems) or '未识别'}",
    ]
    if commit_info.patch_total > 0:
        ver = f" {commit_info.patch_version}" if commit_info.patch_version else ""
        lines.append(
            f"Patchset: [PATCH{ver} {commit_info.patch_index}/{commit_info.patch_total}]"
            f"  （共 {commit_info.patch_total} 个 patch）"
        )
    if commit_info.lore_url:
        lines.append(f"Lore    : {commit_info.lore_url}")

    if commit_info.body.strip():
        lines += ["", "## Commit Message", commit_info.body.strip()]

    # ── 2. Diff ───────────────────────────────────────────────────────
    if diff:
        diff_start = diff.find("\ndiff --git")
        diff_body = diff[diff_start:].strip() if diff_start != -1 else diff
        truncated = len(diff_body) > 6000
        lines += [
            "", "## 代码变更 (diff)", "```diff",
            diff_body[:6000] + ("\n... [diff 已截断，仅展示前 6000 字符]" if truncated else ""),
            "```",
        ]

    # ── 3. 同系列相关 commit ──────────────────────────────────────────
    if related_commits:
        lines += ["", SEP, f"# 同系列相关 Commit（共 {len(related_commits)} 个）", SEP]
        for rc in related_commits:
            lines += [
                "",
                f"## {rc['hash']}  {rc['subject']}",
                "```diff",
                rc['diff'] + ("\n... [截断]" if len(rc['diff']) >= 2000 else ""),
                "```",
            ]

    # ── 4. Lore 邮件线程（预处理 + 全量输出）─────────────────────────
    if lore_emails:
        # ── 预处理：去噪 + 分级 ──
        processed, preproc_stats = preprocess_emails(lore_emails)

        total_orig = preproc_stats["total"]
        dropped    = preproc_stats["dropped"]
        kept       = len(processed)

        lines += [
            "", SEP,
            f"# Lore 邮件线程（原始 {total_orig} 封"
            f"，过滤 {dropped} 封机器通知"
            f"，保留 {kept} 封）",
            SEP,
        ]
        if preproc_stats["high"] or preproc_stats["medium"]:
            lines.append(
                f"# [HIGH:{preproc_stats['high']} "
                f"MEDIUM:{preproc_stats['medium']} "
                f"LOW:{preproc_stats['low']}]"
            )

        # ── 按优先级从高到低排序，同优先级保持原始时间顺序 ──
        processed.sort(key=lambda e: -e.get("_priority", 1))

        # ── 全量输出（不截断、不丢弃）──
        for em in processed:
            priority = EmailPriority(em.get("_priority", 1))
            body = em.get("body", "")

            # 优先级标记
            prio_tag = {
                EmailPriority.HIGH:   "[讨论]",
                EmailPriority.MEDIUM: "[概述/数据]",
                EmailPriority.LOW:    "[PATCH摘要]",
            }.get(priority, "")

            # 邮件头：From / Date / Message-Id / In-Reply-To
            email_hdr = [
                "",
                f"## {prio_tag} {em.get('subject', '')}",
                f"From : {em.get('from', '')}",
                f"Date : {em.get('date', '')}",
            ]
            if em.get("message_id"):
                email_hdr.append(f"Message-Id : {em['message_id']}")
            if em.get("in_reply_to"):
                email_hdr.append(f"In-Reply-To : {em['in_reply_to']}")
            email_hdr += ["", body]
            lines += email_hdr

            # PATCH 邮件：追加原始 diff 代码（被 strip_patch_diff 去掉的部分）
            if priority == EmailPriority.LOW:
                raw_body = em.get("_raw_body", "")
                if raw_body and raw_body != body:
                    # 提取 diff 部分（body 之后的内容）
                    diff_part = raw_body[len(body):].strip() if raw_body.startswith(body[:50]) else ""
                    if not diff_part:
                        # 退化：找 --- 分隔线后面的内容
                        cut = raw_body.find("\n---\n")
                        if cut != -1:
                            diff_part = raw_body[cut:].strip()
                    if diff_part and len(diff_part) > 20:
                        lines += ["", "```diff", diff_part[:8000], "```"]
    else:
        lines += ["", "# Lore 邮件线程", "（未抓取到邮件讨论）"]
        if commit_info.lore_url:
            lines.append(f"建议访问：{commit_info.lore_url}")

    # ── 5. 分析清单（完整提示词）──────────────────────────────────
    tail_lines = [
        "", SEP,
        "# 分析任务清单",
        SEP,
        "",
        "你是一名 Linux 内核调度器领域的资深工程师，正在为团队撰写一份",
        "技术分析报告。请基于上方提供的 commit 信息、代码 diff 和邮件讨论，",
        "用中文完成以下分析。要求：引用具体的代码行、函数名、邮件原文；",
        "区分事实与推测；如果信息不足请明确标注「信息不足」而非编造。",
        "",
        "---",
        "",
        "## 一、背景与问题（What & Why）",
        "",
        "1. 这个 patchset 要解决的核心问题是什么？",
        "   - 用 1-2 句话概括问题本质",
        "   - 该问题在什么场景下会触发？（workload 类型、拓扑结构、配置条件）",
        "   - 该问题的影响有多严重？（性能回归数据、crash、逻辑错误）",
        "",
        "2. 原有代码的缺陷分析",
        "   - 指出有问题的函数/代码路径",
        "   - 解释为什么原来的实现会导致上述问题",
        "   - 如果是历史遗留问题，简述其演变过程",
        "",
        "## 二、方案分析（How）",
        "",
        "3. 这个 commit 的具体改动",
        "   - 逐个说明改了哪些函数、改了什么",
        "   - 新增/修改的参数、数据结构、调用关系",
        "   - 对调用链上下游的影响",
        "",
        "4. 设计思路与权衡",
        "   - 为什么选择这种实现方式？",
        "   - 讨论中是否出现过替代方案？如果有，为什么被否决？",
        "   - 这个方案有什么已知的局限或 trade-off？",
        "",
        "## 三、邮件讨论精华（Review Insights）",
        "",
        "5. 关键讨论摘要",
        "   - 列出邮件线程中 3-5 个最重要的讨论点",
        "   - 每个讨论点注明：谁提出的、核心观点、最终结论",
        "   - 如果有争议未达成共识，也要记录",
        "",
        "6. Reviewer 的关注点",
        "   - 哪些 reviewer 给出了实质性反馈？",
        "   - 他们关注的风险点是什么？",
        "   - 是否有性能测试数据被引用？结果如何？",
        "",
        "## 四、影响评估（Impact）",
        "",
        "7. 影响范围",
        "   - 涉及哪些子系统/模块？",
        "   - 对哪些场景有正面影响？哪些场景可能有副作用？",
        "   - 是否需要配合其他 patch 一起理解？（patchset 中的上下文）",
        "",
        "8. 后续关注",
        "   - 邮件中提到的尚未解决的问题或 TODO",
        "   - 可能需要的后续 patch",
        "   - 对下游（Android、发行版）的潜在影响",
        "",
        "## 五、一句话总结",
        "",
        "9. 用一句话（不超过 50 字）概括这个 patch 做了什么，适合写在周报里。",
        "",
        "---",
        "",
        "输出格式要求：",
        "- 按上述编号结构输出，保留标题层级",
        "- 引用代码用 `反引号`，引用邮件原文用 > 块引用",
        "- 关键术语首次出现时附英文原文",
        "- 总字数控制在 2000-4000 字",
    ]
    lines += tail_lines

    return "\n".join(lines)


# ───────────────────────────────────────────────────────────────────────────────
# 主程序
# ───────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="贪心预取：commit + patchset + Lore 线程 → OpenClaw 上下文"
    )
    parser.add_argument("commit", help="git commit hash 或引用 (HEAD, HEAD~3, ...)")
    parser.add_argument("--repo", default=KERNEL_REPO, help="本地内核仓库路径")
    parser.add_argument("--full", action="store_true",
                        help="完整模式：抓 Lore 线程 + 同系列 commit（需联网）")
    parser.add_argument("--email-json", default="",
                        help="本地邮件 JSON 文件路径（Lore 无法访问时的降级方案）")
    parser.add_argument("--output", help="输出文件路径（默认自动生成）")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR), help="输出目录")
    args = parser.parse_args()

    print()

    # ── Step 1: 解析 commit ───────────────────────────────────────────
    print(f"[1/4] 解析 commit: {args.commit}")
    ca = CommitAnalyzer(repo_path=args.repo)
    info = ca.analyze(args.commit)
    print(f"  {info.subject}")
    print(f"  作者: {info.author}  日期: {info.date.strftime('%Y-%m-%d')}")
    if info.patch_total:
        ver = f" {info.patch_version}" if info.patch_version else ""
        print(f"  Patchset: [PATCH{ver} {info.patch_index}/{info.patch_total}]")
    if info.lore_url:
        print(f"  Lore: {info.lore_url}")

    # ── Step 2: 获取 diff ─────────────────────────────────────────────
    print(f"\n[2/4] 获取 diff...")
    diff = get_full_diff(args.repo, info.hash or args.commit)
    print(f"  {len(diff)} 字符")

    # ── Step 3: 抓取 Lore 线程 + 同系列 commit ───────────────────────
    lore_emails = []
    related_commits = []

    if args.full:
        if info.lore_url:
            print(f"\n[3/4] 抓取 Lore 线程...")
            lore_emails = fetch_lore_thread(info.lore_url, args.email_json)
            if lore_emails:
                print(f"  获得 {len(lore_emails)} 封邮件")
            else:
                print(f"  ⚠ 线程抓取失败（Lore Anubis 防护/网络不通）")
                if not args.email_json:
                    print(f"  提示: 可用 --email-json <path> 指定本地已保存的邮件")
        else:
            print(f"\n[3/4] 无 Lore 直链，跳过线程抓取")

        print(f"\n[3b] 查找同系列相关 commit...")
        related_commits = get_related_commits(info, args.repo)
        print(f"  找到 {len(related_commits)} 个相关 commit")
    else:
        print(f"\n[3/4] 跳过网络抓取（加 --full 开启）")

    # ── Step 4: 打包 ──────────────────────────────────────────────────
    print(f"\n[4/4] 打包上下文（预处理 + 全量输出）...")
    if lore_emails:
        _, stats = preprocess_emails(lore_emails)
        print(f"  预处理: 总计{stats['total']}封 → "
              f"丢弃{stats['dropped']}封机器通知, "
              f"HIGH:{stats['high']} MEDIUM:{stats['medium']} LOW:{stats['low']}")
    context = build_context(info, diff, lore_emails, related_commits)

    os.makedirs(args.output_dir, exist_ok=True)
    short_hash = (info.hash or args.commit)[:12]
    out_path = args.output or os.path.join(args.output_dir, f"{short_hash}_context_full.txt")

    with open(out_path, "w", encoding="utf-8") as f:
        f.write(context)

    size_kb = len(context) // 1024
    email_count = len(lore_emails)
    print(f"\n  已保存: {out_path}  (完整版，不截断)")
    print(f"  大小: {size_kb} KB  |  邮件: {email_count} 封  |  相关commit: {len(related_commits)} 个")

    # ── 输出调用命令 ──────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("  用法：")
    print("=" * 70)
    print(f"\n  # 完整版直接阅读/翻译：")
    print(f"  cat {out_path}")
    print(f"\n  # 裁剪为 AI 精简版（默认 100K 字符）：")
    print(f"  python trim_context.py {out_path}")
    print(f"\n  # 指定裁剪大小（如 200K 字符）：")
    print(f"  python trim_context.py {out_path} --max-size 200000")
    print(f"\n  # 直接传给 OpenClaw：")
    print(f"  openclaw chat --file {out_path}")
    print()


if __name__ == "__main__":
    main()