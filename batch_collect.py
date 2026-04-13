#!/usr/bin/env python3
"""batch_collect.py — 批量采集内核邮件并入知识库

三阶段过滤流程：
  1. 粗筛：用 lore.kernel.org 按子系统+时间范围搜索
  2. 规则预筛：subject/body 关键词匹配
  3. AI 精筛：DeepSeek 判断语义相关性

用法:
    python batch_collect.py \
      --keywords "latency,QoS,deadline,SCHED_DEADLINE,latency-nice,interactive" \
      --date-from 2017-01-01 --date-to 2018-12-31 \
      --list linux-kernel --max-emails 5000 \
      --api-key sk-xxx --api-provider deepseek

    # 仅粗筛+规则预筛（不调用 AI，省钱调试用）
    python batch_collect.py \
      --keywords "latency,QoS" --date-from 2017-01-01 --date-to 2018-12-31 \
      --no-ai

    # 跳过粗筛，只对已入库邮件做 AI 精筛
    python batch_collect.py --ai-filter-only --api-key sk-xxx
"""

import argparse
import json
import logging
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).parent))

from email_translator.lore_client import LoreClient
from email_translator.lore_thread_fetcher import LoreThreadFetcher
from email_translator.thread_builder import build_threads
from email_translator.knowledge_db import KnowledgeDB
from email_translator.config import EMAILS_DIR

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ======================================================================
# 规则预筛
# ======================================================================

# 默认的关键词列表（scheduler 延迟/QoS 相关）
DEFAULT_RELEVANCE_KEYWORDS = [
    "latency", "latency-nice", "latency_nice",
    "deadline", "SCHED_DEADLINE", "sched_deadline",
    "QoS", "quality of service",
    "interactive", "responsiveness", "response time",
    "latency sensitive", "latency-sensitive",
    "real-time", "realtime", "RT",
    "preempt", "preemption",
    "nice", "autogroup",
    "idle", "wakeup", "wake_up", "wake-up",
]


def rule_filter(email: Dict, keywords: List[str]) -> bool:
    """规则预筛：subject 或 body 前 2000 字符中包含任一关键词。"""
    text = (email.get("subject", "") + " " + email.get("body", "")[:2000]).lower()
    return any(kw.lower() in text for kw in keywords)


# ======================================================================
# AI 精筛
# ======================================================================

_AI_PROMPT_TEMPLATE = """你是一个 Linux 内核专家。判断下面这封内核邮件是否与以下话题**直接相关**：
「scheduler 延迟敏感调度 / 调度 QoS / 调度延迟优化 / SCHED_DEADLINE / latency-nice / 交互式任务调度优化」

规则：
- 仅当邮件**主要讨论或直接涉及**上述话题时回答 YES
- 只是提到调度器但不涉及延迟/QoS 的，回答 NO
- 只回答 YES 或 NO，然后用一句话说明理由

Subject: {subject}
From: {from_addr}
Body (前800字):
{body}
"""


def ai_filter_single(email: Dict, api_caller) -> Tuple[bool, str, float]:
    """用 AI 判断单封邮件的相关性。返回 (relevant, reason, score)。"""
    prompt = _AI_PROMPT_TEMPLATE.format(
        subject=email.get("subject", ""),
        from_addr=email.get("from", ""),
        body=email.get("body", "")[:800],
    )

    text, error = api_caller._call(prompt)
    if error:
        logger.warning("AI 调用失败: %s (message_id=%s)", error, email.get("message_id", ""))
        return False, f"AI_ERROR: {error}", 0

    text = text.strip()
    first_line = text.split("\n")[0].strip().upper()
    reason = text.split("\n")[-1].strip() if "\n" in text else text

    if first_line.startswith("YES"):
        return True, reason, 1.0
    else:
        return False, reason, 0.0


def ai_filter_batch(emails: List[Dict], api_caller,
                     workers: int = 4) -> List[Dict]:
    """并发对多封邮件做 AI 精筛。返回标注了 relevance_score/reason 的邮件列表。"""
    results = []
    total = len(emails)

    def _process(idx_email):
        idx, em = idx_email
        relevant, reason, score = ai_filter_single(em, api_caller)
        em["relevance_score"] = score
        em["relevance_reason"] = reason
        return idx, em, relevant

    logger.info("开始 AI 精筛 %d 封邮件 (workers=%d)...", total, workers)

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(_process, (i, em)): i
                   for i, em in enumerate(emails)}

        done = 0
        relevant_count = 0
        for future in as_completed(futures):
            done += 1
            idx, em, relevant = future.result()
            if relevant:
                relevant_count += 1
                results.append(em)
            if done % 50 == 0 or done == total:
                logger.info("  AI 精筛进度: %d/%d, 相关: %d", done, total, relevant_count)
            # 限速：避免 API 过载
            time.sleep(0.1)

    logger.info("AI 精筛完成: %d/%d 封相关", len(results), total)
    return results


# ======================================================================
# 主流程
# ======================================================================

def run_collect(args):
    db = KnowledgeDB()

    # 解析关键词
    keywords = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]
    filter_keywords = keywords + DEFAULT_RELEVANCE_KEYWORDS

    # 创建采集任务
    job_id = db.create_job(
        keywords=args.keywords,
        date_from=args.date_from,
        date_to=args.date_to,
        list_name=args.list,
    )
    logger.info("创建采集任务 #%d: keywords=%s, %s ~ %s",
                job_id, args.keywords, args.date_from, args.date_to)

    # ── 第1步：粗筛 ──────────────────────────────────────────────────
    logger.info("="*60)
    logger.info("第1步: 粗筛 - 从 lore.kernel.org 搜索邮件...")
    logger.info("="*60)

    client = LoreClient(timeout=30, delay=1.5)
    all_emails = []

    # 用每个关键词分别搜索，合并去重
    seen_mids = set()
    for kw in keywords:
        logger.info("  搜索关键词: %r", kw)
        batch = client.search_emails(
            topic=kw,
            list_name=args.list,
            max_emails=args.max_emails,
            date_from=args.date_from,
            date_to=args.date_to,
        )
        for em in batch:
            mid = em.get("message_id", "")
            if mid and mid not in seen_mids and not db.email_exists(mid):
                seen_mids.add(mid)
                all_emails.append(em)
        logger.info("  找到 %d 封 (去重后累计 %d)", len(batch), len(all_emails))
        time.sleep(1)

    logger.info("粗筛完成: 共 %d 封新邮件", len(all_emails))
    db.update_job(job_id, total_found=len(all_emails))

    if not all_emails:
        logger.info("无新邮件，结束。")
        db.update_job(job_id, status="done")
        return

    # ── 第2步：规则预筛 ──────────────────────────────────────────────
    logger.info("="*60)
    logger.info("第2步: 规则预筛 - 关键词匹配...")
    logger.info("="*60)

    pre_filtered = [em for em in all_emails if rule_filter(em, filter_keywords)]
    logger.info("规则预筛: %d/%d 封通过", len(pre_filtered), len(all_emails))

    # ── 第3步：AI 精筛 ──────────────────────────────────────────────
    if args.no_ai:
        logger.info("跳过 AI 精筛 (--no-ai)")
        relevant_emails = pre_filtered
        for em in relevant_emails:
            em["relevance_score"] = 0.5
            em["relevance_reason"] = "rule_match_only"
    else:
        logger.info("="*60)
        logger.info("第3步: AI 精筛 - DeepSeek 判断相关性...")
        logger.info("="*60)

        from email_translator.translator import APITranslator
        api_caller = APITranslator(
            api_key=args.api_key,
            provider=args.api_provider,
            model=args.model or "",
            timeout=30,
        )
        relevant_emails = ai_filter_batch(
            pre_filtered, api_caller, workers=args.workers
        )

    db.update_job(job_id, total_relevant=len(relevant_emails))

    if not relevant_emails:
        logger.info("无相关邮件，结束。")
        db.update_job(job_id, status="done")
        return

    # ── 第4步：下载完整线程并入库 ────────────────────────────────────
    logger.info("="*60)
    logger.info("第4步: 下载完整线程并入库 (%d 封相关邮件)...", len(relevant_emails))
    logger.info("="*60)

    # 按 thread 去重：同一线程只下载一次
    thread_roots = {}  # message_id -> email
    for em in relevant_emails:
        mid = em.get("message_id", "")
        # 找线程根：优先用 references 的第一个，否则用自身
        refs = em.get("references", [])
        root_mid = refs[0] if refs else mid
        if root_mid not in thread_roots:
            thread_roots[root_mid] = em

    logger.info("去重后需下载 %d 个线程", len(thread_roots))

    fetcher = LoreThreadFetcher(timeout=20, max_retries=2)
    total_new = 0

    for i, (root_mid, root_email) in enumerate(thread_roots.items(), 1):
        source_url = root_email.get("source_url", "")
        logger.info("  [%d/%d] 下载线程: %s", i, len(thread_roots),
                     root_email.get("subject", "")[:60])

        thread_emails = []
        if source_url:
            try:
                thread_emails = fetcher.fetch_by_url(source_url.strip())
            except Exception as e:
                logger.warning("    线程下载失败: %s", e)

        if not thread_emails:
            # 降级：只存当前这封邮件
            thread_emails = [root_email]

        # 存入知识库
        for em in thread_emails:
            em["relevance_score"] = root_email.get("relevance_score", 0)
            em["relevance_reason"] = root_email.get("relevance_reason", "")

        new, skip = db.insert_emails_bulk(thread_emails)
        total_new += new
        if new:
            logger.info("    入库 %d 封 (跳过 %d 封已存在)", new, skip)

        # 构建线程关系
        if len(thread_emails) > 1:
            threads = build_threads(thread_emails)
            for t in threads:
                td = t.to_dict()
                db.upsert_thread({
                    "id": td["root"]["message_id"],
                    "root_message_id": td["root"]["message_id"],
                    "subject": td["root"]["subject"],
                    "start_date": td["date_range"][0],
                    "end_date": td["date_range"][1],
                    "email_count": 1 + len(td["replies"]),
                    "participant_count": len(td["participants"]),
                })

        # 保存原始 JSON
        safe_topic = args.keywords.replace(",", "_")[:30]
        json_dir = EMAILS_DIR / f"kb_{safe_topic}"
        json_dir.mkdir(parents=True, exist_ok=True)
        for em in thread_emails:
            mid_safe = re.sub(r'[<>@/\\]', '_', em.get("message_id", "unknown"))[:80]
            json_path = json_dir / f"{mid_safe}.json"
            if not json_path.exists():
                json_path.write_text(
                    json.dumps(em, ensure_ascii=False, indent=2), encoding="utf-8"
                )

        time.sleep(1.5)  # 对 lore 限速

    logger.info("="*60)
    logger.info("采集完成！新增 %d 封邮件入库", total_new)
    logger.info("知识库统计: %s", db.stats())
    logger.info("="*60)
    db.update_job(job_id, status="done")


def main():
    parser = argparse.ArgumentParser(
        description="批量采集内核邮件入知识库 (粗筛→规则预筛→AI精筛→下载线程→入库)"
    )
    parser.add_argument("--keywords", required=True,
                        help="搜索关键词，逗号分隔 (如 latency,QoS,SCHED_DEADLINE)")
    parser.add_argument("--date-from", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--date-to", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--list", default="all",
                        help="邮件列表 (默认 all, 可选 linux-kernel, linux-mm 等)")
    parser.add_argument("--max-emails", type=int, default=2000,
                        help="每个关键词最大搜索数 (默认 2000)")

    # AI 精筛参数
    parser.add_argument("--api-key", default="", help="API 密钥 (AI精筛用)")
    parser.add_argument("--api-provider", default="deepseek",
                        help="API 服务商 (默认 deepseek)")
    parser.add_argument("--model", default="", help="模型名 (留空用默认)")
    parser.add_argument("--workers", type=int, default=4,
                        help="AI 精筛并发数 (默认 4)")
    parser.add_argument("--no-ai", action="store_true",
                        help="跳过 AI 精筛，只用规则预筛")

    parser.add_argument("--proxy", default="", help="代理地址 (如 127.0.0.1:7897)")

    args = parser.parse_args()

    if not args.no_ai and not args.api_key:
        # 尝试从 config.json 读取
        config_path = Path(__file__).parent / "config.json"
        if config_path.exists():
            cfg = json.loads(config_path.read_text())
            args.api_key = cfg.get("api_key", "")
            args.api_provider = cfg.get("api_provider", args.api_provider)
            args.model = args.model or cfg.get("model", "")

        if not args.api_key:
            logger.error("AI 精筛需要 --api-key 或 config.json 中的 api_key。使用 --no-ai 可跳过。")
            sys.exit(1)

    run_collect(args)


if __name__ == "__main__":
    main()