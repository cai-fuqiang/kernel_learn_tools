"""
Email Client - IMAP 邮件搜索与下载
"""

import imaplib
import email
import email.message
import json
import logging
from datetime import datetime, timedelta
from email.header import decode_header as _decode_header
from typing import List, Dict, Optional
from pathlib import Path

from .config import EMAILS_DIR, EMAIL_PROVIDERS

logger = logging.getLogger(__name__)


class EmailClient:
    """基于 IMAP 的邮件客户端"""

    def __init__(self, user: str, password: str, provider: str = "gmail",
                 custom_server: str = "", custom_port: int = 993):
        """
        初始化邮件客户端

        Args:
            user:           邮箱地址
            password:       密码或授权码
            provider:       服务商 (gmail/outlook/qq/163/aliyun/custom)
            custom_server:  provider="custom" 时指定服务器地址
            custom_port:    provider="custom" 时指定端口
        """
        self.user = user
        self.password = password
        self.conn: Optional[imaplib.IMAP4_SSL] = None

        if provider == "custom":
            self.imap_server = custom_server
            self.imap_port = custom_port
        else:
            cfg = EMAIL_PROVIDERS.get(provider, EMAIL_PROVIDERS["gmail"])
            self.imap_server = cfg["imap_server"]
            self.imap_port = cfg["imap_port"]

    # ------------------------------------------------------------------
    # 连接管理
    # ------------------------------------------------------------------

    def connect(self) -> bool:
        """建立 IMAP SSL 连接并登录"""
        try:
            self.conn = imaplib.IMAP4_SSL(self.imap_server, self.imap_port)
            self.conn.login(self.user, self.password)
            logger.info(f"已登录邮箱: {self.user}")
            return True
        except Exception as e:
            logger.error(f"连接/登录失败: {e}")
            return False

    def disconnect(self):
        """关闭连接"""
        if self.conn:
            try:
                self.conn.close()
                self.conn.logout()
            except Exception:
                pass
            self.conn = None
            logger.info("已断开邮箱连接")

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *args):
        self.disconnect()

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    def search_emails(self, topic: str, folder: str = "INBOX",
                      max_emails: int = 50,
                      date_range_days: int = 30) -> List[Dict]:
        """
        按话题关键词搜索邮件

        Args:
            topic:           话题关键词（会同时搜索主题和正文）
            folder:          邮箱文件夹，默认收件箱
            max_emails:      最多返回数量
            date_range_days: 仅搜索最近 N 天

        Returns:
            邮件信息字典列表
        """
        if not self.conn:
            raise RuntimeError("尚未连接邮箱，请先调用 connect()")

        self.conn.select(folder)

        # 构建 IMAP SEARCH 条件
        criteria_parts = []
        if date_range_days > 0:
            since = (datetime.now() - timedelta(days=date_range_days)).strftime("%d-%b-%Y")
            criteria_parts.append(f"SINCE {since}")

        # 主题或正文包含关键词
        encoded = topic.encode("utf-8").decode("ascii", errors="replace")
        criteria_parts.append(f'OR SUBJECT "{encoded}" BODY "{encoded}"')

        criteria = " ".join(criteria_parts)
        logger.info(f"IMAP SEARCH: {criteria}")

        status, data = self.conn.search("UTF-8", criteria)
        if status != "OK" or not data[0]:
            logger.info("未找到符合条件的邮件")
            return []

        ids = data[0].split()
        # 取最新的 N 封
        ids = ids[-max_emails:]
        logger.info(f"命中 {len(ids)} 封邮件")

        results = []
        for eid in reversed(ids):           # 从新到旧
            try:
                st, msg_data = self.conn.fetch(eid, "(RFC822)")
                if st == "OK":
                    parsed = self._parse(msg_data[0][1])
                    parsed["_imap_id"] = eid.decode()
                    results.append(parsed)
            except Exception as e:
                logger.warning(f"fetch {eid} 失败: {e}")

        return results

    # ------------------------------------------------------------------
    # 解析
    # ------------------------------------------------------------------

    def _parse(self, raw: bytes) -> Dict:
        """解析原始邮件为结构化字典"""
        try:
            msg = email.message_from_bytes(raw)
            subject  = self._decode_str(msg.get("Subject", ""))
            sender   = self._decode_str(msg.get("From", ""))
            to       = self._decode_str(msg.get("To", ""))
            date_str = msg.get("Date", "")

            body, attachments = self._extract_body(msg)

            return {
                "subject":     subject,
                "sender":      sender,
                "to":          to,
                "date":        date_str,
                "body":        body,
                "attachments": attachments,
            }
        except Exception as e:
            logger.error(f"解析邮件失败: {e}")
            return {"subject": "", "sender": "", "to": "",
                    "date": "", "body": "", "attachments": []}

    def _extract_body(self, msg: email.message.Message):
        """提取纯文本正文和附件列表"""
        body = ""
        attachments = []

        if msg.is_multipart():
            for part in msg.walk():
                ct  = part.get_content_type()
                cd  = str(part.get("Content-Disposition", ""))
                if "attachment" in cd:
                    fname = part.get_filename()
                    if fname:
                        attachments.append(self._decode_str(fname))
                elif ct == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        charset = part.get_content_charset() or "utf-8"
                        try:
                            body += payload.decode(charset)
                        except Exception:
                            body += payload.decode("utf-8", errors="ignore")
        else:
            if msg.get_content_type() == "text/plain":
                payload = msg.get_payload(decode=True)
                if payload:
                    charset = msg.get_content_charset() or "utf-8"
                    try:
                        body = payload.decode(charset)
                    except Exception:
                        body = payload.decode("utf-8", errors="ignore")

        return body.strip(), attachments

    def _decode_str(self, header: str) -> str:
        """解码 MIME 编码的邮件头字段"""
        parts = []
        for segment, enc in _decode_header(header):
            if isinstance(segment, bytes):
                parts.append(segment.decode(enc or "utf-8", errors="ignore"))
            else:
                parts.append(str(segment))
        return "".join(parts)

    # ------------------------------------------------------------------
    # 保存到本地
    # ------------------------------------------------------------------

    def save_emails(self, emails: List[Dict], topic: str) -> List[str]:
        """
        将邮件列表保存为 JSON 文件

        Returns:
            保存的文件路径列表
        """
        safe_topic = topic.replace(" ", "_").replace("/", "_")[:50]
        dest_dir = EMAILS_DIR / safe_topic
        dest_dir.mkdir(parents=True, exist_ok=True)

        saved = []
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        for i, em in enumerate(emails, start=1):
            path = dest_dir / f"email_{i:03d}_{ts}.json"
            try:
                path.write_text(json.dumps(em, ensure_ascii=False, indent=2),
                                encoding="utf-8")
                saved.append(str(path))
                logger.debug(f"已保存: {path}")
            except Exception as e:
                logger.error(f"保存失败: {e}")

        logger.info(f"共保存 {len(saved)} 封邮件至 {dest_dir}")
        return saved