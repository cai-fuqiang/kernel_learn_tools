"""
Lore Thread Fetcher - 从 lore.kernel.org 抓取完整邮件线程

给定一封邮件的 Lore URL 或 Message-ID，下载整个线程的 mbox，
解析成结构化邮件列表（含所有回复和 review）。

支持多种下载策略（按优先级）：
  1. mbox.gz 端点 (t.mbox.gz) — 完整线程
  2. 单封邮件 raw 端点 (/raw) — 降级到单封
  3. 从已保存的本地邮件 JSON 加载

Anubis PoW 绕过：
  Lore 部署了 Anubis 机器人防护，需要解 SHA256 PoW challenge。
  本模块自动提取 challenge 并用 Python 求解，获取 cookie 后
  再下载 mbox。若 challenge 为 null（直接拒绝），降级到其他策略。
"""
import email
import email.header
import email.policy
import gzip
import hashlib
import json
import logging
import os
import re
import time
from email.utils import parsedate_to_datetime
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

# lore.kernel.org 端点模板
_LORE_REDIRECT_URL = "https://lore.kernel.org/r/{msgid}"
_LORE_MBOX_URL     = "https://lore.kernel.org/{list}/{msgid}/t.mbox.gz"
_LORE_RAW_URL      = "https://lore.kernel.org/{list}/{msgid}/raw"

# Anubis challenge 端点
_ANUBIS_PASS_URL = "/.within.website/x/cmd/anubis/api/pass-challenge"

# 多个 User-Agent 轮换
# 注意: b4 UA 会被 Anubis 直接 403，必须把浏览器 UA 放在前面
_USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "b4/0.14.2",
]


class AnubisPoWSolver:
    """
    Anubis PoW 求解器

    算法: SHA256(data + nonce)，要求 hash 的前 N 个字节为 0
    其中 N = difficulty / 2，若 difficulty 为奇数，还需高 4 位为 0
    """

    @staticmethod
    def solve(data: str, difficulty: int) -> Tuple[str, int]:
        """
        求解 PoW challenge

        Args:
            data:       challenge 的 randomData 字符串
            difficulty: 难度（需要多少个 hex 0）

        Returns:
            (hex_hash, nonce) 满足条件的 hash 和 nonce
        """
        required_zero_bytes = difficulty // 2
        is_odd = (difficulty % 2) != 0

        nonce = 0
        while True:
            candidate = f"{data}{nonce}"
            hash_bytes = hashlib.sha256(candidate.encode()).digest()

            # 检查前 N 个字节是否为 0
            valid = all(hash_bytes[i] == 0 for i in range(required_zero_bytes))

            # 如果 difficulty 为奇数，额外检查高 4 位
            if valid and is_odd:
                valid = (hash_bytes[required_zero_bytes] >> 4) == 0

            if valid:
                hex_hash = hash_bytes.hex()
                logger.debug("PoW solved: nonce=%d hash=%s...", nonce, hex_hash[:16])
                return hex_hash, nonce

            nonce += 1
            if nonce % 50000 == 0:
                logger.debug("PoW solving... nonce=%d", nonce)


class LoreThreadFetcher:
    """从 lore.kernel.org 抓取完整邮件线程（含 Anubis PoW 绕过）"""

    def __init__(self, timeout: int = 30, delay: float = 0.5, max_retries: int = 2):
        self.timeout = timeout
        self.delay   = delay
        self.max_retries = max_retries
        self._session: Optional[requests.Session] = None
        self._ua_index = 0

    def _get_session(self, rotate_ua: bool = False) -> requests.Session:
        """获取或创建 requests session（含 cookie jar）"""
        if self._session is None:
            self._session = requests.Session()
        if rotate_ua:
            self._ua_index = (self._ua_index + 1) % len(_USER_AGENTS)
        self._session.headers.update({
            "User-Agent": _USER_AGENTS[self._ua_index],
        })
        return self._session

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def fetch_by_url(self, lore_url: str) -> List[Dict]:
        """
        给定任意 lore URL，返回完整线程的邮件列表。

        下载策略（按优先级自动降级）：
          1. 尝试 mbox.gz 完整线程下载
          2. 如果被 Anubis 拦截且无法求解 → 尝试 raw 单封邮件
          3. 如果网络完全不通 → 返回空列表

        Args:
            lore_url: 如 https://lore.kernel.org/r/20230531124604.274010996@infradead.org

        Returns:
            邮件字典列表，字段与 LKMLClient 兼容
        """
        msgid, list_name = self._resolve_url(lore_url)
        if not msgid:
            logger.warning("无法从 URL 提取 msgid: %s", lore_url)
            return []

        logger.info("Lore 线程: list=%s  msgid=%s", list_name or "?", msgid)
        return self.fetch_thread(msgid, list_name)

    def fetch_thread(self, msgid: str, list_name: str = "") -> List[Dict]:
        """
        给定 msgid，下载完整线程 mbox 并解析。

        Args:
            msgid:     邮件 Message-ID（不含尖括号）
            list_name: lore 列表名（如 linux-kernel），留空则自动推断
        """
        # 优先用 /r/ 重定向拿到真实列表名
        if not list_name:
            list_name = self._resolve_list(msgid)

        effective_list = list_name or "all"

        # ── 策略 1: 完整 mbox 线程下载 ──
        mbox_url = _LORE_MBOX_URL.format(list=effective_list, msgid=msgid)
        logger.info("策略1: 下载完整线程 mbox: %s", mbox_url)
        mbox_data = self._fetch_mbox_with_retries(mbox_url)

        if mbox_data:
            emails = self._parse_mbox(mbox_data)
            if emails:
                logger.info("mbox 解析完成：%d 封邮件", len(emails))
                return emails
            else:
                logger.warning("mbox 数据解析为空（可能是 HTML/错误页面）")

        # ── 策略 2: 单封邮件 raw 下载 ──
        raw_url = _LORE_RAW_URL.format(list=effective_list, msgid=msgid)
        logger.info("策略2: 降级到 raw 单封邮件: %s", raw_url)
        raw_data = self._fetch_raw_email(raw_url)
        if raw_data:
            emails = self._parse_single_raw(raw_data)
            if emails:
                logger.info("raw 解析完成：%d 封邮件", len(emails))
                return emails

        logger.error("所有策略均失败，无法获取 msgid=%s 的邮件", msgid)
        logger.info("提示: lore.kernel.org 部署了 Anubis 防护，"
                     "某些网络环境下可能无法直接访问")
        logger.info("建议: 1) 检查网络连通性  "
                     "2) 在浏览器中手动访问 %s 获取邮件", mbox_url)
        return []

    # ------------------------------------------------------------------
    # 内部：mbox 下载（含 Anubis PoW + 重试）
    # ------------------------------------------------------------------

    def _fetch_mbox_with_retries(self, url: str) -> Optional[bytes]:
        """带重试和 UA 轮换的 mbox 下载"""
        for attempt in range(self.max_retries + 1):
            if attempt > 0:
                logger.info("重试 %d/%d (切换 User-Agent)...",
                            attempt, self.max_retries)
                time.sleep(self.delay * attempt)

            data = self._fetch_mbox_with_anubis(url, rotate_ua=(attempt > 0))
            if data:
                return data

        return None

    def _fetch_mbox_with_anubis(self, url: str,
                                 rotate_ua: bool = False) -> Optional[bytes]:
        """
        下载 mbox，自动处理 Anubis PoW challenge。

        流程：
        1. 先尝试 POST（b4 风格）
        2. 如果失败，尝试 GET
        3. 如果返回 gzip 数据 → 直接返回
        4. 如果返回 Anubis 页面 → 提取 challenge → 求解 PoW → 获取 cookie
        5. 如果是 403 / Anubis reject（null challenge）→ 返回 None
        """
        session = self._get_session(rotate_ua=rotate_ua)

        # ── POST 请求（b4 的方式）──
        try:
            resp = session.post(url, data="", timeout=self.timeout)
            result = self._check_response(resp, "POST")
            if result is not None:
                return result
        except requests.exceptions.Timeout:
            logger.warning("POST 超时: %s", url)
        except Exception as e:
            logger.debug("POST 失败: %s", e)

        # ── GET 请求 ──
        try:
            resp = session.get(url, timeout=self.timeout)
        except requests.exceptions.Timeout:
            logger.warning("GET 超时: %s", url)
            return None
        except Exception as e:
            logger.error("GET 请求失败: %s", e)
            return None

        result = self._check_response(resp, "GET")
        if result is not None:
            return result

        # ── 检查是否是 Anubis 挑战页面 ──
        if resp.status_code == 200 and b"anubis_challenge" in resp.content:
            return self._handle_anubis(resp, url, session)

        # ── 503 服务过载 → 指数退避重试 ──
        if resp.status_code == 503:
            backoff = self.delay * (attempt + 1) * 2
            logger.warning("503 Service Unavailable，等待 %.1fs 后重试...", backoff)
            time.sleep(backoff)
            return None  # 让外层重试循环处理

        # ── 403 或其他错误 ──
        if resp.status_code == 403:
            logger.info("403 Forbidden，尝试访问首页触发 Anubis 挑战...")
            # 主动访问首页触发 Anubis 获取 cookie
            try:
                parsed = urlparse(url)
                home_url = f"{parsed.scheme}://{parsed.netloc}/"
                home_resp = session.get(home_url, timeout=self.timeout)
                if home_resp.status_code == 200 and b"anubis_challenge" in home_resp.content:
                    result = self._handle_anubis(home_resp, url, session)
                    if result is not None:
                        return result
            except Exception as e:
                logger.debug("首页 Anubis 触发失败: %s", e)
            logger.warning("403 Forbidden — 无法通过 Anubis 获取 cookie")
        else:
            logger.warning("未知响应: status=%d content_type=%s",
                           resp.status_code,
                           resp.headers.get("content-type"))
        return None

    def _check_response(self, resp, method: str) -> Optional[bytes]:
        """检查响应是否为有效的 gzip mbox 数据"""
        if resp.status_code == 200 and resp.content[:2] == b'\x1f\x8b':
            logger.info("%s 直接获取 mbox 成功", method)
            try:
                return gzip.decompress(resp.content)
            except Exception as e:
                logger.error("gzip 解压失败: %s", e)
        return None

    def _handle_anubis(self, resp, url: str,
                       session: requests.Session) -> Optional[bytes]:
        """处理 Anubis 挑战页面"""
        logger.info("检测到 Anubis PoW 挑战，开始求解...")

        # 提取 challenge JSON
        challenge_data = self._extract_anubis_challenge(resp.text)
        if challenge_data is None:
            logger.warning("Anubis challenge 为 null — 直接拒绝，无法求解")
            return None

        challenge = challenge_data.get("challenge", {})
        rules = challenge_data.get("rules", {})

        challenge_id = challenge.get("id", "")
        random_data  = challenge.get("randomData", "")
        difficulty   = rules.get("difficulty", 5)

        if not challenge_id or not random_data:
            logger.error("challenge 数据不完整: %s", challenge_data)
            return None

        logger.info("Anubis challenge: id=%s difficulty=%d",
                     challenge_id, difficulty)

        # 求解 PoW
        t0 = time.time()
        hex_hash, nonce = AnubisPoWSolver.solve(random_data, difficulty)
        elapsed = int((time.time() - t0) * 1000)
        logger.info("PoW 求解完成: nonce=%d elapsed=%dms", nonce, elapsed)

        # 提取 base_prefix
        base_prefix = self._extract_json_var(resp.text, "anubis_base_prefix") or ""

        # 构造 pass-challenge URL
        parsed = urlparse(resp.url or url)
        base_url = f"{parsed.scheme}://{parsed.netloc}"
        pass_url = base_url + base_prefix + _ANUBIS_PASS_URL

        params = {
            "id": challenge_id,
            "response": hex_hash,
            "nonce": str(nonce),
            "redir": url,
            "elapsedTime": str(elapsed),
        }

        logger.info("提交 PoW 结果到: %s", pass_url)
        try:
            session.get(pass_url, params=params, timeout=self.timeout,
                        allow_redirects=True)
        except Exception as e:
            logger.error("提交 PoW 失败: %s", e)
            return None

        # 用获得的 cookie 重新请求 mbox
        time.sleep(0.5)
        for method in ("POST", "GET"):
            try:
                if method == "POST":
                    resp2 = session.post(url, data="", timeout=self.timeout)
                else:
                    resp2 = session.get(url, timeout=self.timeout)
                result = self._check_response(resp2, f"post-PoW {method}")
                if result is not None:
                    return result
            except Exception as e:
                logger.debug("post-PoW %s 失败: %s", method, e)

        logger.error("PoW 通过后仍无法获取 mbox")
        return None

    # ------------------------------------------------------------------
    # 内部：raw 单封邮件下载
    # ------------------------------------------------------------------

    def _fetch_raw_email(self, url: str) -> Optional[bytes]:
        """下载单封邮件的 raw 原始格式"""
        session = self._get_session()
        try:
            resp = session.get(url, timeout=self.timeout)
            if resp.status_code == 200:
                content_type = resp.headers.get("content-type", "")
                # raw 端点返回 text/plain
                if "text/plain" in content_type or resp.content[:5] != b'<html':
                    # 检查不是 HTML 错误页面
                    if not self._is_html_error(resp.content):
                        return resp.content
            logger.debug("raw 下载失败: status=%d", resp.status_code)
        except Exception as e:
            logger.debug("raw 下载异常: %s", e)
        return None

    @staticmethod
    def _is_html_error(content: bytes) -> bool:
        """检测内容是否是 HTML 错误页面（而非邮件原文）"""
        head = content[:500].lower()
        return (b'<!doctype html' in head or
                b'<html' in head or
                b'anubis' in head or
                b'403 forbidden' in head)

    # ------------------------------------------------------------------
    # 内部：Anubis challenge 提取
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_anubis_challenge(html: str) -> Optional[dict]:
        """从 Anubis 页面提取 challenge JSON，null 时返回 None"""
        m = re.search(
            r'<script[^>]+id=["\']anubis_challenge["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE
        )
        if not m:
            return None
        try:
            data = json.loads(m.group(1))
            return data  # 可能是 None（null）或 dict
        except json.JSONDecodeError:
            return None

    @staticmethod
    def _extract_json_var(html: str, var_id: str):
        """从 Anubis 页面提取指定 script 标签的 JSON 值"""
        m = re.search(
            rf'<script[^>]+id=["\']' + var_id + rf'["\'][^>]*>(.*?)</script>',
            html, re.DOTALL | re.IGNORECASE
        )
        if not m:
            return None
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            return None

    # ------------------------------------------------------------------
    # 内部：URL 解析
    # ------------------------------------------------------------------

    def _resolve_url(self, url: str):
        """从 Lore URL 中提取 msgid 和 list_name"""
        # 格式1: /r/<msgid>
        m = re.search(r'/r/([^/\s]+)', url)
        if m:
            return m.group(1), ""

        # 格式2: /<list>/<msgid>/
        m = re.search(r'lore\.kernel\.org/([^/]+)/([^/\s]+)', url)
        if m:
            list_name = m.group(1)
            msgid     = m.group(2).rstrip('/')
            return msgid, list_name

        return "", ""

    def _resolve_list(self, msgid: str) -> str:
        """通过 /r/ 重定向获取真实列表名"""
        url = _LORE_REDIRECT_URL.format(msgid=msgid)
        session = self._get_session()
        try:
            resp = session.head(url, allow_redirects=True, timeout=self.timeout)
            final_url = resp.url
            m = re.search(r'lore\.kernel\.org/([^/]+)/', final_url)
            if m:
                list_name = m.group(1)
                if list_name != "r":
                    return list_name
        except Exception as e:
            logger.debug("_resolve_list 失败: %s", e)
        return ""

    # ------------------------------------------------------------------
    # 内部：mbox 解析
    # ------------------------------------------------------------------

    def _parse_mbox(self, mbox_bytes: bytes) -> List[Dict]:
        """解析 mbox 格式，返回邮件字典列表"""
        # 先检查是否是 HTML 错误页面
        if self._is_html_error(mbox_bytes):
            logger.warning("mbox 内容实际是 HTML 错误页面，跳过解析")
            return []

        # 尝试多种编码避免乱码
        for enc in ("utf-8", "latin-1"):
            try:
                text = mbox_bytes.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = mbox_bytes.decode("utf-8", errors="replace")
        # mbox 每封邮件以 "From " 行开头
        raw_messages = re.split(
            r'\nFrom \S+ \S+ \S+ +\d+ \S+ \d+\n', "\n" + text
        )
        emails = []

        for raw in raw_messages:
            raw = raw.strip()
            if not raw:
                continue
            try:
                msg = email.message_from_string(
                    raw, policy=email.policy.compat32
                )
                parsed = self._extract_email(msg)
                if parsed.get("subject") or parsed.get("body"):
                    emails.append(parsed)
            except Exception as e:
                logger.debug("解析邮件失败: %s", e)

        # 按时间排序（统一转为 UTC timestamp 避免 naive/aware 混合比较）
        def sort_key(e):
            try:
                dt = parsedate_to_datetime(e.get("date", ""))
                return dt.timestamp()
            except Exception:
                return 0.0

        emails.sort(key=sort_key)
        return emails

    def _parse_single_raw(self, raw_bytes: bytes) -> List[Dict]:
        """解析单封 raw 邮件"""
        if self._is_html_error(raw_bytes):
            return []
        try:
            text = raw_bytes.decode("utf-8", errors="replace")
            msg = email.message_from_string(
                text, policy=email.policy.compat32
            )
            parsed = self._extract_email(msg)
            if parsed.get("subject") or parsed.get("body"):
                return [parsed]
        except Exception as e:
            logger.debug("解析 raw 邮件失败: %s", e)
        return []

    @staticmethod
    def _decode_payload(payload: bytes, charset: str | None) -> str:
        """尝试多种编码解码邮件 payload，避免 U+FFFD 乱码"""
        # 优先使用邮件头声明的 charset
        encodings = []
        if charset:
            encodings.append(charset)
        encodings.extend(["utf-8", "latin-1", "iso-8859-1", "windows-1252"])
        for enc in encodings:
            try:
                return payload.decode(enc)
            except (UnicodeDecodeError, LookupError):
                continue
        return payload.decode("utf-8", errors="replace")

    def _extract_email(self, msg) -> Dict:
        """从 email.Message 对象提取字段"""
        def decode_header(value: str) -> str:
            if not value:
                return ""
            parts = email.header.decode_header(value)
            decoded = []
            for part, charset in parts:
                if isinstance(part, bytes):
                    decoded.append(
                        part.decode(charset or "utf-8", errors="replace")
                    )
                else:
                    decoded.append(part)
            return " ".join(decoded).strip()

        subject    = decode_header(msg.get("Subject", ""))
        from_      = decode_header(msg.get("From", ""))
        date       = msg.get("Date", "")
        message_id = msg.get("Message-ID", "").strip().strip("<>")
        in_reply_to = msg.get("In-Reply-To", "").strip().strip("<>")
        references  = [r.strip().strip("<>")
                       for r in msg.get("References", "").split()
                       if r.strip()]

        # 提取正文
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        body = self._decode_payload(
                            payload, part.get_content_charset()
                        )
                        break
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                body = self._decode_payload(
                    payload, msg.get_content_charset()
                )

        return {
            "subject":     subject,
            "from":        from_,
            "date":        date,
            "body":        body.strip(),
            "message_id":  f"<{message_id}>" if message_id else "",
            "in_reply_to": f"<{in_reply_to}>" if in_reply_to else "",
            "references":  [f"<{r}>" for r in references],
            "attachments": [],
            "source_url":  "",
        }