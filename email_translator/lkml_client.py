"""
LKML Client - 无需登录，直接爬取 lkml.org 公开归档
支持按关键词搜索、按日期范围过滤
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Dict, List, Optional
from urllib import request as urllib_request
from urllib.error import URLError
from urllib.parse import urlencode, quote

from .config import EMAILS_DIR

logger = logging.getLogger(__name__)

BASE_URL = "https://lkml.org"


# -----------------------------------------------------------------------
# HTML 解析器：解析单封邮件页面
# -----------------------------------------------------------------------

class _EmailPageParser(HTMLParser):
    """解析 lkml.org/lkml/YYYY/M/D/N 单封邮件页面"""

    def __init__(self):
        super().__init__()
        self.in_pre   = False
        self.pre_text = []
        self.meta: Dict[str, str] = {}
        self._current_meta_key = ""
        self._in_date_cell  = False
        self._in_subj_cell  = False
        self._in_from_cell  = False
        self._in_meta_value = False

    def handle_starttag(self, tag, attrs):
        attrs_d = dict(attrs)
        if tag == "pre":
            self.in_pre = True
        # lkml.org 在 <pre> 中使用 <br /> 表示换行
        if tag == "br" and self.in_pre:
            self.pre_text.append("\n")
        # 元信息在 <table> 的 Date / Subject / From 行
        # lkml.org 用 <b> 标签标注字段名
        if tag == "b":
            self._current_meta_key = ""

    def handle_endtag(self, tag):
        if tag == "pre":
            self.in_pre = False

    def handle_data(self, data):
        if self.in_pre:
            self.pre_text.append(data)
        # 抓 Date / Subject / From 行的简单文本
        text = data.strip()
        if text in ("Date", "Subject", "From"):
            self._current_meta_key = text
        elif self._current_meta_key and text:
            self.meta[self._current_meta_key] = text
            self._current_meta_key = ""

    @property
    def body(self) -> str:
        return "".join(self.pre_text).strip()


# -----------------------------------------------------------------------
# HTML 解析器：解析每日列表页
# -----------------------------------------------------------------------

class _DayListParser(HTMLParser):
    """解析 lkml.org/lkml/YYYY/M/D 日期列表页，提取邮件链接和主题"""

    def __init__(self):
        super().__init__()
        self._in_link = False
        self._current_href = ""
        self.entries: List[Dict[str, str]] = []  # [{url, subject}]

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            attrs_d = dict(attrs)
            href = attrs_d.get("href", "")
            # 邮件链接形如 /lkml/YYYY/M/D/N
            if re.match(r"^/lkml/\d{4}/\d+/\d+/\d+$", href):
                self._in_link = True
                self._current_href = href

    def handle_endtag(self, tag):
        if tag == "a":
            self._in_link = False

    def handle_data(self, data):
        if self._in_link and self._current_href:
            subject = data.strip()
            if subject:
                self.entries.append({
                    "url":     BASE_URL + self._current_href,
                    "subject": subject,
                })
            self._current_href = ""


# -----------------------------------------------------------------------
# 主客户端
# -----------------------------------------------------------------------

class LKMLClient:
    """
    无需账号，直接从 lkml.org 公开归档搜索并下载邮件。

    用法:
        client = LKMLClient()
        emails = client.search_emails("memory management", days=7, max_emails=20)
    """

    def __init__(self, timeout: int = 15, delay: float = 0.5):
        """
        Args:
            timeout: HTTP 请求超时（秒）
            delay:   每次请求之间的间隔（秒），避免对服务器施压
        """
        self.timeout = timeout
        self.delay   = delay

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def search_emails(self, topic: str, days: int = 30,
                      max_emails: int = 20,
                      date_from: Optional[str] = None,
                      date_to: Optional[str] = None,
                      author: Optional[str] = None) -> List[Dict]:
        """
        按关键词搜索 lkml.org 归档邮件（不需要登录）。

        Args:
            topic:      关键词（大小写不敏感，在主题中匹配）
            days:       搜索最近 N 天（当 date_from/date_to 均未指定时生效）
            max_emails: 最多返回邮件数
            date_from:  起始日期，格式 YYYY-MM-DD（含）
            date_to:    截止日期，格式 YYYY-MM-DD（含，默认今天）
            author:     作者过滤，大小写不敏感，匹配发件人姓名或邮箱地址的子串；
                        为 None 或空字符串时不过滤

        Returns:
            邮件字典列表，字段与原 EmailClient 保持兼容
        """
        # 解析日期范围
        if date_from or date_to:
            dt_to   = (datetime.strptime(date_to,   "%Y-%m-%d")
                       if date_to   else datetime.now())
            dt_from = (datetime.strptime(date_from, "%Y-%m-%d")
                       if date_from else dt_to - timedelta(days=days))
        else:
            dt_to   = datetime.now()
            dt_from = dt_to - timedelta(days=days)

        # 保证 from <= to
        if dt_from > dt_to:
            dt_from, dt_to = dt_to, dt_from

        total_days = (dt_to - dt_from).days + 1
        author_kw  = author.lower().strip() if author else ""
        logger.info(
            f"搜索 lkml.org: topic={topic!r}, "
            f"{dt_from.strftime('%Y-%m-%d')} ~ {dt_to.strftime('%Y-%m-%d')}, "
            f"author={author!r}, max={max_emails}"
        )
        # 支持多关键词组合搜索：
        # - "A AND B" → 主题必须同时包含 A 和 B
        # - "A OR B"  → 主题包含 A 或 B 即可
        # - 默认（无 AND/OR）→ 作为单个关键词匹配
        keywords = self._parse_keywords(topic)

        # 生成要爬取的日期列表（从新到旧）
        dates = [
            dt_to - timedelta(days=i)
            for i in range(total_days)
        ]

        # 第一步：按日期+主题关键词收集候选链接
        # 作者信息只在详情页，所以候选池要足够大
        pool_limit = max_emails * (10 if author_kw else 3)
        candidates: List[Dict[str, str]] = []

        for dt in dates:
            if len(candidates) >= pool_limit:
                break
            day_entries = self._fetch_day_list(dt)
            for entry in day_entries:
                if self._match_keywords(entry["subject"], keywords):
                    candidates.append(entry)
            time.sleep(self.delay)

        logger.info(f"主题匹配 {len(candidates)} 封候选邮件，开始下载详情...")

        # 第二步：下载详情，按作者过滤，直到凑满 max_emails
        results = []
        for entry in candidates:
            if len(results) >= max_emails:
                break
            detail = self._fetch_email(entry["url"])
            if not detail:
                time.sleep(self.delay)
                continue
            # 作者过滤（在 sender 字段中做子串匹配）
            if author_kw and author_kw not in detail.get("sender", "").lower():
                time.sleep(self.delay)
                continue
            results.append(detail)
            time.sleep(self.delay)

        logger.info(f"成功下载 {len(results)} 封邮件")
        return results

    def save_emails(self, emails: List[Dict], topic: str) -> List[str]:
        """保存邮件到本地 JSON（与原 EmailClient 接口兼容）"""
        safe_topic = topic.replace(" ", "_").replace("/", "_")[:50]
        dest_dir = EMAILS_DIR / safe_topic
        dest_dir.mkdir(parents=True, exist_ok=True)

        saved = []
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for i, em in enumerate(emails, start=1):
            path = dest_dir / f"email_{i:03d}_{ts}.json"
            try:
                path.write_text(
                    json.dumps(em, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                saved.append(str(path))
            except Exception as e:
                logger.error(f"保存失败: {e}")

        logger.info(f"共保存 {len(saved)} 封邮件至 {dest_dir}")
        return saved

    # ------------------------------------------------------------------
    # 内部：关键词解析与匹配
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_keywords(topic: str) -> Dict:
        """
        解析关键词表达式，支持 AND / OR 组合。
        返回 {"mode": "and"|"or"|"plain", "terms": [...]}
        """
        topic_stripped = topic.strip()
        # 检查是否包含 AND / OR 运算符（大小写不敏感）
        if re.search(r'\bAND\b', topic_stripped):
            terms = [t.strip().lower() for t in re.split(r'\bAND\b', topic_stripped, flags=re.IGNORECASE) if t.strip()]
            return {"mode": "and", "terms": terms}
        elif re.search(r'\bOR\b', topic_stripped):
            terms = [t.strip().lower() for t in re.split(r'\bOR\b', topic_stripped, flags=re.IGNORECASE) if t.strip()]
            return {"mode": "or", "terms": terms}
        else:
            return {"mode": "plain", "terms": [topic_stripped.lower()]}

    @staticmethod
    def _match_keywords(text: str, keywords: Dict) -> bool:
        """根据解析后的关键词匹配文本"""
        text_lower = text.lower()
        mode = keywords["mode"]
        terms = keywords["terms"]
        if mode == "and":
            return all(t in text_lower for t in terms)
        elif mode == "or":
            return any(t in text_lower for t in terms)
        else:
            return terms[0] in text_lower if terms else False

    # ------------------------------------------------------------------
    # 内部：按日期爬取列表
    # ------------------------------------------------------------------

    def _fetch_day_list(self, dt: datetime) -> List[Dict[str, str]]:
        """获取某一天的邮件列表 -> [{url, subject}]"""
        url = f"{BASE_URL}/lkml/{dt.year}/{dt.month}/{dt.day}"
        html = self._get(url)
        if not html:
            return []
        parser = _DayListParser()
        try:
            parser.feed(html)
        except Exception as e:
            logger.debug(f"解析日期列表失败 {url}: {e}")
        return parser.entries

    # ------------------------------------------------------------------
    # 内部：爬取单封邮件
    # ------------------------------------------------------------------

    def _fetch_email(self, url: str) -> Optional[Dict]:
        """爬取单封邮件页面，返回与 EmailClient 兼容的字典"""
        html = self._get(url)
        if not html:
            return None

        parser = _EmailPageParser()
        try:
            parser.feed(html)
        except Exception as e:
            logger.debug(f"解析邮件页面失败 {url}: {e}")
            return None

        meta = parser.meta
        body = parser.body

        # 从 URL 提取发送日期（/lkml/YYYY/M/D/N）
        m = re.search(r"/lkml/(\d{4})/(\d+)/(\d+)/\d+", url)
        date_str = ""
        if m:
            try:
                date_str = datetime(int(m.group(1)), int(m.group(2)),
                                    int(m.group(3))).strftime("%a, %d %b %Y")
            except ValueError:
                pass

        # 为 lkml.org 邮件生成 Message-ID，因为原始网站不提供
        # 使用 URL 哈希作为唯一标识符
        import hashlib
        message_id = f"<lkml-{hashlib.md5(url.encode()).hexdigest()}@lkml.org>"
        
        return {
            "subject":     meta.get("Subject", ""),
            "from":        meta.get("From", ""),
            "to":          "linux-kernel@vger.kernel.org",
            "date":        meta.get("Date", date_str),
            "body":        body,
            "attachments": [],
            "message_id":  message_id,
            "in_reply_to": "",
            "references":  [],
            "source_url":  url,
        }

    # ------------------------------------------------------------------
    # 内部：HTTP GET
    # ------------------------------------------------------------------

    def _get(self, url: str) -> Optional[str]:
        """发送 GET 请求，返回 HTML 字符串；失败返回 None"""
        req = urllib_request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (compatible; email-translator/1.0; "
                    "+https://github.com/example/email-translator)"
                ),
                "Accept": "text/html,application/xhtml+xml",
            },
        )
        try:
            with urllib_request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                # lkml.org 页面是 UTF-8
                return raw.decode("utf-8", errors="ignore")
        except URLError as e:
            logger.warning(f"请求失败 {url}: {e}")
            return None
        except Exception as e:
            logger.warning(f"未知错误 {url}: {e}")
            return None