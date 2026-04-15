#!/usr/bin/env python3
"""batch_collect.py — 批量采集内核邮件并入知识库

支持两种过滤模式：
  方案B (话题配置驱动):  --topic-config topics/sched_latency.json
    → 从配置文件读取 搜索词+黑名单+过滤关键词+AI prompt
  方案C (纯 AI 精筛):    --keywords "fair sleeper,vruntime" (不加 --topic-config)
    → 只用 lore 搜索 + AI 精筛，不做规则预筛

用法:
    # 方案B: 话题配置驱动 (前期推荐)
    python batch_collect.py \
      --topic-config topics/sched_latency.json \
      --date-from 2006-01-01 --date-to 2010-12-31 \
      --max-threads 100

    # 方案C: 纯 AI 精筛 (后续稳定后用)
    python batch_collect.py \
      --keywords "fair sleeper,latency nice,SCHED_DEADLINE" \
      --date-from 2006-01-01 --date-to 2010-12-31 \
      --max-threads 100 --ai-only

    # 跳过所有 AI (调试用)
    python batch_collect.py \
      --topic-config topics/sched_latency.json \
      --date-from 2006-01-01 --date-to 2010-12-31 --no-ai
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
# 话题配置加载
# ======================================================================

class TopicConfig:
    """话题配置：从 JSON 文件加载搜索词、过滤关键词、黑名单、AI prompt。"""

    def __init__(self, config_path: str = None):
        self.name = ""
        self.display_name = ""
        self.description = ""
        self.search_keywords = []     # lore 搜索用
        self.filter_keywords = []     # 规则预筛用 (旧模式: 任意匹配)
        self.filter_require_subsystem = []  # 双层过滤: 子系统域词
        self.filter_require_topic = []      # 双层过滤: 话题域词
        self.subject_blacklist = []   # 标题黑名单（正则列表）
        self.ai_filter_prompt = ""    # AI 精筛 prompt 模板
        self.ai_summary_extra = ""    # AI 摘要补充提示

        if config_path:
            self._load(config_path)

    def _load(self, path: str):
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"话题配置文件不存在: {path}")
        cfg = json.loads(p.read_text(encoding="utf-8"))
        self.name = cfg.get("name", p.stem)
        self.display_name = cfg.get("display_name", self.name)
        self.description = cfg.get("description", "")
        self.search_keywords = cfg.get("search_keywords", [])
        self.filter_keywords = cfg.get("filter_keywords", [])
        self.filter_require_subsystem = cfg.get("filter_require_subsystem", [])
        self.filter_require_topic = cfg.get("filter_require_topic", [])
        self.subject_blacklist = cfg.get("subject_blacklist", [])
        self.ai_filter_prompt = cfg.get("ai_filter_prompt", "")
        self.ai_summary_extra = cfg.get("ai_summary_extra", "")
        mode = "双层" if self.filter_require_subsystem else "关键词"
        logger.info("加载话题配置: %s (%d 搜索词, 过滤模式=%s, %d 黑名单)",
                    self.display_name, len(self.search_keywords),
                    mode, len(self.subject_blacklist))

    def compile_blacklist(self):
        """编译黑名单正则。"""
        if not self.subject_blacklist:
            return None
        pattern = "|".join(self.subject_blacklist)
        return re.compile(pattern, re.IGNORECASE)


# ======================================================================
# 规则预筛 (方案B)
# ======================================================================

# 硬编码的通用标题黑名单 —— 这些模式在任何话题下都不应入库
# 话题配置文件的 subject_blacklist 会叠加在此基础上
_BUILTIN_SUBJECT_BLACKLIST = [
    # --- Git Pull / 合并请求 ---
    r"\[GIT PULL\]", r"\[git pull\]", r"GIT PULL", r"git pull",
    # --- Stable 补丁集 ---
    r"\d+\.\d+\.\d+-stable review",        # "4.9.71-stable review"
    r"\[PATCH \d+\.\d+ ",                   # "[PATCH 4.9 000/177]"
    r"AUTOSEL",                             # "[PATCH AUTOSEL for ..."
    # --- 通用大型合集 / 噪音 ---
    r"\[PULL\]", r"\[pull\]",
    r"linux-next:", r"mmotm ",
    r"active bugs in .* merge window",
]
_BUILTIN_BLACKLIST_RE = re.compile(
    "|".join(_BUILTIN_SUBJECT_BLACKLIST), re.IGNORECASE
)


def subject_blacklist_match(subject: str, blacklist_re) -> bool:
    """返回 True 表示该邮件应被跳过（内置黑名单 + 话题黑名单命中）。"""
    if not subject:
        return False
    # 先检查内置黑名单
    if _BUILTIN_BLACKLIST_RE.search(subject):
        return True
    # 再检查话题级黑名单
    if blacklist_re and blacklist_re.search(subject):
        return True
    return False


def rule_filter(email: Dict, keywords: List[str], blacklist_re=None,
                require_subsystem: List[str] = None,
                require_topic: List[str] = None) -> bool:
    """规则预筛：先过黑名单，再检查关键词。

    双层过滤模式 (当 require_subsystem + require_topic 均非空时):
      text 必须同时包含至少一个 subsystem 域词 + 至少一个 topic 域词。
    兼容模式 (无双层配置时): 任意匹配 keywords 中一个即可。
    """
    subject = email.get("subject", "")
    if subject_blacklist_match(subject, blacklist_re):
        return False
    text = (subject + " " + email.get("body", "")[:2000]).lower()

    # 双层过滤: 必须同时命中 subsystem 域 + topic 域
    if require_subsystem and require_topic:
        has_subsystem = any(kw.lower() in text for kw in require_subsystem)
        has_topic = any(kw.lower() in text for kw in require_topic)
        return has_subsystem and has_topic

    # 兼容旧模式: 任意匹配一个关键词
    return any(kw.lower() in text for kw in keywords)


# ======================================================================
# AI 精筛
# ======================================================================

_DEFAULT_AI_PROMPT = """你是一个 Linux 内核专家。判断下面这封内核邮件是否与以下话题**直接相关**：
「{topic_desc}」

规则：
- 仅当邮件**主要讨论或直接涉及**上述话题时回答 YES
- 只是提到相关词汇但不涉及核心讨论的，回答 NO
- 只回答 YES 或 NO，然后用一句话说明理由

Subject: {subject}
From: {from_addr}
Body (前800字):
{body}
"""


def ai_filter_single(email: Dict, api_caller,
                      prompt_template: str = "",
                      topic_desc: str = "") -> Tuple[bool, str, float]:
    """用 AI 判断单封邮件的相关性。返回 (relevant, reason, score)。"""
    tmpl = prompt_template or _DEFAULT_AI_PROMPT
    prompt = tmpl + "\n\nSubject: {subject}\nFrom: {from_addr}\nBody (前800字):\n{body}"
    # 如果 prompt_template 已包含占位符则直接用
    if "{subject}" in tmpl:
        prompt = tmpl

    prompt = prompt.format(
        topic_desc=topic_desc,
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
                     workers: int = 4,
                     prompt_template: str = "",
                     topic_desc: str = "") -> List[Dict]:
    """并发对多封邮件做 AI 精筛。返回标注了 relevance_score/reason 的邮件列表。"""
    results = []
    total = len(emails)

    def _process(idx_email):
        idx, em = idx_email
        relevant, reason, score = ai_filter_single(
            em, api_caller, prompt_template, topic_desc)
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
            try:
                idx, em, relevant = future.result()
                if relevant:
                    relevant_count += 1
                    results.append(em)
            except Exception as e:
                logger.warning("  AI 精筛异常: %s", e)
            if done % 50 == 0 or done == total:
                logger.info("  AI 精筛进度: %d/%d, 相关: %d", done, total, relevant_count)
            time.sleep(0.1)

    logger.info("AI 精筛完成: %d/%d 封相关", len(results), total)
    return results


# ======================================================================
# 主流程
# ======================================================================

def run_collect(args):
    """兼容入口：已切换到流式并行实现。"""
    return run_collect_v2(args)

    # ===== 以下为历史实现（保留作回滚参考，不再执行） =====
    db = KnowledgeDB()

    # 加载话题配置 (方案B) 或使用 CLI 关键词 (方案C)
    topic_cfg = None
    if args.topic_config:
        topic_cfg = TopicConfig(args.topic_config)
        keywords = topic_cfg.search_keywords
        filter_keywords = topic_cfg.filter_keywords
        blacklist_re = topic_cfg.compile_blacklist()
        ai_prompt = topic_cfg.ai_filter_prompt
        topic_desc = topic_cfg.description
        topic_name = topic_cfg.name
    else:
        keywords = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]
        filter_keywords = keywords  # 方案C: 搜索词就是过滤词
        blacklist_re = None
        ai_prompt = ""
        topic_desc = ", ".join(keywords)
        topic_name = args.keywords.replace(",", "_")[:30]

    if not keywords:
        logger.error("必须提供 --topic-config 或 --keywords")
        sys.exit(1)

    # 检查是否有未完成的任务（用于断点续传或持续运行）
    existing_job = None
    if args.resume or args.continuous:
        existing_job = db.conn.execute(
            "SELECT * FROM collect_jobs WHERE keywords = ? AND status != 'done' "
            "ORDER BY id DESC LIMIT 1",
            (",".join(keywords),)
        ).fetchone()
    
    if existing_job and (args.resume or args.continuous):
        job_id = existing_job["id"]
        logger.info("继续采集任务 #%d: %s, %s ~ %s",
                    job_id, topic_name, existing_job["date_from"], existing_job["date_to"])
        
        # 如果是持续运行，更新日期范围为当前到结束日期
        if args.continuous:
            db.update_job(job_id, date_to=args.date_to)
    else:
        # 创建新的采集任务
        job_id = db.create_job(
            keywords=",".join(keywords),
            date_from=args.date_from,
            date_to=args.date_to,
            list_name=args.list,
        )
        logger.info("创建采集任务 #%d: %s, %s ~ %s",
                    job_id, topic_name, args.date_from, args.date_to)
    
    # 如果指定了断点续传，检查是否有未完成的队列
    if (args.resume or args.continuous) and db.get_queue_stats(job_id)["total"] > 0:
        logger.info("检测到未完成的采集队列，继续执行...")
        # 重置下载中状态为待处理
        db.reset_queue_status(job_id, "pending")
        run_download_workers(db, job_id, args, topic_name)
        return
    
    # 清空旧队列（如果不是断点续传）
    if not (args.resume or args.continuous):
        with db.conn:
            db.conn.execute("DELETE FROM collect_queue WHERE job_id = ?", (job_id,))

    # ── 第1步：粗筛（并发搜索多个关键词）─────────────────────────────
    logger.info("=" * 60)
    logger.info("第1步: 粗筛 - 从 lore.kernel.org 并发搜索邮件...")
    logger.info("=" * 60)

    # 流式并行：生产者(搜索+过滤+入队) 与 消费者(下载入库)并行
    import threading
    all_emails = []
    seen_mids = set()
    search_lock = threading.Lock()
    queue_lock = threading.Lock()

    # 读取/维护每关键词独立断点
    job_row = db.conn.execute(
        "SELECT progress FROM collect_jobs WHERE id = ?", (job_id,)
    ).fetchone()
    progress_map = {}
    if job_row and job_row[0]:
        try:
            progress_map = json.loads(job_row[0])
        except Exception:
            progress_map = {}

    # 预算：本轮最多入队线程数（0=不限制）
    thread_budget = args.max_threads if args.max_threads > 0 else 0
    queued_threads = [0]
    found_emails = [0]
    relevant_emails = [0]

    # 启动消费者线程（常驻拉队列）
    producer_done = threading.Event()
    consumer_thread = threading.Thread(
        target=run_download_workers,
        args=(db, job_id, args, topic_name, producer_done, True),
        daemon=True,
    )
    consumer_thread.start()

    # AI 调用器（按需初始化）
    api_caller = None
    if not args.no_ai:
        from email_translator.translator import APITranslator
        api_caller = APITranslator(
            api_key=args.api_key,
            provider=args.api_provider,
            model=args.model or "",
            timeout=30,
        )

    def process_and_enqueue(batch: List[Dict]) -> int:
        """对单批搜索结果进行过滤并入队，返回本批入队线程数。"""
        if not batch:
            return 0

        # 规则预筛
        if args.ai_only:
            pre_filtered = batch
        else:
            pre_filtered = [em for em in batch if rule_filter(em, filter_keywords, blacklist_re)]

        # AI 精筛
        if args.no_ai:
            relevant = pre_filtered
            for em in relevant:
                em["relevance_score"] = em.get("relevance_score", 0.5) or 0.5
                em["relevance_reason"] = em.get("relevance_reason", "rule_match_only")
        else:
            relevant = ai_filter_batch(
                pre_filtered,
                api_caller,
                workers=max(1, min(args.workers, 4)),
                prompt_template=ai_prompt,
                topic_desc=topic_desc,
            )

        relevant_emails[0] += len(relevant)

        # 按 thread root 去重后入队
        thread_roots = {}
        for em in relevant:
            mid = em.get("message_id", "")
            refs = em.get("references", [])
            root_mid = refs[0] if refs else mid
            if root_mid and root_mid not in thread_roots:
                thread_roots[root_mid] = em

        added = 0
        with queue_lock:
            for _, root_email in thread_roots.items():
                if thread_budget and queued_threads[0] >= thread_budget:
                    break
                if db.add_to_queue(job_id, root_email, priority=root_email.get("relevance_score", 0)):
                    queued_threads[0] += 1
                    added += 1
        return added


    def search_one_keyword(kw):
        logger.info("  搜索关键词: %r", kw)
        kw_client = LoreClient(timeout=30, delay=1.5)
        
        # 检查是否有断点续传的搜索进度
        last_search_time = db.conn.execute(
            "SELECT last_search_time FROM collect_jobs WHERE id = ?", (job_id,)
        ).fetchone()[0] or args.date_from
        
        batch = kw_client.search_emails(
            topic=kw,
            list_name=args.list,
            max_emails=args.max_emails,
            date_from=last_search_time,
            date_to=args.date_to,
            timeout=600,  # 10分钟超时保护
        )
        added = 0
        with search_lock:
            for em in batch:
                mid = em.get("message_id", "")
                if mid and mid not in seen_mids:
                    seen_mids.add(mid)
                    all_emails.append(em)
                    added += 1
        logger.info("  关键词 %r: 找到 %d 封, 去重后新增 %d", kw, len(batch), added)
        
        # 更新搜索进度
        if batch:
            latest_date = max(em.get("date", "") for em in batch)
            db.update_job(job_id, last_search_time=latest_date)

    search_workers = min(len(keywords), args.workers)
    with ThreadPoolExecutor(max_workers=search_workers) as pool:
        futures = [pool.submit(search_one_keyword, kw) for kw in keywords]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                logger.error("  搜索异常: %s", e)

    logger.info("粗筛完成: 共 %d 封新邮件", len(all_emails))
    db.update_job(job_id, total_found=len(all_emails))

    if not all_emails:
        logger.info("无新邮件，结束。")
        db.update_job(job_id, status="done")
        return

    # ── 第2步：预筛 ──────────────────────────────────────────────────
    if args.ai_only:
        # 方案C: 跳过规则预筛，全部交给 AI
        logger.info("方案C模式: 跳过规则预筛，全部 %d 封交给 AI", len(all_emails))
        pre_filtered = all_emails
    else:
        # 方案B: 标题黑名单 + 关键词预筛
        logger.info("=" * 60)
        logger.info("第2步: 规则预筛 - 黑名单 + 关键词匹配...")
        logger.info("=" * 60)

        pre_filtered = [em for em in all_emails
                        if rule_filter(em, filter_keywords, blacklist_re)]

        blacklisted = len(all_emails) - len([
            em for em in all_emails
            if not subject_blacklist_match(em.get("subject", ""), blacklist_re)
        ])
        logger.info("规则预筛: %d/%d 封通过 (黑名单排除 %d)",
                    len(pre_filtered), len(all_emails), blacklisted)

    # ── 第3步：AI 精筛 ──────────────────────────────────────────────
    if args.no_ai:
        logger.info("跳过 AI 精筛 (--no-ai)")
        relevant_emails = pre_filtered
        for em in relevant_emails:
            em["relevance_score"] = 0.5
            em["relevance_reason"] = "rule_match_only"
    else:
        logger.info("=" * 60)
        logger.info("第3步: AI 精筛 (%d 封候选)...", len(pre_filtered))
        logger.info("=" * 60)

        from email_translator.translator import APITranslator
        api_caller = APITranslator(
            api_key=args.api_key,
            provider=args.api_provider,
            model=args.model or "",
            timeout=30,
        )
        relevant_emails = ai_filter_batch(
            pre_filtered, api_caller, workers=args.workers,
            prompt_template=ai_prompt, topic_desc=topic_desc,
        )

    db.update_job(job_id, total_relevant=len(relevant_emails))

    if not relevant_emails:
        logger.info("无相关邮件，结束。")
        db.update_job(job_id, status="done")
        return

    # ── 第4步：按 thread 去重，添加到采集队列 ────────────────────────
    thread_roots = {}
    for em in relevant_emails:
        mid = em.get("message_id", "")
        refs = em.get("references", [])
        root_mid = refs[0] if refs else mid
        if root_mid not in thread_roots:
            thread_roots[root_mid] = em

    # --max-threads 截断
    max_threads = args.max_threads
    if max_threads and len(thread_roots) > max_threads:
        logger.info("线程数 %d > --max-threads %d，截断", len(thread_roots), max_threads)
        items = list(thread_roots.items())[:max_threads]
        thread_roots = dict(items)

    # 添加到采集队列
    logger.info("=" * 60)
    logger.info("第4步: 将 %d 个线程添加到采集队列...", len(thread_roots))
    logger.info("=" * 60)
    
    added_count = 0
    for root_mid, root_email in thread_roots.items():
        if db.add_to_queue(job_id, root_email, priority=root_email.get("relevance_score", 0)):
            added_count += 1
    
    logger.info("已添加 %d/%d 个线程到采集队列", added_count, len(thread_roots))
    db.update_job(job_id, total_relevant=added_count)
    
    # 运行下载工作进程
    run_download_workers(db, job_id, args, topic_name)

    # JSON 保存目录
    json_dir = EMAILS_DIR / f"kb_{topic_name}"
    json_dir.mkdir(parents=True, exist_ok=True)

    db_lock = threading.Lock()
    total_new = 0
    done_count = [0]
    total_threads = len(thread_roots)

    def download_one_thread(item):
        nonlocal total_new
        root_mid, root_email = item
        source_url = root_email.get("source_url", "")

        fetcher = LoreThreadFetcher(timeout=20, max_retries=2)

        thread_emails = []
        if source_url:
            try:
                thread_emails = fetcher.fetch_by_url(source_url.strip())
            except Exception as e:
                logger.warning("    线程下载失败: %s", e)

        if not thread_emails:
            thread_emails = [root_email]

        thread_id = root_email.get("message_id", "")
        for em in thread_emails:
            em["relevance_score"] = root_email.get("relevance_score", 0)
            em["relevance_reason"] = root_email.get("relevance_reason", "")
            em["thread_id"] = thread_id

        # 构建线程树（多封邮件时）
        thread_objs = []
        if len(thread_emails) > 1:
            try:
                thread_objs = build_threads(thread_emails)
            except Exception as e:
                logger.warning("    线程构建失败: %s", e)

        with db_lock:
            new, skip = db.insert_emails_bulk(thread_emails)
            total_new += new
            done_count[0] += 1
            idx = done_count[0]

            thread_created = False
            for t in thread_objs:
                td = t.to_dict()
                # 始终以 thread_id 作为 thread id，保持与 email.thread_id 一致
                db.upsert_thread({
                    "id": thread_id,
                    "root_message_id": thread_id,
                    "subject": td["root"]["subject"],
                    "start_date": td["date_range"][0],
                    "end_date": td["date_range"][1],
                    "email_count": 1 + len(td["replies"]),
                    "participant_count": len(td["participants"]),
                })
                thread_created = True
                break

            # 即使只有1封邮件或 build_threads 失败，也要创建 thread 记录
            if not thread_created:
                first_date = thread_emails[0].get("date", "")
                db.upsert_thread({
                    "id": thread_id,
                    "root_message_id": thread_id,
                    "subject": root_email.get("subject", ""),
                    "start_date": first_date,
                    "end_date": first_date,
                    "email_count": len(thread_emails),
                    "participant_count": 1,
                })

        if new:
            logger.info("  [%d/%d] 入库 %d 封 (跳过 %d): %s",
                        idx, total_threads, new, skip,
                        root_email.get("subject", "")[:50])
        else:
            logger.info("  [%d/%d] 跳过 (已存在): %s",
                        idx, total_threads,
                        root_email.get("subject", "")[:50])

        for em in thread_emails:
            mid_safe = re.sub(r'[<>@/\\]', '_', em.get("message_id", "unknown"))[:80]
            json_path = json_dir / f"{mid_safe}.json"
            if not json_path.exists():
                try:
                    json_path.write_text(
                        json.dumps(em, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                except Exception:
                    pass

        return new

    THREAD_TIMEOUT = 60
    pending = list(thread_roots.items())
    futures = {}

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for item in pending:
            f = pool.submit(download_one_thread, item)
            futures[f] = item

        done = 0
        while futures:
            done_futures = []
            for f in list(futures.keys()):
                try:
                    f.result(timeout=THREAD_TIMEOUT)
                    done_futures.append(f)
                except TimeoutError:
                    root_mid, root_email = futures[f]
                    logger.warning("  下载超时 (>%ds): %s",
                                   THREAD_TIMEOUT,
                                   root_email.get("subject", "")[:50])
                    done_futures.append(f)
                except Exception as e:
                    root_mid, root_email = futures[f]
                    logger.error("  下载异常: %s — %s",
                                root_email.get("subject", "")[:50], e)
                    done_futures.append(f)

            for f in done_futures:
                del futures[f]
                done += 1
                if done >= len(pending) - 2:
                    for remaining in futures:
                        try:
                            remaining.cancel()
                        except Exception:
                            pass
                    break

    logger.info("=" * 60)
    logger.info("采集完成！新增 %d 封邮件入库", total_new)
    logger.info("知识库统计: %s", db.stats())
    logger.info("=" * 60)
    db.update_job(job_id, status="done")


def run_download_workers(db: KnowledgeDB, job_id: int, args, topic_name: str):
    """运行下载工作进程（消费者）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading
    
    json_dir = EMAILS_DIR / f"kb_{topic_name}"
    json_dir.mkdir(parents=True, exist_ok=True)
    
    db_lock = threading.Lock()
    total_new = [0]  # 使用列表以便在闭包中修改
    
    def download_one_thread(queue_item: Dict):
        """下载单个线程"""
        queue_id = queue_item["id"]
        root_mid = queue_item["root_message_id"]
        subject = queue_item["subject"]
        source_url = queue_item["source_url"]
        
        # 更新状态为下载中
        db.update_queue_status(queue_id, "downloading")
        
        try:
            fetcher = LoreThreadFetcher(timeout=30, max_retries=2)
            
            thread_emails = []
            if source_url:
                try:
                    thread_emails = fetcher.fetch_by_url(source_url.strip())
                except Exception as e:
                    logger.warning("线程下载失败: %s - %s", subject[:50], e)
            
            if not thread_emails:
                # 降级到单封邮件
                thread_emails = [{
                    "message_id": root_mid,
                    "subject": subject,
                    "source_url": source_url,
                    "relevance_score": queue_item["relevance_score"],
                    "relevance_reason": queue_item["relevance_reason"],
                }]
            
            # 设置线程ID和元数据
            thread_id = root_mid
            for em in thread_emails:
                em["relevance_score"] = queue_item["relevance_score"]
                em["relevance_reason"] = queue_item["relevance_reason"]
                em["thread_id"] = thread_id
            
            # 构建线程对象
            thread_objs = []
            if len(thread_emails) > 1:
                try:
                    thread_objs = build_threads(thread_emails)
                except Exception as e:
                    logger.warning("线程构建失败: %s", e)
            
            # 入库
            with db_lock:
                new, skip = db.insert_emails_bulk(thread_emails)
                total_new[0] += new
                
                for t in thread_objs:
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
            
            # 保存JSON文件
            for em in thread_emails:
                mid_safe = re.sub(r'[<>@/\\\\]', '_', em.get("message_id", "unknown"))[:80]
                json_path = json_dir / f"{mid_safe}.json"
                if not json_path.exists():
                    try:
                        json_path.write_text(
                            json.dumps(em, ensure_ascii=False, indent=2), encoding="utf-8"
                        )
                    except Exception:
                        pass
            
            # 更新状态为完成
            db.update_queue_status(queue_id, "completed")
            
            logger.info("  线程完成: %s (新增 %d 封)", subject[:50], new)
            return new
            
        except Exception as e:
            logger.error("  线程处理异常: %s - %s", subject[:50], e)
            # 更新状态为失败，并增加重试次数
            retry_count = queue_item.get("retry_count", 0) + 1
            db.update_queue_status(queue_id, "failed", retry_count)
            return 0
    
    def retry_failed_items():
        """重试失败的队列项目"""
        failed_items = db.get_failed_queue_items(job_id, max_retries=3)
        if failed_items:
            logger.info("重试 %d 个失败的线程...", len(failed_items))
            for item in failed_items:
                db.update_queue_status(item["id"], "pending")
    
    # 主下载循环
    logger.info("=" * 60)
    logger.info("开始并发下载线程 (workers=%d)...", args.workers)
    logger.info("=" * 60)
    
    download_timeout = 300  # 5分钟超时
    
    while True:
        # 获取待下载的队列项目
        queue_items = db.get_queue_items(job_id, status="pending", limit=args.workers * 2)
        
        if not queue_items:
            # 检查是否还有失败的可重试项目
            retry_failed_items()
            queue_items = db.get_queue_items(job_id, status="pending", limit=args.workers * 2)
            
            if not queue_items:
                # 没有更多项目了，检查是否完成
                stats = db.get_queue_stats(job_id)
                if stats["pending"] == 0 and stats["downloading"] == 0:
                    logger.info("所有线程下载完成！")
                    break
                else:
                    # 等待一段时间再检查
                    time.sleep(5)
                    continue
        
        # 并发下载
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(download_one_thread, item) for item in queue_items]
            
            for future in as_completed(futures, timeout=download_timeout):
                try:
                    future.result(timeout=30)  # 单个线程30秒超时
                except Exception as e:
                    logger.error("下载工作进程异常: %s", e)
    
    # 统计和完成
    stats = db.get_queue_stats(job_id)
    logger.info("=" * 60)
    logger.info("采集完成！新增 %d 封邮件入库", total_new[0])
    logger.info("队列统计: 总计 %d, 完成 %d, 失败 %d", 
                stats["total"], stats["completed"], stats["failed"])
    logger.info("知识库统计: %s", db.stats())
    logger.info("=" * 60)
    db.update_job(job_id, status="done")


def run_download_workers_stream(db: KnowledgeDB, job_id: int, args, topic_name: str, producer_done):
    """流式消费者：生产未结束时持续拉取队列，支持中断续传。"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    json_dir = EMAILS_DIR / f"kb_{topic_name}"
    json_dir.mkdir(parents=True, exist_ok=True)
    db_lock = threading.Lock()

    def download_one_thread(queue_item: Dict):
        queue_id = queue_item["id"]
        db.update_queue_status(queue_id, "downloading")
        root_mid = queue_item["root_message_id"]
        subject = queue_item.get("subject", "")
        source_url = queue_item.get("source_url", "")

        try:
            fetcher = LoreThreadFetcher(timeout=30, max_retries=2)
            thread_emails = fetcher.fetch_by_url(source_url.strip()) if source_url else []
            if not thread_emails:
                thread_emails = [{
                    "message_id": root_mid,
                    "subject": subject,
                    "source_url": source_url,
                    "relevance_score": queue_item.get("relevance_score", 0),
                    "relevance_reason": queue_item.get("relevance_reason", ""),
                }]

            for em in thread_emails:
                em["thread_id"] = root_mid
                em["relevance_score"] = queue_item.get("relevance_score", 0)
                em["relevance_reason"] = queue_item.get("relevance_reason", "")

            # 构建线程树（多封邮件时）
            thread_objs = []
            if len(thread_emails) > 1:
                try:
                    thread_objs = build_threads(thread_emails)
                except Exception:
                    pass

            with db_lock:
                db.insert_emails_bulk(thread_emails)

                thread_created = False
                for t in thread_objs:
                    td = t.to_dict()
                    # 始终以 root_mid 作为 thread id，保持与 email.thread_id 一致
                    db.upsert_thread({
                        "id": root_mid,
                        "root_message_id": root_mid,
                        "subject": td["root"]["subject"],
                        "start_date": td["date_range"][0],
                        "end_date": td["date_range"][1],
                        "email_count": 1 + len(td["replies"]),
                        "participant_count": len(td["participants"]),
                    })
                    thread_created = True
                    break  # 只取第一个线程树（root_mid 对应的）

                # 即使只有1封邮件或 build_threads 失败，也要创建 thread 记录
                if not thread_created:
                    first_date = thread_emails[0].get("date", "")
                    db.upsert_thread({
                        "id": root_mid,
                        "root_message_id": root_mid,
                        "subject": subject or thread_emails[0].get("subject", ""),
                        "start_date": first_date,
                        "end_date": first_date,
                        "email_count": len(thread_emails),
                        "participant_count": 1,
                    })

            for em in thread_emails:
                mid_safe = re.sub(r'[<>@/\\\\]', '_', em.get("message_id", "unknown"))[:80]
                json_path = json_dir / f"{mid_safe}.json"
                if not json_path.exists():
                    try:
                        json_path.write_text(json.dumps(em, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception:
                        pass

            db.update_queue_status(queue_id, "completed")
        except Exception:
            retry_count = queue_item.get("retry_count", 0) + 1
            db.update_queue_status(queue_id, "failed", retry_count)

    while True:
        pending = db.get_queue_items(job_id, status="pending", limit=max(2, args.workers * 2))
        if not pending:
            failed = db.get_failed_queue_items(job_id, max_retries=3)
            for item in failed:
                db.update_queue_status(item["id"], "pending")
            pending =db.get_queue_items(job_id, status="pending", limit=max(2, args.workers * 2))

        if not pending:
            stats = db.get_queue_stats(job_id)
            if producer_done.is_set() and stats["pending"] == 0 and stats["downloading"] == 0:
                break
            time.sleep(2)
            continue

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            futures = [pool.submit(download_one_thread, item) for item in pending]
            for fut in as_completed(futures):
                try:
                    fut.result(timeout=300)
                except Exception:
                    pass


def run_collect_v2(args):
    """流式生产者-消费者：搜索过滤入队与下载入库并行。"""
    import threading

    db = KnowledgeDB()
    if args.topic_config:
        cfg = TopicConfig(args.topic_config)
        keywords, filter_keywords = cfg.search_keywords, cfg.filter_keywords
        blacklist_re = cfg.compile_blacklist()
        require_subsystem = cfg.filter_require_subsystem
        require_topic = cfg.filter_require_topic
        ai_prompt, topic_desc, topic_name = cfg.ai_filter_prompt, cfg.description, cfg.name
    else:
        keywords = [kw.strip() for kw in args.keywords.split(",") if kw.strip()]
        filter_keywords, blacklist_re = keywords, None
        require_subsystem, require_topic = [], []
        ai_prompt, topic_desc = "", ", ".join(keywords)
        topic_name = args.keywords.replace(",", "_")[:30]

    if not keywords:
        logger.error("必须提供 --topic-config 或 --keywords")
        return

    existing_job = None
    if args.resume or args.continuous:
        existing_job = db.conn.execute(
            "SELECT * FROM collect_jobs WHERE keywords = ? AND status != 'done' ORDER BY id DESC LIMIT 1",
            (",".join(keywords),),
        ).fetchone()
    job_id = existing_job["id"] if existing_job else db.create_job(
        keywords=",".join(keywords), date_from=args.date_from, date_to=args.date_to, list_name=args.list
    )

    progress = {}
    row = db.conn.execute("SELECT progress FROM collect_jobs WHERE id = ?", (job_id,)).fetchone()
    if row and row[0]:
        try:
            progress = json.loads(row[0])
        except Exception:
            progress = {}

    producer_done = threading.Event()
    consumer = threading.Thread(
        target=run_download_workers_stream,
        args=(db, job_id, args, topic_name, producer_done),
        daemon=True,
    )
    consumer.start()

    api_caller = None
    if not args.no_ai:
        from email_translator.translator import APITranslator
        api_caller = APITranslator(api_key=args.api_key, provider=args.api_provider, model=args.model or "", timeout=30)

    seen_mids = set()
    found_count = 0
    relevant_count = 0
    enqueue_count = 0
    budget = args.max_threads if args.max_threads > 0 else 0
    lock = threading.Lock()

    def process_batch(batch):
        nonlocal relevant_count, enqueue_count
        if args.ai_only:
            pre_filtered = batch
        else:
            pre_filtered = [em for em in batch if rule_filter(
                em, filter_keywords, blacklist_re,
                require_subsystem=require_subsystem,
                require_topic=require_topic)]

        if args.no_ai:
            relevant = pre_filtered
            for em in relevant:
                em["relevance_score"] = em.get("relevance_score", 0.5) or 0.5
                em["relevance_reason"] = em.get("relevance_reason", "rule_match_only")
        else:
            relevant = ai_filter_batch(pre_filtered, api_caller, workers=max(1, min(args.workers, 4)), prompt_template=ai_prompt, topic_desc=topic_desc)

        roots = {}
        for em in relevant:
            mid = em.get("message_id", "")
            refs = em.get("references", [])
            root_mid = refs[0] if refs else mid
            if root_mid and root_mid not in roots:
                roots[root_mid] = em

        with lock:
            relevant_count += len(relevant)
            for _, root_email in roots.items():
                if budget and enqueue_count >= budget:
                    break
                if db.add_to_queue(job_id, root_email, priority=root_email.get("relevance_score", 0)):
                    enqueue_count += 1

    def producer_one_keyword(kw):
        nonlocal found_count
        cursor = progress.get(kw, {}).get("last_date") or args.date_from
        client = LoreClient(timeout=30, delay=1.5)
        batch = client.search_emails(topic=kw, list_name=args.list, max_emails=args.max_emails, date_from=cursor, date_to=args.date_to, timeout=600)
        dedup = []
        with lock:
            for em in batch:
                mid = em.get("message_id", "")
                if mid and mid not in seen_mids:
                    seen_mids.add(mid)
                    dedup.append(em)
            found_count += len(dedup)
        process_batch(dedup)

        if batch:
            latest = max(em.get("date", "") for em in batch)
            progress[kw] = {"last_date": latest, "updated_at": time.time()}
            global_last = max(v.get("last_date", "") for v in progress.values() if isinstance(v, dict))
            db.update_job(job_id, progress=json.dumps(progress, ensure_ascii=False), last_search_time=global_last, total_found=found_count, total_relevant=relevant_count)

    with ThreadPoolExecutor(max_workers=min(len(keywords), max(1, args.workers))) as pool:
        futs = [pool.submit(producer_one_keyword, kw) for kw in keywords]
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:
                logger.error("关键词生产失败: %s", e)

    producer_done.set()
    consumer.join()

    stats = db.get_queue_stats(job_id)
    status = "done" if (stats["pending"] == 0 and stats["downloading"] == 0) else "running"
    db.update_job(job_id, status=status, total_found=found_count, total_relevant=relevant_count, progress=json.dumps(progress, ensure_ascii=False))
    logger.info("采集完成: job=%d status=%s found=%d relevant=%d queue=%s", job_id, status, found_count, relevant_count, stats)


def main():
    parser = argparse.ArgumentParser(
        description="批量采集内核邮件入知识库 (支持话题配置驱动 / 纯AI精筛两种模式)"
    )

    # 模式选择
    parser.add_argument("--topic-config", default="",
                        help="话题配置文件路径 (方案B, 如 topics/sched_latency.json)")
    parser.add_argument("--keywords", default="",
                        help="搜索关键词，逗号分隔 (方案C, 或方案B的覆盖)")
    parser.add_argument("--ai-only", action="store_true",
                        help="纯AI精筛模式(方案C): 跳过规则预筛和黑名单，全部交AI判断")

    # 搜索范围
    parser.add_argument("--date-from", required=True, help="开始日期 YYYY-MM-DD")
    parser.add_argument("--date-to", required=True, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--list", default="all",
                        help="邮件列表 (默认 all)")
    parser.add_argument("--max-emails", type=int, default=2000,
                        help="每个关键词最大搜索数 (默认 2000)")
    parser.add_argument("--max-threads", type=int, default=0,
                        help="最大下载线程数 (0=不限制, 如 100)")

    # AI 参数
    parser.add_argument("--api-key", default="", help="API 密钥")
    parser.add_argument("--api-provider", default="aliyun",
                        help="API 服务商 (默认 aliyun)")
    parser.add_argument("--model", default="", help="模型名 (留空用默认)")
    parser.add_argument("--workers", type=int, default=4,
                        help="并发数 (默认 4)")
    parser.add_argument("--no-ai", action="store_true",
                        help="跳过 AI 精筛，只用规则预筛")

    parser.add_argument("--proxy", default="", help="代理地址")
    
    # 采集控制
    parser.add_argument("--resume", action="store_true",
                        help="断点续传模式：继续未完成的采集任务")
    parser.add_argument("--continuous", action="store_true",
                        help="持续运行模式：24小时不间断采集")

    args = parser.parse_args()

    # 校验参数
    if not args.topic_config and not args.keywords:
        logger.error("必须提供 --topic-config 或 --keywords")
        sys.exit(1)

    # 从 config.json 补充 API 配置
    if not args.no_ai and not args.api_key:
        config_path = Path(__file__).parent / "config.json"
        if config_path.exists():
            cfg = json.loads(config_path.read_text())
            args.api_key = cfg.get("api_key", "")
            args.api_provider = cfg.get("api_provider", args.api_provider)
            args.model = args.model or cfg.get("model", "")

        if not args.api_key:
            logger.error("AI 精筛需要 --api-key 或 config.json。使用 --no-ai 可跳过。")
            sys.exit(1)

    if args.continuous:
        # 持续运行模式
        logger.info("启动24小时持续采集模式...")
        cycle_count = 0
        while True:
            cycle_count += 1
            logger.info("=" * 60)
            logger.info(f"开始第 {cycle_count} 轮采集...")
            logger.info("=" * 60)
            
            try:
                run_collect_v2(args)
            except Exception as e:
                logger.error(f"第 {cycle_count} 轮采集异常: {e}")
            
            # 检查是否达到日期范围末尾
            db = KnowledgeDB()
            job = db.conn.execute(
                "SELECT * FROM collect_jobs ORDER BY id DESC LIMIT 1"
            ).fetchone()
            if job and job["last_search_time"] and job["last_search_time"] >= args.date_to:
                logger.info("已到达日期范围末尾，停止持续采集")
                break
            
            # 等待一段时间后继续下一轮
            wait_time = 3600  # 1小时
            logger.info(f"等待 {wait_time} 秒后进行下一轮采集...")
            time.sleep(wait_time)
    else:
        # 单次运行模式
        run_collect_v2(args)


if __name__ == "__main__":
    main()