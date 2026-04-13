"""Thread Builder - 按 Message-ID/In-Reply-To/References 构建邮件线程树"""
import logging
from collections import defaultdict
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, List

logger = logging.getLogger(__name__)


def _normalize_mid(mid: str) -> str:
    if not mid:
        return ""
    mid = str(mid).strip()
    if mid.startswith("<") and mid.endswith(">"):
        return mid
    if mid.startswith("<"):
        return f"{mid}>"
    if mid.endswith(">"):
        return f"<{mid}"
    return f"<{mid}>"


def _to_dt(date_str: str) -> datetime:
    """将日期字符串解析为 UTC 时区的 datetime（确保所有结果可比较）"""
    if not date_str:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = parsedate_to_datetime(date_str)
        # 统一转为 UTC
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            dt = datetime.fromisoformat(date_str)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except Exception:
            return datetime.min.replace(tzinfo=timezone.utc)


class Thread:
    """邮件线程：根邮件 + 回复列表"""

    def __init__(self, root: Dict):
        self.root = root
        self.replies: List[Dict] = []
        self.participants = set()
        self.date_range = (root.get("date", ""), root.get("date", ""))
        self._update_participants(root)

    def add_reply(self, reply: Dict):
        self.replies.append(reply)
        self._update_participants(reply)
        self._refresh_date_range(reply)

    def _refresh_date_range(self, email: Dict):
        cur_min, cur_max = self.date_range
        d = email.get("date", "")
        if not cur_min or _to_dt(d) < _to_dt(cur_min):
            cur_min = d
        if not cur_max or _to_dt(d) > _to_dt(cur_max):
            cur_max = d
        self.date_range = (cur_min, cur_max)

    def _update_participants(self, email: Dict):
        sender = email.get("from", "")
        if sender:
            self.participants.add(sender)

    def finalize(self):
        self.replies.sort(key=lambda e: _to_dt(e.get("date", "")))
        for r in self.replies:
            self._refresh_date_range(r)

    def to_dict(self):
        return {
            "root": self.root,
            "replies": self.replies,
            "participants": sorted(self.participants),
            "date_range": self.date_range,
        }


def build_threads(emails: List[Dict]) -> List[Thread]:
    """根据邮件头字段构建线程列表。"""
    normalized = []
    for e in emails:
        item = dict(e)
        item["message_id"] = _normalize_mid(item.get("message_id", ""))
        item["in_reply_to"] = _normalize_mid(item.get("in_reply_to", ""))
        refs = item.get("references", [])
        if isinstance(refs, str):
            refs = [r.strip() for r in refs.split() if r.strip()]
        item["references"] = [_normalize_mid(r) for r in refs if r]
        normalized.append(item)

    email_map = {e.get("message_id"): e for e in normalized if e.get("message_id")}
    replies_map: Dict[str, List[Dict]] = defaultdict(list)
    roots: List[Dict] = []

    all_refs = set()
    for e in normalized:
        all_refs.update(e.get("references", []))

    for e in normalized:
        mid = e.get("message_id")
        if not mid:
            logger.warning("邮件缺失 Message-ID，跳过: %s", e.get("subject", "无主题"))
            continue

        in_reply_to = e.get("in_reply_to", "")
        refs = e.get("references", [])
        parent_mid = in_reply_to or (refs[-1] if refs else "")

        is_root = (not in_reply_to) and (mid not in all_refs)
        if is_root:
            roots.append(e)
            continue

        if parent_mid and parent_mid in email_map:
            replies_map[parent_mid].append(e)
        else:
            roots.append(e)

    threads: List[Thread] = []
    visited = set()

    for root in roots:
        root_mid = root.get("message_id")
        if not root_mid or root_mid in visited:
            continue

        thread = Thread(root)
        visited.add(root_mid)
        _build_replies(thread, root_mid, replies_map, visited)
        thread.finalize()
        threads.append(thread)

    for e in normalized:
        mid = e.get("message_id")
        if mid and mid not in visited:
            thread = Thread(e)
            visited.add(mid)
            _build_replies(thread, mid, replies_map, visited)
            thread.finalize()
            threads.append(thread)

    threads.sort(key=lambda t: _to_dt(t.root.get("date", "")), reverse=True)
    logger.info("构建完成：%d 个线程，共 %d 封邮件", len(threads), len(emails))
    return threads


def _build_replies(thread: Thread, parent_mid: str, replies_map: Dict[str, List[Dict]], visited: set):
    for reply in sorted(replies_map.get(parent_mid, []), key=lambda e: _to_dt(e.get("date", ""))):
        reply_mid = reply.get("message_id")
        if reply_mid and reply_mid in visited:
            continue
        thread.add_reply(reply)
        if reply_mid:
            visited.add(reply_mid)
            _build_replies(thread, reply_mid, replies_map, visited)