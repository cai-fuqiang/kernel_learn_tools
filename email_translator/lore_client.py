"""Lore Client - lore.kernel.org 全文搜索客户端

搜索请求使用 requests.Session + Anubis PoW 绕过，
复用 lore_thread_fetcher.py 中的 AnubisPoWSolver。
"""
import email as email_lib, email.policy, json, logging, re, time
from datetime import datetime
from typing import Dict, List, Optional
from urllib.parse import urlencode

import requests

from .config import EMAILS_DIR
from .lore_thread_fetcher import AnubisPoWSolver

logger = logging.getLogger(__name__)
LORE_BASE = "https://lore.kernel.org"

# Anubis pass-challenge 端点
_ANUBIS_PASS_URL = "/.within.website/x/cmd/anubis/api/pass-challenge"

# User-Agent 列表（轮换使用）
# 注意: b4 UA 会被 Anubis 直接 403，必须把浏览器 UA 放在前面
_USER_AGENTS = [
    "Mozilla/5.0 (X11; Linux x86_64; rv:109.0) Gecko/20100101 Firefox/115.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
    " (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "b4/0.14.2",
]

KNOWN_LISTS = {
    "all":"all", "lkml":"all", "linux-kernel":"all",
    "linux-mm":"linux-mm", "linux-sched":"linux-kernel",
    "linux-fsdevel":"linux-fsdevel", "netdev":"netdev",
    "linux-block":"linux-block", "stable":"stable",
}

class LoreClient:
    """通过 lore.kernel.org 进行全文搜索并下载完整邮件（含所有邮件头）。

    使用 requests.Session 维持 cookie，遇到 Anubis PoW 挑战时自动求解。
    """
    def __init__(self, timeout:int=20, delay:float=1.0, max_retries:int=2):
        self.timeout=timeout; self.delay=delay
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

    def search_emails(self, topic:str, list_name:str="all",
                      max_emails:int=20, author:Optional[str]=None,
                      date_from:Optional[str]=None, date_to:Optional[str]=None) -> List[Dict]:
        lst = KNOWN_LISTS.get(list_name, list_name)
        author_kw = author.lower().strip() if author else ""
        query = topic
        if date_from or date_to:
            df=date_from or "2005-01-01"
            dt=date_to or datetime.now().strftime("%Y-%m-%d")
            query=f"({topic}) d:{df}..{dt}"
        logger.info(f"搜索 lore.kernel.org/{lst}: query={query!r}, max={max_emails}")
        results=[]; page=1
        while len(results) < max_emails:
            batch=self._fetch_page(lst, query, page, per_page=min(200,max_emails*3))
            if not batch: break
            for em in batch:
                if len(results)>=max_emails: break
                if author_kw and author_kw not in em.get("from","").lower(): continue
                results.append(em)
            if len(batch)<200: break
            page+=1; time.sleep(self.delay)
        logger.info(f"共获取 {len(results)} 封邮件")
        return results

    def save_emails(self, emails:List[Dict], topic:str) -> List[str]:
        safe=topic.replace(" ","_").replace("/","_")[:50]
        dest=EMAILS_DIR/safe; dest.mkdir(parents=True, exist_ok=True)
        saved=[]; ts=datetime.now().strftime("%Y%m%d_%H%M%S")
        for i,em in enumerate(emails,1):
            path=dest/f"email_{i:03d}_{ts}.json"
            try:
                path.write_text(json.dumps(em,ensure_ascii=False,indent=2),encoding="utf-8")
                saved.append(str(path))
            except Exception as e: logger.error(f"保存失败:{e}")
        logger.info(f"保存 {len(saved)} 封邮件至 {dest}")
        return saved

    def _fetch_page(self, lst:str, query:str, page:int=1, per_page:int=200) -> List[Dict]:
        offset=(page-1)*per_page
        params=urlencode({"q":query,"x":"m","o":str(offset)})
        url=f"{LORE_BASE}/{lst}/?{params}"
        logger.debug(f"请求 {url}")
        # Lore 搜索必须用 POST 才能获取 mbox 格式 (GET 时 x=m 被忽略)
        raw=self._post_search(url)
        if not raw:
            # fallback: 尝试 Atom feed 解析
            atom_params=urlencode({"q":query,"x":"A","o":str(offset)})
            atom_url=f"{LORE_BASE}/{lst}/?{atom_params}"
            logger.debug("mbox POST 失败，尝试 Atom feed: %s", atom_url)
            atom_raw=self._get(atom_url, accept="application/atom+xml")
            return self._parse_atom(atom_raw) if atom_raw else []
        return self._parse_mbox(raw) if raw else []

    def _post_search(self, url:str) -> Optional[str]:
        """用 POST 请求获取 mbox 格式的搜索结果。

        Lore 搜索在 GET 模式下忽略 x=m 参数，必须 POST 才返回 mbox。
        """
        for attempt in range(self.max_retries + 1):
            session = self._get_session(rotate_ua=(attempt > 0))
            if attempt > 0:
                time.sleep(self.delay * attempt)
                logger.info("  POST 搜索重试 %d/%d...", attempt, self.max_retries)
            try:
                # POST with z=results+only 获取 mbox 结果
                resp = session.post(url, data={"z": "results only"},
                                    headers={"Accept": "application/mbox, text/plain"},
                                    timeout=self.timeout)
            except requests.exceptions.Timeout:
                logger.warning("POST 超时 %s", url)
                continue
            except Exception as e:
                logger.warning("POST 失败 %s: %s", url, e)
                continue

            # 检查是否是 mbox 数据
            if resp.status_code == 200 and b"anubis_challenge" not in resp.content:
                ct = resp.headers.get("content-type", "")
                content = resp.content
                # 如果是 gzip
                if content[:2] == b'\x1f\x8b':
                    import gzip
                    try:
                        content = gzip.decompress(content)
                    except Exception:
                        pass
                text = content.decode("utf-8", errors="replace")
                # 验证确实是 mbox 格式 (包含 From 行或邮件头)
                if "From " in text[:500] or "Message-ID:" in text[:2000]:
                    logger.info("POST 获取 mbox 成功 (%d bytes)", len(text))
                    return text
                # 可能返回 HTML 页面
                if "<html" in text[:200].lower():
                    logger.debug("POST 返回 HTML 而非 mbox")
                    # 不算失败，跳出让 fallback 处理
                    return None

            # Anubis 挑战
            if resp.status_code == 200 and b"anubis_challenge" in resp.content:
                logger.info("POST 搜索遇到 Anubis 挑战，求解...")
                solved = self._handle_anubis(resp, url, session)
                if solved and ("From " in solved[:500] or "Message-ID:" in solved[:2000]):
                    return solved
                # PoW 通过但返回非 mbox，跳出让 fallback 处理
                return None

            if resp.status_code == 403:
                # 403 后尝试通过首页触发 Anubis 获取 cookie
                logger.info("POST 403, 尝试通过首页触发 Anubis 获取 cookie...")
                self._trigger_anubis_via_homepage(url, session)
                continue

        return None

    def _parse_atom(self, atom_text:str) -> List[Dict]:
        """解析 Atom XML feed 格式的搜索结果。"""
        results = []
        # 提取每个 <entry>...</entry>
        entries = re.findall(r'<entry>(.*?)</entry>', atom_text, re.DOTALL)
        for entry in entries:
            try:
                subject = self._xml_text(entry, "title")
                author_name = self._xml_text(entry, "name")
                author_email = self._xml_text(entry, "email")
                updated = self._xml_text(entry, "updated")
                # 提取链接
                link_m = re.search(r'<link\s+href="([^"]+)"', entry)
                link = link_m.group(1) if link_m else ""
                # 提取正文 (在 <content> 中的 <pre> 标签内)
                body = ""
                content_m = re.search(r'<pre[^>]*>(.*?)</pre>', entry, re.DOTALL)
                if content_m:
                    body = self._unescape_html(content_m.group(1))
                # 提取 message-id from link
                msg_id = ""
                if link:
                    # link 格式: https://lore.kernel.org/all/<msgid>/
                    mid_m = re.search(r'/all/([^/]+)/', link)
                    if mid_m:
                        msg_id = f"<{mid_m.group(1)}>"
                from_str = f"{author_name} <{author_email}>" if author_email else author_name
                results.append({
                    "subject": subject,
                    "from": from_str,
                    "to": "", "cc": "",
                    "date": updated,
                    "body": body,
                    "attachments": [],
                    "message_id": msg_id,
                    "in_reply_to": "",
                    "references": [],
                    "source_url": link,
                })
            except Exception as e:
                logger.debug("解析 Atom entry 失败: %s", e)
        return results

    @staticmethod
    def _xml_text(xml:str, tag:str) -> str:
        """从 XML 片段提取标签文本"""
        m = re.search(rf'<{tag}[^>]*>(.*?)</{tag}>', xml, re.DOTALL)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _unescape_html(text:str) -> str:
        """反转义 HTML 实体"""
        import html
        return html.unescape(text)

    def _parse_mbox(self, mbox_text:str) -> List[Dict]:
        results=[]
        blocks=re.split(r"(?m)^From \S+ \w{3} \w{3} +\d+ \d+:\d+:\d+ \d{4}$",mbox_text)
        for block in blocks:
            block=block.strip()
            if not block: continue
            try:
                msg=email_lib.message_from_string(block,policy=email_lib.policy.compat32)
                parsed=self._extract(msg)
                if parsed: results.append(parsed)
            except Exception as e: logger.debug(f"解析邮件块失败:{e}")
        return results

    def _extract(self, msg) -> Optional[Dict]:
        subject=self._decode_header(msg.get("Subject",""))
        sender=self._decode_header(msg.get("From",""))
        if not subject and not sender: return None
        date=msg.get("Date",""); msg_id=msg.get("Message-ID","").strip()
        in_reply=msg.get("In-Reply-To","").strip()
        references=msg.get("References","").strip()
        body=self._extract_body(msg)
        source_url=""
        if msg_id:
            source_url=f"{LORE_BASE}/all/{msg_id.strip('<>')}/ "
        # references 拆分为列表
        refs_list = [r.strip() for r in re.split(r'[\s,]+', references) if r.strip()] if references else []
        return {"subject":subject,"from":sender,"to":msg.get("To",""),
                "cc":msg.get("Cc",""),"date":date,"body":body,
                "attachments":[],"message_id":msg_id,
                "in_reply_to":in_reply,"references":refs_list,
                "source_url":source_url.strip()}

    def _extract_body(self, msg) -> str:
        body=""
        if msg.is_multipart():
            for part in msg.walk():
                if (part.get_content_type()=="text/plain"
                        and "attachment" not in str(part.get("Content-Disposition",""))):
                    payload=part.get_payload(decode=True)
                    if payload:
                        charset=part.get_content_charset() or "utf-8"
                        try: body+=payload.decode(charset)
                        except: body+=payload.decode("utf-8",errors="ignore")
        else:
            if msg.get_content_type()=="text/plain":
                payload=msg.get_payload(decode=True)
                if payload:
                    charset=msg.get_content_charset() or "utf-8"
                    try: body=payload.decode(charset)
                    except: body=payload.decode("utf-8",errors="ignore")
        return body.strip()

    @staticmethod
    def _decode_header(value:str) -> str:
        if not value: return ""
        from email.header import decode_header
        parts=[]
        for seg,enc in decode_header(value):
            if isinstance(seg,bytes): parts.append(seg.decode(enc or "utf-8",errors="ignore"))
            else: parts.append(str(seg))
        return "".join(parts)

    def _get(self, url:str, accept:str="text/html") -> Optional[str]:
        """发起 GET 请求，自动处理 Anubis PoW 挑战。

        流程:
        1. 用 requests Session 发 GET
        2. 如果返回正常文本 → 直接返回
        3. 如果返回 Anubis 挑战页 → 求解 PoW → 获取 cookie → 重新请求
        4. 如果 403 → 先访问主页触发 Anubis → 求解 → 再重试
        5. 多次重试 + UA 轮换
        """
        for attempt in range(self.max_retries + 1):
            session = self._get_session(rotate_ua=(attempt > 0))
            if attempt > 0:
                time.sleep(self.delay * attempt)
                logger.info("  Lore 搜索重试 %d/%d (切换 User-Agent)...",
                            attempt, self.max_retries)
            try:
                resp = session.get(url, headers={"Accept": accept},
                                   timeout=self.timeout)
            except requests.exceptions.Timeout:
                logger.warning("请求超时 %s", url)
                continue
            except Exception as e:
                logger.warning("请求失败 %s: %s", url, e)
                continue

            # 正常响应（包含 mbox 数据）
            content = resp.text
            if resp.status_code == 200 and b"anubis_challenge" not in resp.content:
                return content

            # Anubis 挑战页面
            if resp.status_code == 200 and b"anubis_challenge" in resp.content:
                logger.info("检测到 Anubis PoW 挑战，开始求解...")
                solved = self._handle_anubis(resp, url, session)
                if solved:
                    return solved
                # 求解失败，继续重试（换 UA）
                continue

            # 403 — 主动触发 Anubis 挑战（访问首页获取 cookie）
            if resp.status_code == 403:
                logger.info("403 Forbidden，尝试访问首页触发 Anubis 挑战...")
                solved = self._trigger_anubis_via_homepage(url, session)
                if solved:
                    return solved
                continue

            logger.warning("Lore 响应异常: status=%d url=%s", resp.status_code, url)

        logger.error("所有重试均失败，无法获取 %s", url)
        return None

    def _handle_anubis(self, resp: requests.Response, original_url: str,
                       session: requests.Session) -> Optional[str]:
        """处理 Anubis PoW 挑战页，求解后重新请求原始 URL。"""
        # 提取 challenge JSON
        m = re.search(
            r'<script[^>]+id=["\']anubis_challenge["\'][^>]*>(.*?)</script>',
            resp.text, re.DOTALL | re.IGNORECASE
        )
        if not m:
            logger.warning("未找到 Anubis challenge 标签")
            return None

        try:
            challenge_data = json.loads(m.group(1))
        except json.JSONDecodeError:
            logger.warning("Anubis challenge JSON 解析失败")
            return None

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

        logger.info("Anubis challenge: id=%s difficulty=%d", challenge_id, difficulty)

        # 求解 PoW
        t0 = time.time()
        hex_hash, nonce = AnubisPoWSolver.solve(random_data, difficulty)
        elapsed = int((time.time() - t0) * 1000)
        logger.info("PoW 求解完成: nonce=%d elapsed=%dms", nonce, elapsed)

        # 提取 base_prefix
        m2 = re.search(
            r'<script[^>]+id=["\']anubis_base_prefix["\'][^>]*>(.*?)</script>',
            resp.text, re.DOTALL | re.IGNORECASE
        )
        base_prefix = ""
        if m2:
            try:
                base_prefix = json.loads(m2.group(1)) or ""
            except json.JSONDecodeError:
                pass

        # 提交 PoW 结果
        pass_url = f"{LORE_BASE}{base_prefix}{_ANUBIS_PASS_URL}"
        params = {
            "id": challenge_id,
            "response": hex_hash,
            "nonce": str(nonce),
            "redir": original_url,
            "elapsedTime": str(elapsed),
        }

        logger.info("提交 PoW 结果到: %s", pass_url)
        try:
            session.get(pass_url, params=params, timeout=self.timeout,
                        allow_redirects=True)
        except Exception as e:
            logger.error("提交 PoW 失败: %s", e)
            return None

        # 用获得的 cookie 重新请求原始搜索 URL
        time.sleep(0.5)
        try:
            resp2 = session.get(original_url,
                                headers={"Accept": "text/plain, application/mbox"},
                                timeout=self.timeout)
            if resp2.status_code == 200 and b"anubis_challenge" not in resp2.content:
                logger.info("Anubis PoW 通过，成功获取搜索结果")
                return resp2.text
            else:
                logger.warning("PoW 通过后仍未获取有效数据 (status=%d)", resp2.status_code)
        except Exception as e:
            logger.error("PoW 通过后重新请求失败: %s", e)

        return None

    def _trigger_anubis_via_homepage(self, target_url: str,
                                      session: requests.Session) -> Optional[str]:
        """403 时主动访问首页触发 Anubis 挑战，获取 cookie 后重新访问目标 URL。"""
        try:
            home_resp = session.get(f"{LORE_BASE}/", timeout=self.timeout)
        except Exception as e:
            logger.warning("访问首页失败: %s", e)
            return None

        if home_resp.status_code == 200 and b"anubis_challenge" in home_resp.content:
            logger.info("首页返回 Anubis 挑战，开始求解...")
            # 求解首页的 Anubis（redir 设为 /）
            solved = self._handle_anubis(home_resp, "/", session)
            if solved is not None:
                # cookie 已获取，重新请求目标 URL
                time.sleep(0.5)
                try:
                    resp = session.get(target_url, timeout=self.timeout)
                    if resp.status_code == 200 and b"anubis_challenge" not in resp.content:
                        logger.info("通过首页 Anubis 获取 cookie，搜索成功")
                        return resp.text
                    else:
                        logger.warning("首页 Anubis 通过后搜索仍失败 (status=%d)", resp.status_code)
                except Exception as e:
                    logger.error("使用 cookie 重新搜索失败: %s", e)
        elif home_resp.status_code == 200:
            # 首页正常但目标 403，可能是 IP 限制
            logger.warning("首页正常但目标 URL 仍 403，可能是 IP 限制")
        else:
            logger.warning("首页响应异常: status=%d", home_resp.status_code)

        return None
