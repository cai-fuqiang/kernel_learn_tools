"""
Translator - 多后端翻译模块
支持：
  - api    : 直接调用 OpenAI 兼容 HTTP API（OpenAI / DeepSeek / Kimi / 其他）
  - openclaw: 调用本地 OpenClaw CLI（需要已安装 openclaw）
  - google : Google 翻译（免费，支持代理）
  - youdao : 有道翻译（免费，支持代理）
"""

import logging
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Dict, List, Optional, Tuple
from urllib import request as urllib_request
from urllib.error import URLError
import json

from .config import OPENCLAW_CONFIG

logger = logging.getLogger(__name__)


def _build_opener(proxy: Optional[str] = None) -> urllib_request.OpenerDirector:
    """构建 urllib opener，可选代理支持（不影响全局设置）"""
    handlers = []
    if proxy:
        proxy_url = proxy if "://" in proxy else f"http://{proxy}"
        handlers.append(urllib_request.ProxyHandler({
            "http":  proxy_url,
            "https": proxy_url,
        }))
    else:
        # 不使用代理，显式传空 handler 避免从环境变量获取
        handlers.append(urllib_request.ProxyHandler({}))
    return urllib_request.build_opener(*handlers)


TRANSLATE_PROMPT = """请将以下英文邮件内容翻译为中文，要求：
1. 保持原文段落结构
2. 专业术语尽量保留英文并在括号内给出中文解释
3. 只输出翻译结果，不要添加额外说明

--- 邮件主题 ---
{subject}

--- 邮件正文 ---
{body}
"""


# -----------------------------------------------------------------------
# 基类
# -----------------------------------------------------------------------

class BaseTranslator:
    """翻译器基类，子类只需实现 _call(prompt) -> (text, error)"""

    def __init__(self, max_retries: int = 3, batch_size: int = 5,
                 proxy: Optional[str] = None):
        self.max_retries = max_retries
        self.batch_size  = batch_size
        self.proxy       = proxy
        self._opener     = _build_opener(proxy)

    def translate_email(self, email_info: Dict) -> Dict:
        result  = dict(email_info)
        subject = email_info.get("subject", "")
        body    = email_info.get("body", "")

        if not subject and not body:
            result["subject_cn"] = ""
            result["body_cn"]    = ""
            return result

        prompt = TRANSLATE_PROMPT.format(
            subject=subject or "(无主题)",
            body=body    or "(无正文)",
        )

        text, error = self._call_with_retry(prompt)

        if error:
            result["translation_error"] = error
            result["subject_cn"] = ""
            result["body_cn"]    = text or ""
        else:
            result["subject_cn"], result["body_cn"] = self._split(text, subject)

        return result

    def translate_emails(self, emails: List[Dict]) -> List[Dict]:
        total   = len(emails)
        results = []
        logger.info(f"开始翻译 {total} 封邮件，使用 {self.__class__.__name__}")

        for i, em in enumerate(emails, start=1):
            logger.info(f"  [{i}/{total}] {em.get('subject', '')[:50]}")
            results.append(self.translate_email(em))
            if i % self.batch_size == 0 and i < total:
                time.sleep(1)

        logger.info("翻译完成")
        return results

    def translate_text(self, text: str) -> Tuple[str, str]:
        """翻译纯文本，返回 (translated, error)。
        Google/有道子类会覆盖此方法直接调用 _translate_text；
        基类默认走 _call_with_retry。
        """
        return self._call_with_retry(text)

    def _call_with_retry(self, prompt: str) -> Tuple[str, str]:
        for attempt in range(1, self.max_retries + 1):
            text, error = self._call(prompt)
            if not error:
                return text, ""
            logger.warning(f"翻译失败 (尝试 {attempt}/{self.max_retries}): {error}")
            if attempt < self.max_retries:
                time.sleep(2 ** attempt)
        return "", f"已重试 {self.max_retries} 次仍失败：{error}"

    def _call(self, prompt: str) -> Tuple[str, str]:
        raise NotImplementedError

    @staticmethod
    def _split(text: str, orig_subject: str) -> Tuple[str, str]:
        """第一行作为主题译文，其余作为正文译文"""
        lines = text.strip().splitlines()
        if len(lines) >= 2:
            return lines[0].strip(), "\n".join(lines[1:]).strip()
        return orig_subject, text.strip()


# -----------------------------------------------------------------------
# 后端 1：OpenAI 兼容 HTTP API
# -----------------------------------------------------------------------

class APITranslator(BaseTranslator):
    """
    直接调用 OpenAI 兼容 REST API 进行翻译。

    支持的服务（只需改 base_url 和 api_key）：
      - OpenAI      : https://api.openai.com/v1
      - DeepSeek    : https://api.deepseek.com/v1
      - Kimi (Moonshot): https://api.moonshot.cn/v1
      - 硅基流动    : https://api.siliconflow.cn/v1
      - 阿里百炼    : https://dashscope.aliyuncs.com/compatible-mode/v1
      - 任何 OpenAI 兼容接口
    """

    # 常见服务的默认 base_url 和默认模型
    PRESETS = {
        "openai":     ("https://api.openai.com/v1",                  "gpt-4o-mini"),
        "deepseek":   ("https://api.deepseek.com/v1",                "deepseek-chat"),
        "kimi":       ("https://api.moonshot.cn/v1",                 "moonshot-v1-8k"),
        "siliconflow":("https://api.siliconflow.cn/v1",              "deepseek-ai/DeepSeek-V3"),
        "aliyun":     ("https://dashscope.aliyuncs.com/compatible-mode/v1", "qwen-turbo"),
    }

    def __init__(self, api_key: str, model: str = "",
                 base_url: str = "", provider: str = "openai",
                 timeout: int = 60, max_retries: int = 3,
                 batch_size: int = 5, proxy: str = None):
        """
        Args:
            api_key:   API 密钥
            model:     模型名，留空则使用 preset 默认值
            base_url:  API 基础 URL，留空则使用 preset 默认值
            provider:  预设名称 openai/deepseek/kimi/siliconflow/aliyun
            timeout:   请求超时秒数
            proxy:     代理地址（如 127.0.0.1:7897），仅翻译请求使用
        """
        super().__init__(max_retries=max_retries, batch_size=batch_size, proxy=proxy)
        preset_url, preset_model = self.PRESETS.get(provider, self.PRESETS["openai"])
        self.base_url  = (base_url  or preset_url).rstrip("/")
        self.model     = model      or preset_model
        self.api_key   = api_key
        self.timeout   = timeout

    def _call(self, prompt: str) -> Tuple[str, str]:
        url     = f"{self.base_url}/chat/completions"
        payload = json.dumps({
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.3,
        }).encode("utf-8")

        req = urllib_request.Request(
            url,
            data=payload,
            headers={
                "Content-Type":  "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                text = body["choices"][0]["message"]["content"].strip()
                return text, ""
        except URLError as e:
            return "", f"网络请求失败: {e}"
        except (KeyError, IndexError) as e:
            return "", f"解析响应失败: {e}"
        except Exception as e:
            return "", f"未知错误: {e}"


# -----------------------------------------------------------------------
# 后端 2：OpenClaw CLI
# -----------------------------------------------------------------------

class OpenClawTranslator(BaseTranslator):
    """通过本地 OpenClaw CLI 进行翻译"""

    def __init__(self, model: str = "kimi", executable: str = "",
                 timeout: int = 300, max_retries: int = 3,
                 batch_size: int = 5, proxy: str = None):
        super().__init__(max_retries=max_retries, batch_size=batch_size, proxy=proxy)
        cfg = OPENCLAW_CONFIG
        self.model      = model
        self.executable = executable or cfg.get("executable", "openclaw")
        self.timeout    = timeout    or cfg.get("timeout", 300)

    def _call(self, prompt: str) -> Tuple[str, str]:
        cmd = [self.executable, "chat",
               "--prompt", prompt,
               "--model",  self.model,
               "--no-stream"]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True,
                                  timeout=self.timeout)
            if proc.returncode == 0:
                output = proc.stdout.strip()
                return (output, "") if output else ("", "输出为空")
            err = proc.stderr.strip() or f"returncode={proc.returncode}"
            return "", err
        except subprocess.TimeoutExpired:
            return "", "超时"
        except FileNotFoundError:
            return "", (f"未找到 openclaw 可执行文件 '{self.executable}'，"
                        "请确保 OpenClaw 已安装并在 PATH 中")
        except Exception as e:
            return "", str(e)


# -----------------------------------------------------------------------
# 工厂函数：统一入口
# -----------------------------------------------------------------------

# -----------------------------------------------------------------------
# 后端 3：Google 翻译（免费，无需 API Key）
# -----------------------------------------------------------------------

class GoogleTranslator(BaseTranslator):
    """
    使用 Google 翻译非官方接口，免费无需 API Key。
    接口: https://translate.googleapis.com/translate_a/single
    """

    ENDPOINT = "https://translate.googleapis.com/translate_a/single"

    def __init__(self, source: str = "en", target: str = "zh-CN",
                 timeout: int = 30, max_retries: int = 3,
                 batch_size: int = 5, proxy: str = None):
        super().__init__(max_retries=max_retries, batch_size=batch_size, proxy=proxy)
        self.source  = source
        self.target  = target
        self.timeout = timeout

    def translate_email(self, email_info: Dict) -> Dict:
        """覆盖基类：分别翻译主题和正文，不使用 TRANSLATE_PROMPT"""
        result  = dict(email_info)
        subject = email_info.get("subject", "")
        body    = email_info.get("body", "")

        subject_cn, err1 = self._translate_text(subject) if subject else ("", "")
        body_cn,    err2 = self._translate_text(body)    if body    else ("", "")

        errors = [e for e in (err1, err2) if e]
        if errors:
            result["translation_error"] = "; ".join(errors)

        result["subject_cn"] = subject_cn
        result["body_cn"]    = body_cn
        return result

    def _translate_text(self, text: str) -> Tuple[str, str]:
        """调用 Google 翻译接口，返回 (translated, error)"""
        from urllib.parse import urlencode, quote
        # Google 翻译接口每次最多约 5000 字符，超长自动分段
        chunks = self._chunk_text(text, max_len=4000)
        parts  = []
        for chunk in chunks:
            translated, error = self._call_with_retry(chunk)
            if error:
                return "", error
            parts.append(translated)
        return "\n".join(parts), ""

    def translate_text(self, text: str) -> Tuple[str, str]:
        """覆盖基类：使用 Google 翻译的 _translate_text"""
        return self._translate_text(text)

    def _call(self, text: str) -> Tuple[str, str]:
        from urllib.parse import urlencode
        params = urlencode({
            "client": "gtx",
            "sl":     self.source,
            "tl":     self.target,
            "dt":     "t",
            "q":      text,
        })
        url = f"{self.ENDPOINT}?{params}"
        req = urllib_request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0"},
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                # data[0] 是句子翻译列表，每项 [译文, 原文, ...]
                translated = "".join(
                    item[0] for item in data[0] if item[0]
                )
                return translated, ""
        except URLError as e:
            return "", f"Google翻译请求失败: {e}"
        except (IndexError, KeyError, TypeError) as e:
            return "", f"Google翻译解析失败: {e}"
        except Exception as e:
            return "", f"Google翻译未知错误: {e}"

    @staticmethod
    def _chunk_text(text: str, max_len: int = 4000) -> List[str]:
        """将长文本按段落切分，每段不超过 max_len 字符"""
        if len(text) <= max_len:
            return [text]
        chunks, current = [], []
        current_len = 0
        for para in text.splitlines(keepends=True):
            if current_len + len(para) > max_len and current:
                chunks.append("".join(current))
                current, current_len = [], 0
            current.append(para)
            current_len += len(para)
        if current:
            chunks.append("".join(current))
        return chunks or [text]


# -----------------------------------------------------------------------
# 后端 4：有道翻译（免费 Web 接口，无需 API Key）
# -----------------------------------------------------------------------

class YoudaoTranslator(BaseTranslator):
    """
    使用有道翻译 Web 接口，免费无需 API Key。
    接口: https://fanyi.youdao.com/translate
    """

    ENDPOINT = "https://fanyi.youdao.com/translate"

    def __init__(self, source: str = "EN", target: str = "zh-CHS",
                 timeout: int = 30, max_retries: int = 3,
                 batch_size: int = 5, proxy: str = None):
        super().__init__(max_retries=max_retries, batch_size=batch_size, proxy=proxy)
        self.source  = source
        self.target  = target
        self.timeout = timeout

    def translate_email(self, email_info: Dict) -> Dict:
        """覆盖基类：分别翻译主题和正文"""
        result  = dict(email_info)
        subject = email_info.get("subject", "")
        body    = email_info.get("body", "")

        subject_cn, err1 = self._translate_text(subject) if subject else ("", "")
        body_cn,    err2 = self._translate_text(body)    if body    else ("", "")

        errors = [e for e in (err1, err2) if e]
        if errors:
            result["translation_error"] = "; ".join(errors)

        result["subject_cn"] = subject_cn
        result["body_cn"]    = body_cn
        return result

    def _translate_text(self, text: str) -> Tuple[str, str]:
        chunks = GoogleTranslator._chunk_text(text, max_len=2000)
        parts  = []
        for chunk in chunks:
            translated, error = self._call_with_retry(chunk)
            if error:
                return "", error
            parts.append(translated)
        return "\n".join(parts), ""

    def translate_text(self, text: str) -> Tuple[str, str]:
        """覆盖基类：使用有道翻译的 _translate_text"""
        return self._translate_text(text)

    def _call(self, text: str) -> Tuple[str, str]:
        from urllib.parse import urlencode
        import time as _time
        payload = urlencode({
            "i":       text,
            "from":    self.source,
            "to":      self.target,
            "smartresult": "dict",
            "client":  "fanyideskweb",
            "doctype":  "json",
            "version":  "2.1",
            "keyfrom":  "fanyi.web",
            "action":   "FY_BY_CLICKBUTTION",
        }).encode("utf-8")

        req = urllib_request.Request(
            self.ENDPOINT,
            data=payload,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent":   "Mozilla/5.0",
                "Referer":      "https://fanyi.youdao.com/",
            },
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                # 正常响应: {"translateResult": [[{"tgt": "..."}]], ...}
                lines = data.get("translateResult", [])
                translated = "\n".join(
                    item["tgt"]
                    for row in lines
                    for item in row
                    if item.get("tgt")
                )
                return translated, ""
        except URLError as e:
            return "", f"有道翻译请求失败: {e}"
        except (KeyError, IndexError, TypeError) as e:
            return "", f"有道翻译解析失败: {e}"
        except Exception as e:
            return "", f"有道翻译未知错误: {e}"


# -----------------------------------------------------------------------
# 工厂函数：统一入口
# -----------------------------------------------------------------------

def create_translator(backend: str = "api", **kwargs) -> BaseTranslator:
    """
    工厂函数，根据 backend 参数创建对应的翻译器。

    backend="api":
        必填: api_key
        可选: provider (openai/deepseek/kimi/siliconflow/aliyun)
              model, base_url, timeout

    backend="openclaw":
        可选: model, executable, timeout

    backend="google":
        可选: source (default: en), target (default: zh-CN), timeout
        免费无需 API Key

    backend="youdao":
        可选: source (default: EN), target (default: zh-CHS), timeout
        免费无需 API Key

    示例:
        # 使用 DeepSeek API
        t = create_translator("api", provider="deepseek", api_key="sk-xxx")

        # 使用 Google 翻译（免费）
        t = create_translator("google")

        # 使用有道翻译（免费）
        t = create_translator("youdao")

        # 使用 OpenClaw
        t = create_translator("openclaw", model="kimi")
    """
    if backend == "api":
        return APITranslator(**kwargs)
    elif backend == "openclaw":
        return OpenClawTranslator(**kwargs)
    elif backend == "google":
        return GoogleTranslator(**kwargs)
    elif backend == "youdao":
        return YoudaoTranslator(**kwargs)
    else:
        raise ValueError(
            f"未知 backend: {backend!r}，可选 'api' / 'openclaw' / 'google' / 'youdao'"
        )