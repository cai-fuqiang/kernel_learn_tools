"""Lore Client - lore.kernel.org 全文搜索客户端"""
import email as email_lib, email.policy, json, logging, re, time
from datetime import datetime
from typing import Dict, List, Optional
from urllib import request as urllib_request
from urllib.error import URLError
from urllib.parse import urlencode
from .config import EMAILS_DIR

logger = logging.getLogger(__name__)
LORE_BASE = "https://lore.kernel.org"

KNOWN_LISTS = {
    "all":"all", "lkml":"all", "linux-kernel":"all",
    "linux-mm":"linux-mm", "linux-sched":"linux-kernel",
    "linux-fsdevel":"linux-fsdevel", "netdev":"netdev",
    "linux-block":"linux-block", "stable":"stable",
}

class LoreClient:
    """通过 lore.kernel.org 进行全文搜索并下载完整邮件（含所有邮件头）。"""
    def __init__(self, timeout:int=20, delay:float=1.0):
        self.timeout=timeout; self.delay=delay

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
        params=urlencode({"q":query,"x":"A","o":str(offset)})
        url=f"{LORE_BASE}/{lst}/?{params}"
        logger.debug(f"请求 {url}")
        raw=self._get(url, accept="text/plain, application/mbox")
        return self._parse_mbox(raw) if raw else []

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
        req=urllib_request.Request(url,headers={
            "User-Agent":"Mozilla/5.0 (lkml-knowledge-extractor/2.0)",
            "Accept":accept})
        try:
            with urllib_request.urlopen(req,timeout=self.timeout) as resp:
                raw=resp.read()
                ct=resp.headers.get_content_charset() or "utf-8"
                return raw.decode(ct,errors="ignore")
        except URLError as e: logger.warning(f"请求失败 {url}: {e}"); return None
        except Exception as e: logger.warning(f"未知错误 {url}: {e}"); return None
