#!/usr/bin/env python3
"""translate_context.py — 将 context_full.txt 翻译并整理为可读文档

读取 pack_for_openclaw.py 生成的完整版 context_full.txt，
将英文邮件正文翻译为中文，保留代码/diff 不翻译，
输出一份结构清晰的文档，适合人工阅读和分析。

输出格式：
  - HTML（默认）：自包含 HTML 文件，线程树状缩进，折叠交互
  - Markdown：兼容旧格式

翻译后端：
  1. Google 翻译（免费，默认）
  2. 有道翻译（免费备选）
  3. API（OpenAI/DeepSeek 等，需要 key）

用法:
    # 默认输出 HTML
    python translate_context.py data/output/xxxx_context_full.txt

    # 输出 Markdown（旧格式）
    python translate_context.py data/output/xxxx_context_full.txt --format md

    # 使用有道翻译
    python translate_context.py data/output/xxxx_context_full.txt --backend youdao

    # 只解析不翻译（调试用）
    python translate_context.py data/output/xxxx_context_full.txt --dry-run
"""
import argparse
import hashlib
import html as html_module
import logging
import re
import sys
import time
from pathlib import Path
from typing import List, Tuple, Optional

sys.path.insert(0, str(Path(__file__).parent))
from email_translator.translator import create_translator, BaseTranslator
from email_translator.translation_cache import TranslationCache

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger(__name__)


class CachedTranslator:
    """翻译器缓存包装：先查缓存，未命中才调用真实翻译"""

    def __init__(self, translator: BaseTranslator, cache: TranslationCache, backend: str):
        self._translator = translator
        self._cache = cache
        self._backend = backend
        self.last_cache_hit = False  # 上一次调用是否命中缓存

    def translate_email(self, email_info: dict) -> dict:
        """包装 translate_email，对 body 做缓存"""
        body = email_info.get("body", "")
        if body:
            cached = self._cache.get(self._backend, body)
            if cached is not None:
                self.last_cache_hit = True
                result = dict(email_info)
                result["subject_cn"] = ""
                result["body_cn"] = cached
                return result
        self.last_cache_hit = False
        result = self._translator.translate_email(email_info)
        body_cn = result.get("body_cn", "")
        if body and body_cn and "translation_error" not in result:
            self._cache.put(self._backend, body, body_cn)
        return result

    def translate_text(self, text: str):
        """包装 translate_text，对文本做缓存"""
        if text:
            cached = self._cache.get(self._backend, text)
            if cached is not None:
                self.last_cache_hit = True
                return cached, ""
        self.last_cache_hit = False
        translated, error = self._translator.translate_text(text)
        if text and translated and not error:
            self._cache.put(self._backend, text, translated)
        return translated, error

    def __getattr__(self, name):
        return getattr(self._translator, name)


# ─── 解析 context_full.txt 的各区段 ──────────────────────────────────────────

def parse_commit_section(text: str) -> dict:
    """解析 COMMIT 信息区段"""
    info = {}
    for key in ("Hash", "Subject", "Author", "Date", "Files", "Subsys", "Lore", "Patchset"):
        m = re.search(rf"^{key}\s*:\s*(.+)$", text, re.MULTILINE)
        if m:
            info[key.lower()] = m.group(1).strip()
    m = re.search(r"## Commit Message\n(.+?)(?=\n## |\Z)", text, re.DOTALL)
    if m:
        info["commit_message"] = m.group(1).strip()
    return info


def parse_diff_section(text: str) -> str:
    """提取 diff 代码块"""
    m = re.search(r"## 代码变更 \(diff\)\n```diff\n(.+?)```", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def parse_emails(text: str) -> Tuple[str, List[dict]]:
    """解析邮件线程区段 -> (header_line, emails_list)"""
    m = re.search(
        r"^(={70}\n# Lore 邮件线程.*?\n={70}\n(?:# \[HIGH:.*\n)?)",
        text, re.MULTILINE,
    )
    if not m:
        return "", []

    header = m.group(1).strip()
    rest = text[m.end():]

    tail_m = re.search(r"\n={70}\n# (?:分析任务清单|请基于以上内容)", rest)
    email_body = rest[: tail_m.start()] if tail_m else rest

    blocks = re.split(r"\n(?=## \[)", email_body)
    emails = []
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        em = {"raw": block}
        hm = re.match(r"## (\[[^\]]+\])\s*(.*)", block)
        if hm:
            em["tag"] = hm.group(1)
            em["subject"] = hm.group(2)
        else:
            em["tag"] = ""
            em["subject"] = block.split("\n")[0]
        fm = re.search(r"^From\s*:\s*(.+)$", block, re.MULTILINE)
        dm = re.search(r"^Date\s*:\s*(.+)$", block, re.MULTILINE)
        mid_m = re.search(r"^Message-Id\s*:\s*(.+)$", block, re.MULTILINE)
        irt_m = re.search(r"^In-Reply-To\s*:\s*(.+)$", block, re.MULTILINE)
        em["from"] = fm.group(1).strip() if fm else ""
        em["date"] = dm.group(1).strip() if dm else ""
        em["message_id"] = mid_m.group(1).strip() if mid_m else ""
        em["in_reply_to"] = irt_m.group(1).strip() if irt_m else ""
        # body: 最后一个头字段行之后的内容
        header_fields = [fm, dm, mid_m, irt_m]
        last_hdr_end = max((m.end() for m in header_fields if m), default=0)
        em["body"] = block[last_hdr_end:].strip() if last_hdr_end else ""
        emails.append(em)
    return header, emails


def parse_analysis_checklist(text: str) -> str:
    """提取尾部分析清单"""
    m = re.search(r"(={70}\n# (?:分析任务清单|请基于以上内容).*)", text, re.DOTALL)
    return m.group(1).strip() if m else ""


# ─── 翻译逻辑 ────────────────────────────────────────────────────────────────

def should_translate(body: str) -> bool:
    """判断正文是否需要翻译。

    先分离 diff 代码块，只对正文部分做判断，避免大 patch 邮件被误判为不需要翻译。
    """
    if not body or len(body.strip()) < 20:
        return False
    # 分离 diff，只检查正文部分
    text_part, _ = _split_body_and_diff(body)
    if not text_part or len(text_part.strip()) < 20:
        return False
    lines = text_part.strip().splitlines()
    code_lines = sum(1 for l in lines if l.startswith(("+", "-", "diff ", "@@", "index ")))
    return code_lines <= len(lines) * 0.7



def _is_code_or_data_line(line):
    """判断单行是否是代码/数据行，不应被合并或翻译。"""
    s = line.strip()
    if not s:
        return False
    if re.search(r'\w\+0x[0-9a-f]+(?:/0x[0-9a-f]+)?', s):
        return True
    if re.search(r'\{[.\-]+\}-\{\d+:\d+\}', s):
        return True
    if re.match(r'\s*\S+\.\w+\s+\|\s+\d+', s):
        return True
    if re.search(r'\d+\s+files?\s+changed', s):
        return True
    if re.match(r'\s*(struct\s+\w|#define\s|static\s|void\s|int\s|unsigned\s|'
                r'long\s|char\s|const\s|enum\s|typedef\s|return\s|if\s*\(|'
                r'for\s*\(|while\s*\()', s):
        return True
    if re.match(r'\s*(WARNING:|BUG:|Call Trace:|<TASK>|</TASK>|'
                r'RIP:|RSP:|RAX:|CPU:|PID:|Comm:|Workqueue:)', s):
        return True
    if re.match(r'\s*(kernel|include|arch|drivers|fs|mm|net|lib)/\S+', s):
        return True
    if re.match(r'\s*(0x[0-9a-f]{4,}|[0-9a-f]{12,})\b', s):
        return True
    if re.match(r'\s*->\s*#\d+', s):
        return True
    # 含大数字（带逗号分隔）的数据/统计行，如 "sched  1,243,911  3,947,251"
    if re.search(r'\d{1,3}(?:,\d{3}){1,}', s):
        return True
    # 下划线标识符 + 数字的统计行，如 "tick_check_preempts  12,899,049"
    if re.match(r'\s*\w+_\w+\s+[\d,]+', s):
        return True
    # 表格/数据对齐行：多个单词以多空格分隔（>=2个连续空格），如 "schedstat  Base  EEVDF"
    if re.search(r'\S\s{2,}\S', s) and len(s.split()) <= 8:
        # 额外检查：至少包含一个数字或下划线标识符或全大写词
        if (re.search(r'\d', s) or re.search(r'\w+_\w+', s)
                or any(w.isupper() and len(w) > 1 for w in s.split())):
            return True
    # ASCII 图表行：包含连续 ---|、|---、|<、>字母 等绘图字符
    if re.search(r'[|][-=]{2,}|[-=]{2,}[|]', s):
        return True
    if re.search(r'[|][-=*]+[|]', s):
        return True
    # 包含 >字母 或 字母|< 格式的图表行
    if re.search(r'>[A-Z]\s+\|', s) or re.search(r'\|<', s):
        return True
    # 变量赋值行：t=数字、V=数字、d=数字 等
    if re.match(r'\s*[a-zA-Z]\s*=\s*\d+', s):
        return True
    # 行中大量非字母字符（>60%），如绘图行 "---|---------|-------*-|"
    alpha_count = sum(1 for c in s if c.isalpha())
    if len(s) > 5 and alpha_count < len(s) * 0.3:
        return True

    # ── 以下为新增内核常见输出格式 ──

    # dmesg 输出行：[    0.000000] 或 [12345.678901] 开头
    if re.match(r'\s*\[\s*\d+\.\d+\]', s):
        return True

    # ftrace 输出行：CPU#0 或 funcname-PID 格式，如 "sched_switch-1234"
    if re.match(r'\s*\S+-\d+\s+\[?\d+\]?', s) and ('|' in s or '=>' in s or '<-' in s):
        return True
    # ftrace 函数图格式：含 | 和缩进的函数调用
    if re.match(r'\s*[\d.]+\s*(us|ms|ns)\s*\|', s):
        return True

    # perf report / perf stat 输出
    # "  5.23%  swapper  [kernel.kallsyms]  [k] intel_idle"
    if re.match(r'\s*\d+\.\d+%\s+\S+', s):
        return True
    # perf stat 格式: "1,234,567,890  cycles  (66.67%)"
    if re.match(r'\s*[\d,]+\s+\S+\s+\([\d.]+%\)', s):
        return True

    # 内核 oops/panic 寄存器和栈帧
    # "RBX: 0000000000000000 RCX: ffffffff81234567"
    if re.match(r'\s*(R[A-Z]{1,2}|CR[0-4]|DR[0-7]|FS|GS|CS|SS|EFLAGS):', s):
        return True
    # 栈帧行 "[<ffffffff81234567>] function_name+0x12/0x34"
    if re.match(r'\s*\[<[0-9a-f]+>\]', s):
        return True
    # "? function_name+0x12/0x34 [module]"
    if re.match(r'\s*\?\s+\S+\+0x[0-9a-f]+', s):
        return True

    # lockdep 输出
    if re.match(r'\s*(->|<-)#\d+', s) or re.search(r'lock_type:\s*\d+', s):
        return True

    # sysfs / procfs 路径行
    if re.match(r'\s*/proc/|/sys/', s):
        return True

    # 配置选项行：CONFIG_XXX=y/m/n
    if re.match(r'\s*CONFIG_[A-Z0-9_]+=', s):
        return True

    # Git 日志行：commit hash 行
    if re.match(r'\s*commit [0-9a-f]{7,40}\b', s):
        return True
    # Git shortlog 格式："Author Name (N):"
    if re.match(r'\s*\S.*\(\d+\):$', s):
        return True

    # 邮件引用 + 代码混合行："> +    some_code();"
    if re.match(r'\s*>\s*[+\-]', s):
        return True

    # 纯分隔线 (>=4 个连续相同符号)
    _sep_end = '$'  # end-of-line anchor
    if re.match(r'\s*[=~#*]{4,}\s*' + _sep_end, s):
        return True
    if re.match(r'\s*-{4,}\s*' + _sep_end, s):
        return True
    # lockdep 依赖链: "-> #4 (&rq->__lock){-.-.}-{2:2}:"
    if re.match(r'\s*->\s*#\d+\s+\(', s):
        return True
    # 缩进的函数调用栈: "       _raw_spin_lock_nested+0x44/0x5c"
    if re.match(r'\s{4,}\S+\+0x[0-9a-f]+', s):
        return True
    # 锁场景表格: "CPU0"
    if re.match(r'\s*CPU\d+\s*' + _sep_end, s):
        return True
    # "lock(&xxx)" 行
    if re.match(r'\s*lock\(', s):
        return True
    # lockdep 标题
    if re.match(r'\s*(Chain exists of|Possible unsafe|other info that might)', s):
        return True
    # "N locks held by process/PID:"
    if re.match(r'\s*\d+\s+locks?\s+held\s+by\s+', s):
        return True
    # "#0: ffff..." 格式的锁列表
    if re.match(r'\s*#\d+:\s+[0-9a-f]', s):
        return True
    # Hardware name / Not tainted 行
    if re.match(r'\s*(Hardware name:|Not tainted)', s):
        return True
    # ftrace 事件行: "bprint: ... tid="
    if re.match(r'\s*\w+:\s+(prev_comm|next_comm|tid=|eligible=)', s):
        return True
    # 命令行示例: "./program ..."
    if re.match(r'\s*\./', s):
        return True
    # EEVDF 模拟输出: "t=数字 V=数字"
    if re.match(r'\s*t=\d+\s+V=\d+', s):
        return True

    return False


def _merge_soft_linebreaks(text, placeholders):
    """合并段落内的软换行，保留代码/数据行不合并。"""
    if not text or '\n' not in text:
        return text
    ph_set = set(placeholders.keys())
    lines = text.split('\n')
    merged = []
    buf = []
    def _flush():
        if buf:
            merged.append(' '.join(buf))
            buf.clear()
    for line in lines:
        stripped = line.strip()
        if not stripped:
            _flush(); merged.append(''); continue
        if any(ph in stripped for ph in ph_set):
            _flush(); merged.append(line); continue
        if stripped.startswith(('>', '+', '-', 'diff ', '@@', 'index ',
                                'Signed-off-by:', 'Reviewed-by:', 'Acked-by:',
                                'Tested-by:', 'Cc:', 'Link:', 'Fixes:',
                                'Reported-by:')):
            _flush(); merged.append(line); continue
        if _is_code_or_data_line(line):
            _flush(); merged.append(line); continue
        if re.match(r'^\s*(\d+[.)]\s|[*\u2022]\s)', stripped):
            _flush()
        buf.append(stripped)
    _flush()
    return '\n'.join(merged)


def _clean_translation_artifacts(text):
    """清理翻译引擎引入的孤立反引号。"""
    bt = chr(96)
    lines = text.split('\n')
    out = []
    for line in lines:
        s = line.strip()
        if s == bt:
            continue
        if s.endswith(bt) and len(s) > 1:
            line = line.rstrip()[:-1].rstrip()
        out.append(line)
    return '\n'.join(out)


def _extract_comment_text(comment_lines: list) -> str:
    """从多行注释行中提取纯文本内容"""
    texts = []
    for line in comment_lines:
        s = line.lstrip()
        if s.startswith(('+', '-', ' ')):
            s = s[1:]
        s = s.strip()
        # 去掉注释标记
        s = re.sub(r'^/\*+\s*', '', s)
        s = re.sub(r'\s*\*+/$', '', s)
        s = re.sub(r'^\*\s?', '', s)
        s = s.strip()
        if s:
            texts.append(s)
    return ' '.join(texts)


def _translate_diff_comments(translator: 'BaseTranslator', diff_text: str) -> str:
    """Translate C code comments in diff, keep code unchanged."""
    if not diff_text or not translator:
        return diff_text

    lines = diff_text.split('\n')
    result = []
    i = 0
    in_multiline = False
    multiline_buf = []       # 收集多行注释的行
    multiline_indices = []   # 对应的行索引

    while i < len(lines):
        line = lines[i]

        # 跳过 diff 元数据行（不检查注释）
        if line.startswith(('diff --git', 'index ', '--- ', '+++ ', '@@ ')):
            if in_multiline:
                # 多行注释被截断，放弃翻译
                result.extend(multiline_buf)
                in_multiline = False
                multiline_buf = []
            result.append(line)
            i += 1
            continue

        # 获取代码行内容（去掉 +/- 前缀来检查注释）
        stripped = line
        prefix = ''
        if line.startswith(('+', '-', ' ')):
            prefix = line[0]
            stripped = line[1:]

        if in_multiline:
            multiline_buf.append(line)
            # 检查是否结束多行注释
            if '*/' in stripped:
                in_multiline = False
                # 提取完整注释文本
                comment_text = _extract_comment_text(multiline_buf)
                if comment_text and len(comment_text) > 10:
                    try:
                        cn = translator.translate_email({"subject": "", "body": comment_text})
                        cn_text = cn.get("body_cn", "")
                        if cn_text and cn_text != comment_text:
                            result.extend(multiline_buf)
                            result.append(f'\x00CN\x00{prefix}/* {cn_text} */')
                            multiline_buf = []
                            i += 1
                            continue
                    except Exception:
                        pass
                result.extend(multiline_buf)
                multiline_buf = []
            i += 1
            continue

        # 检查多行注释开始 /* ... (无 */ 结尾)
        if '/*' in stripped and '*/' not in stripped:
            in_multiline = True
            multiline_buf = [line]
            i += 1
            continue

        # 单行注释：行内 /* ... */
        m = re.search(r'/\*(.+?)\*/', stripped)
        if m:
            comment_body = m.group(1).strip()
            if len(comment_body) > 10 and re.search(r'[a-zA-Z]{3,}', comment_body):
                try:
                    cn = translator.translate_email({"subject": "", "body": comment_body})
                    cn_text = cn.get("body_cn", "")
                    if cn_text and cn_text != comment_body:
                        result.append(line)
                        result.append(f'\x00CN\x00{prefix}/* {cn_text} */')
                        i += 1
                        continue
                except Exception:
                    pass

        # 单行注释：// ...
        m2 = re.search(r'//\s*(.+)', stripped)
        if m2:
            comment_body = m2.group(1).strip()
            if len(comment_body) > 10 and re.search(r'[a-zA-Z]{3,}', comment_body):
                try:
                    cn = translator.translate_email({"subject": "", "body": comment_body})
                    cn_text = cn.get("body_cn", "")
                    if cn_text and cn_text != comment_body:
                        result.append(line)
                        result.append(f'\x00CN\x00{prefix}// {cn_text}')
                        i += 1
                        continue
                except Exception:
                    pass

        result.append(line)
        i += 1

    # 如果多行注释在文件末尾未闭合
    if multiline_buf:
        result.extend(multiline_buf)

    return '\n'.join(result)


def translate_body(translator: BaseTranslator, body: str) -> str:
    """翻译邮件正文，按段落逐段翻译，保留代码块和引用折叠标记不翻译"""
    if not should_translate(body):
        return body

    # ── 第一步：按空行拆分为段落 ──
    raw_paragraphs = re.split(r'(\n\n+)', body)
    # raw_paragraphs 交替包含 [段落, 分隔符, 段落, 分隔符, ...]

    # 提取非空段落列表用于上下文感知判断
    content_parts = [p for p in raw_paragraphs if p.strip()]

    result_parts = []
    for part in raw_paragraphs:
        # 保留空行分隔符原样
        if not part.strip():
            result_parts.append(part)
            continue

        # ── 上下文感知：获取前后非空段落 ──
        part_idx = content_parts.index(part) if part in content_parts else -1
        prev_part = content_parts[part_idx - 1] if part_idx > 0 else ""
        next_part = content_parts[part_idx + 1] if part_idx >= 0 and part_idx < len(content_parts) - 1 else ""

        # 上下文感知判断：短标题夹在数据段落之间 → 不翻译
        if _is_untranslatable_in_context(part.strip(), prev_part.strip(), next_part.strip()):
            result_parts.append(part)
            continue

        # ── 判断段落是否需要翻译 ──
        lines = part.strip().splitlines()
        # 全部是引用行 / diff行 / 签名行 / 代码数据行 → 不翻译
        is_code_or_quote = all(
            l.lstrip().startswith(('>', '+', '-', 'diff ', '@@', 'index '))
            or l.strip().startswith((
                'Signed-off-by:', 'Reviewed-by:', 'Acked-by:',
                'Tested-by:', 'Cc:', 'Link:', 'Fixes:', 'Reported-by:',
            ))
            or _is_code_or_data_line(l)
            or not l.strip()
            for l in lines
        )
        if is_code_or_quote:
            result_parts.append(part)
            continue

        # 段落中代码/数据行占多数 → 整段不翻译
        if lines:
            code_count = sum(1 for l in lines if _is_code_or_data_line(l))
            if code_count >= len(lines) * 0.5:  # 从 0.6 降低到 0.5，更保守
                result_parts.append(part)
                continue

        # 折叠标记不翻译
        if part.strip().startswith('[...') and part.strip().endswith('...]'):
            result_parts.append(part)
            continue

        # 代码块不翻译
        if part.strip().startswith('```'):
            result_parts.append(part)
            continue

        # ── 段落内部保护占位 ──
        placeholders = {}
        counter = [0]

        def _ph(match):
            key = "XYZPH%04dEND" % counter[0]
            counter[0] += 1
            placeholders[key] = match.group(0)
            return key

        protected = part
        protected = re.sub(r"```[\s\S]*?```", _ph, protected)
        protected = re.sub(r"\[\.\.\..*?行引用已省略\.\.\.\]", _ph, protected)
        protected = re.sub(r"^>.*$", _ph, protected, flags=re.MULTILINE)
        protected = re.sub(r"^[+-].*$", _ph, protected, flags=re.MULTILINE)
        protected = re.sub(
            r"^(Signed-off-by|Reviewed-by|Acked-by|Tested-by|Cc|Link):.*$",
            _ph, protected, flags=re.MULTILINE,
        )

        # 保护段落内的代码/数据行（逐行检查并占位）
        _plines = []
        for _ln in protected.split('\n'):
            if _ln.strip() and not any(k in _ln for k in placeholders) and _is_code_or_data_line(_ln):
                key = "XYZPH%04dEND" % counter[0]
                counter[0] += 1
                placeholders[key] = _ln
                _plines.append(key)
            else:
                _plines.append(_ln)
        protected = '\n'.join(_plines)

        # 如果保护后只剩占位符，不翻译
        stripped = protected
        for key in placeholders:
            stripped = stripped.replace(key, "")
        if not stripped.strip():
            result_parts.append(part)
            continue

        # ── 合并段落内软换行 ──
        merged = _merge_soft_linebreaks(protected, placeholders)

        # ── 翻译该段落 ──
        translated = translator.translate_email({"subject": "", "body": merged})
        output = translated.get("body_cn", "") or protected

        # 还原占位符
        for key, val in placeholders.items():
            output = output.replace(key, val)
            lower_key = key.lower()
            if lower_key != key and lower_key in output.lower():
                output = re.sub(re.escape(key), lambda m, v=val: v, output, flags=re.IGNORECASE)

        output = _clean_translation_artifacts(output)
        result_parts.append(output)

    return "".join(result_parts)


def translate_body_aligned(translator: 'BaseTranslator', body: str,
                           progress_cb=None) -> 'List[Tuple[str, Optional[str]]]':
    """按段落翻译邮件正文，返回对齐好的 (英文段落, 中文翻译或None) 列表。

    与 translate_body 使用完全相同的段落拆分和判断逻辑，但输出结构化的
    段落对齐列表，消除渲染阶段的 DP 对齐需求。

    Args:
        translator: 翻译器实例
        body: 邮件正文
        progress_cb: 可选回调 progress_cb(done, total, is_translated)，用于进度输出

    返回:
        List of (en_paragraph, cn_paragraph_or_None)
        - cn 为 None 表示该段落不需要翻译（代码/引用/签名等）
        - cn 为 str 表示翻译后的中文文本
    """
    if not should_translate(body):
        # 整个 body 不需要翻译，按空行拆段，全部标记为不翻译
        paras = re.split(r'\n\n+', body.strip())
        return [(p, None) for p in paras if p.strip()]

    # ── 按空行拆分为段落（只保留非空段落）──
    raw_paragraphs = re.split(r'(\n\n+)', body)
    content_parts = [p for p in raw_paragraphs if p.strip()]
    total_parts = len(content_parts)
    done_parts = 0

    aligned = []  # List[(en_para, cn_or_None)]

    # 预建 id(part) → index 映射，避免 .index() 的 O(N) 线性搜索
    _id_to_idx = {id(p): i for i, p in enumerate(content_parts)}
    _content_idx = 0  # 滑动索引，跟踪当前处理到 content_parts 的哪个位置

    for part in raw_paragraphs:
        # 跳过空行分隔符
        if not part.strip():
            continue

        # ── 上下文感知判断 ──
        # 使用 id() 映射 + 滑动索引，O(1) 查找
        part_idx = _id_to_idx.get(id(part), _content_idx)
        _content_idx = part_idx + 1
        prev_part = content_parts[part_idx - 1] if part_idx > 0 else ""
        next_part = content_parts[part_idx + 1] if part_idx < len(content_parts) - 1 else ""

        if _is_untranslatable_in_context(part.strip(), prev_part.strip(), next_part.strip()):
            aligned.append((part, None))
            continue

        # ── 判断段落是否需要翻译 ──
        lines = part.strip().splitlines()
        is_code_or_quote = all(
            l.lstrip().startswith(('>', '+', '-', 'diff ', '@@', 'index '))
            or l.strip().startswith((
                'Signed-off-by:', 'Reviewed-by:', 'Acked-by:',
                'Tested-by:', 'Cc:', 'Link:', 'Fixes:', 'Reported-by:',
            ))
            or _is_code_or_data_line(l)
            or not l.strip()
            for l in lines
        )
        if is_code_or_quote:
            aligned.append((part, None))
            continue

        if lines:
            code_count = sum(1 for l in lines if _is_code_or_data_line(l))
            if code_count >= len(lines) * 0.5:
                aligned.append((part, None))
                continue

        if part.strip().startswith('[...') and part.strip().endswith('...]'):
            aligned.append((part, None))
            continue

        if part.strip().startswith('```'):
            aligned.append((part, None))
            continue

        # ── 段落内部保护占位 ──
        placeholders = {}
        counter = [0]

        def _ph(match, _c=counter, _p=placeholders):
            key = "XYZPH%04dEND" % _c[0]
            _c[0] += 1
            _p[key] = match.group(0)
            return key

        protected = part
        protected = re.sub(r"```[\s\S]*?```", _ph, protected)
        protected = re.sub(r"\[\.\.\..*?行引用已省略\.\.\.\]", _ph, protected)
        protected = re.sub(r"^>.*$", _ph, protected, flags=re.MULTILINE)
        protected = re.sub(r"^[+-].*$", _ph, protected, flags=re.MULTILINE)
        protected = re.sub(
            r"^(Signed-off-by|Reviewed-by|Acked-by|Tested-by|Cc|Link):.*$",
            _ph, protected, flags=re.MULTILINE,
        )

        _plines = []
        for _ln in protected.split('\n'):
            if _ln.strip() and not any(k in _ln for k in placeholders) and _is_code_or_data_line(_ln):
                key = "XYZPH%04dEND" % counter[0]
                counter[0] += 1
                placeholders[key] = _ln
                _plines.append(key)
            else:
                _plines.append(_ln)
        protected = '\n'.join(_plines)

        stripped = protected
        for key in placeholders:
            stripped = stripped.replace(key, "")
        if not stripped.strip():
            aligned.append((part, None))
            continue

        # ── 合并段落内软换行 ──
        merged = _merge_soft_linebreaks(protected, placeholders)

        # ── 翻译 ──
        tr = translator.translate_email({"subject": "", "body": merged})
        output = tr.get("body_cn", "") or ""

        done_parts += 1
        if output:
            # 还原占位符
            for key, val in placeholders.items():
                output = output.replace(key, val)
                lower_key = key.lower()
                if lower_key != key and lower_key in output.lower():
                    output = re.sub(re.escape(key), lambda m, v=val: v, output, flags=re.IGNORECASE)
            output = _clean_translation_artifacts(output)
            aligned.append((part, output))
            if progress_cb:
                progress_cb(done_parts, total_parts, True)
        else:
            aligned.append((part, None))
            if progress_cb:
                progress_cb(done_parts, total_parts, False)

    return aligned if aligned else [(body, None)]


# ─── 线程构建 ─────────────────────────────────────────────────────────────────

def _base_subject(subject: str) -> str:
    """去掉 Re: 前缀，得到基础 subject"""
    return re.sub(r"^(Re:\s*)+", "", subject, flags=re.IGNORECASE).strip()


def _reply_depth(subject: str) -> int:
    """计算 Re: 的层数"""
    return len(re.findall(r"Re:", subject, re.IGNORECASE))


class ThreadNode:
    """邮件线程树节点"""
    __slots__ = ("email", "children", "depth")

    def __init__(self, email: dict, depth: int = 0):
        self.email = email
        self.depth = depth
        self.children: List["ThreadNode"] = []

    @property
    def subject(self) -> str:
        return self.email.get("subject", "")

    @property
    def author(self) -> str:
        f = self.email.get("from", "")
        return f.split("<")[0].strip() or f

    def total_count(self, _visited: set = None) -> int:
        if _visited is None:
            _visited = set()
        nid = id(self)
        if nid in _visited:
            return 0
        _visited.add(nid)
        return 1 + sum(c.total_count(_visited) for c in self.children)


def _normalize_subject(subject: str) -> str:
    """标准化 subject 用于模糊分组：去 Re:、去尾部空白、截断到合理长度"""
    s = re.sub(r"^(Re:\s*)+", "", subject, flags=re.IGNORECASE).strip()
    # 去掉可能被截断的尾部不完整单词
    # 截断到 60 字符对齐（避免同一 subject 因长度不同被分成多组）
    if len(s) > 60:
        s = s[:60]
        # 回退到最后一个空格，避免截断在单词中间
        last_space = s.rfind(" ")
        if last_space > 40:
            s = s[:last_space]
    return s


def _patch_sort_key(subject: str) -> tuple:
    """提取 PATCH 编号用于排序: [PATCH 03/15] → (0, 3)，非 patch → (1, 0)"""
    m = re.search(r"\[(?:RFC\]\[)?PATCH(?:\s+v\d+)?\s+(\d+)/(\d+)\]", subject)
    if m:
        return (0, int(m.group(1)))
    # cover letter [PATCH 00/15]
    m = re.search(r"\[PATCH(?:\s+v\d+)?\s+0+/(\d+)\]", subject)
    if m:
        return (0, -1)
    return (1, 0)


def _extract_reply_target(body: str) -> str:
    """从邮件正文中提取引用行里被回复者的 email 地址或名字。

    匹配 "On ..., Foo Bar <foo@bar> wrote:" 或 "On ..., Foo Bar wrote:" 模式。
    返回 email 地址（优先）或名字。
    """
    m = re.search(
        r"^On .+?,\s*(.+?)\s+wrote:\s*$",
        body, re.MULTILINE,
    )
    if not m:
        return ""
    who = m.group(1).strip()
    # 提取 <email> 部分
    em = re.search(r"<([^>]+)>", who)
    if em:
        return em.group(1).lower()
    # 退化为名字
    return who.lower()


def _build_thread_tree(emails: List[dict]) -> List[dict]:
    """将扁平邮件列表按 message_id/in_reply_to 组织成线程树。

    策略（按优先级）：
      1. 用 in_reply_to → message_id 建立精确父子关系
      2. 退化到正文引用行 "On ..., X wrote:" 匹配同组内的发件人
      3. 最终退化到按时间顺序挂在根节点下

    Returns:
        [{"base_subject": str, "roots": [ThreadNode], "count": int}, ...]
    """
    # ── 模糊分组：用标准化后的 subject 聚合 ──
    norm_to_canonical: dict[str, str] = {}
    groups: dict[str, list] = {}
    for em in emails:
        bs = _base_subject(em.get("subject", ""))
        ns = _normalize_subject(em.get("subject", ""))
        if ns not in norm_to_canonical:
            norm_to_canonical[ns] = bs
        canonical = norm_to_canonical[ns]
        groups.setdefault(canonical, []).append(em)

    threads = []
    for bs, members in groups.items():
        # 按时间排序，确保父节点先被处理
        members.sort(key=lambda e: e.get("date", ""))

        nodes = []
        mid_map: dict[str, ThreadNode] = {}  # 本组的 message_id 索引

        for em in members:
            node = ThreadNode(em, _reply_depth(em.get("subject", "")))
            nodes.append(node)
            mid = em.get("message_id", "")
            if mid:
                mid_map[mid] = node

        # 第一轮：用 in_reply_to 在组内精确匹配
        # 注意：只在同一 subject 组内匹配，不跨组（patchset 各 PATCH 会
        # in_reply_to 指向 cover letter，但它们应该作为独立线程展示）
        roots = []
        orphans = []  # 有 in_reply_to 但组内找不到父节点的
        for node in nodes:
            irt = node.email.get("in_reply_to", "")
            if not irt:
                # 没有 in_reply_to → 根节点候选
                if _reply_depth(node.subject) == 0:
                    roots.append(node)
                else:
                    orphans.append(node)
                continue
            parent = mid_map.get(irt)
            if parent:
                parent.children.append(node)
            else:
                # in_reply_to 指向组外（如 cover letter） → 当作根或 orphan
                if _reply_depth(node.subject) == 0:
                    roots.append(node)
                else:
                    orphans.append(node)

        # 第二轮：对 orphans 用正文引用行 "On ..., X wrote:" 匹配
        still_orphan = []
        for node in orphans:
            target = _extract_reply_target(node.email.get("body", ""))
            if not target:
                still_orphan.append(node)
                continue
            # 在本组中按时间倒序找最近的匹配发件人
            matched = False
            # 候选：时间早于当前节点的同组邮件
            candidates = [
                n for n in nodes
                if n is not node
                and n.email.get("date", "") < node.email.get("date", "")
            ]
            # 倒序遍历（最近的优先）
            for candidate in reversed(candidates):
                c_from = candidate.email.get("from", "").lower()
                if target in c_from or c_from.split("<")[0].strip().lower().startswith(target.split()[0] if " " in target else target):
                    candidate.children.append(node)
                    matched = True
                    break
            if not matched:
                still_orphan.append(node)

        # 第三轮：剩余 orphan 按时间顺序挂到最近的根节点
        if still_orphan and roots:
            for node in still_orphan:
                # 挂到时间最接近的根
                best = roots[0]
                for r in roots:
                    if r.email.get("date", "") <= node.email.get("date", ""):
                        best = r
                best.children.append(node)
        elif still_orphan and not roots:
            # 没有根节点，把最早的 orphan 当根
            still_orphan.sort(key=lambda n: n.email.get("date", ""))
            roots.append(still_orphan[0])
            for node in still_orphan[1:]:
                roots[0].children.append(node)

        if not roots:
            # 兜底：所有节点都通过 in_reply_to 挂上了，找没被挂的作为根
            children_set = set()
            for n in nodes:
                for c in n.children:
                    children_set.add(id(c))
            roots = [n for n in nodes if id(n) not in children_set]
            if not roots and nodes:
                roots = [nodes[0]]

        # 递归排序子节点（按时间）
        _sort_children(roots)

        total = sum(r.total_count() for r in roots)
        threads.append({"base_subject": bs, "roots": roots, "count": total})

    # ── 排序：patchset 按编号，其他按邮件数 ──
    threads.sort(key=lambda t: (
        _patch_sort_key(t["base_subject"]),  # patch 按编号
        -t["count"],                          # 非 patch 按热度
    ))
    return threads


def _sort_children(nodes: List[ThreadNode], _visited: set = None):
    """递归地对子节点按时间排序（带循环检测）"""
    if _visited is None:
        _visited = set()
    for node in nodes:
        nid = id(node)
        if nid in _visited:
            continue
        _visited.add(nid)
        node.children.sort(key=lambda n: n.email.get("date", ""))
        _sort_children(node.children, _visited)


# ─── HTML 模板 ─────────────────────────────────────────────────────────────────

_HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta name="source-hash" content="{source_hash}">
<title>{title}</title>
<style>
:root {{
  --bg: #0d1117; --surface: #161b22; --border: #30363d;
  --text: #e6edf3; --text-muted: #8b949e; --accent: #58a6ff;
  --red: #f85149; --orange: #d29922; --green: #3fb950; --gray: #6c757d;
  --sidebar-width: 280px;
}}
/* 亮色主题 */
body.light-theme {{
  --bg: #ffffff; --surface: #f6f8fa; --border: #d0d7de;
  --text: #1f2328; --text-muted: #656d76; --accent: #0969da;
  --red: #cf222e; --orange: #bf8700; --green: #1a7f37; --gray: #6c757d;
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  font-family: -apple-system, 'Segoe UI', Roboto, sans-serif;
  background: var(--bg); color: var(--text);
  line-height: 1.6; max-width: 1400px; margin: 0 auto; padding: 24px;
  transition: background 0.3s, color 0.3s;
}}

/* ── 工具栏 ── */
.toolbar {{
  position: sticky; top: 0; z-index: 90;
  background: var(--surface); border-bottom: 1px solid var(--border);
  padding: 8px 16px; margin: -24px -24px 16px;
  display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
  box-shadow: 0 1px 4px rgba(0,0,0,0.2);
}}
.toolbar .search-box {{
  flex: 1; min-width: 200px; max-width: 400px; position: relative;
}}
.toolbar .search-box input {{
  width: 100%; padding: 6px 32px 6px 10px; font-size: 13px;
  border: 1px solid var(--border); border-radius: 6px;
  background: var(--bg); color: var(--text);
  outline: none; transition: border-color 0.2s;
}}
.toolbar .search-box input:focus {{ border-color: var(--accent); }}
.toolbar .search-box .clear-btn {{
  position: absolute; right: 8px; top: 50%; transform: translateY(-50%);
  background: none; border: none; color: var(--text-muted); cursor: pointer;
  font-size: 14px; display: none; padding: 0 2px;
}}
.toolbar .search-box input:not(:placeholder-shown) ~ .clear-btn {{ display: block; }}
.toolbar .filter-group {{
  display: flex; align-items: center; gap: 6px;
}}
.toolbar select {{
  padding: 5px 8px; font-size: 12px; border: 1px solid var(--border);
  border-radius: 6px; background: var(--bg); color: var(--text); cursor: pointer;
}}
.toolbar .theme-toggle {{
  margin-left: auto; background: none; border: 1px solid var(--border);
  border-radius: 6px; padding: 5px 10px; font-size: 13px;
  color: var(--text-muted); cursor: pointer; white-space: nowrap;
}}
.toolbar .theme-toggle:hover {{ background: var(--border); color: var(--text); }}
.toolbar .sidebar-toggle {{
  background: none; border: 1px solid var(--border); border-radius: 6px;
  padding: 5px 10px; font-size: 13px; color: var(--text-muted); cursor: pointer;
}}
.toolbar .sidebar-toggle:hover {{ background: var(--border); color: var(--text); }}

/* ── 侧边栏目录 ── */
.sidebar-overlay {{
  position: fixed; top: 0; left: 0; right: 0; bottom: 0;
  background: rgba(0,0,0,0.4); z-index: 199;
  display: none;
}}
.sidebar-overlay.active {{ display: block; }}
.sidebar {{
  position: fixed; top: 0; right: -320px; width: var(--sidebar-width);
  height: 100vh; background: var(--surface); border-left: 1px solid var(--border);
  z-index: 200; padding: 16px; overflow-y: auto;
  transition: right 0.25s ease;
  box-shadow: -4px 0 16px rgba(0,0,0,0.3);
}}
.sidebar.active {{ right: 0; }}
.sidebar h3 {{
  font-size: 14px; color: var(--accent); margin-bottom: 12px;
  padding-bottom: 8px; border-bottom: 1px solid var(--border);
}}
.sidebar .toc-item {{
  display: block; padding: 6px 8px; font-size: 13px; margin: 2px 0;
  border-radius: 4px; color: var(--text); text-decoration: none;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  cursor: pointer; transition: background 0.15s;
}}
.sidebar .toc-item:hover {{ background: var(--border); }}
.sidebar .toc-item.active {{ background: rgba(88,166,255,0.15); color: var(--accent); }}
.sidebar .toc-item .toc-count {{
  font-size: 11px; color: var(--text-muted); margin-left: 4px;
}}
.sidebar .close-sidebar {{
  position: absolute; top: 12px; right: 12px;
  background: none; border: none; color: var(--text-muted); font-size: 18px;
  cursor: pointer;
}}
.sidebar .close-sidebar:hover {{ color: var(--text); }}

/* ── 键盘导航高亮 ── */
.email-node.kb-active > .email-card,
.email-node.kb-active > details > .email-card {{
  outline: 2px solid var(--accent); outline-offset: -2px;
  border-radius: 8px;
}}
/* 搜索匹配高亮 */
.email-node.search-hidden {{ display: none; }}
.search-no-result {{
  text-align: center; padding: 40px; color: var(--text-muted); font-size: 14px;
  display: none;
}}
.search-no-result.active {{ display: block; }}
/* 快捷键帮助 */
.kbd-hint {{
  position: fixed; bottom: 16px; right: 16px; z-index: 80;
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 16px; font-size: 12px;
  color: var(--text-muted); box-shadow: 0 2px 8px rgba(0,0,0,0.3);
  display: none;
}}
.kbd-hint.active {{ display: block; }}
.kbd-hint kbd {{
  display: inline-block; padding: 1px 6px; border: 1px solid var(--border);
  border-radius: 3px; background: var(--bg); font-family: monospace;
  font-size: 11px; margin: 0 2px;
}}
h1 {{ font-size: 1.6em; margin-bottom: 16px; border-bottom: 1px solid var(--border); padding-bottom: 12px; }}
h2 {{ font-size: 1.3em; margin: 28px 0 12px; color: var(--accent); }}
h3.thread-title {{
  font-size: 1.1em; margin: 20px 0 8px; padding: 8px 12px;
  background: var(--surface); border-radius: 6px; border-left: 3px solid var(--accent);
}}
table {{ border-collapse: collapse; width: 100%; margin: 8px 0 16px; }}
th, td {{ padding: 6px 12px; border: 1px solid var(--border); text-align: left; }}
th {{ background: var(--surface); width: 120px; color: var(--text-muted); font-weight: 500; }}
td {{ word-break: break-all; }}
a {{ color: var(--accent); text-decoration: none; }}
a:hover {{ text-decoration: underline; }}

/* 邮件卡片 */
.email-card {{
  background: var(--surface); border: 1px solid var(--border);
  border-radius: 8px; padding: 12px 16px; margin: 8px 0;
}}
.email-header {{
  display: flex; align-items: center; gap: 8px; margin-bottom: 8px;
  flex-wrap: wrap;
}}
.avatar {{
  width: 28px; height: 28px; border-radius: 50%; display: inline-flex;
  align-items: center; justify-content: center; color: #fff;
  font-size: 13px; font-weight: 600; flex-shrink: 0;
}}
.author {{ font-weight: 600; font-size: 14px; }}
.tag {{
  display: inline-block; padding: 1px 8px; border-radius: 10px;
  font-size: 11px; color: #fff; font-weight: 500;
}}
.date {{ color: var(--text-muted); font-size: 12px; margin-left: auto; }}
.email-body pre {{
  font-family: 'SF Mono', 'Consolas', monospace; font-size: 13px;
  white-space: pre-wrap; word-wrap: break-word; line-height: 1.5;
  color: var(--text); background: transparent; margin: 0;
}}
.count-badge {{
  display: inline-block; background: var(--accent); color: #fff;
  font-size: 11px; padding: 0 6px; border-radius: 10px; margin-left: 6px;
  font-weight: 600;
}}

/* 回复树缩进 */
.replies {{
  margin-left: 20px; padding-left: 16px;
  border-left: 2px solid var(--border);
}}

/* details 折叠 */
details {{ margin: 4px 0; }}
details > summary {{
  cursor: pointer; padding: 6px 10px; border-radius: 6px;
  font-size: 13px; color: var(--text-muted);
  list-style: none;
}}
details > summary::-webkit-details-marker {{ display: none; }}
details > summary::before {{
  content: '▶ '; font-size: 10px; transition: transform 0.2s;
  display: inline-block;
}}
details[open] > summary::before {{ content: '▼ '; }}
details > summary:hover {{ background: var(--surface); }}

details.reply-thread > summary {{
  font-size: 13px; padding: 5px 8px; color: var(--text);
}}
details.original > summary {{
  font-size: 12px; color: var(--text-muted); padding: 4px 8px;
}}
pre.original-text {{
  font-size: 12px; color: var(--text-muted); white-space: pre-wrap;
  word-wrap: break-word; padding: 8px; margin: 4px 0;
  background: rgba(255,255,255,0.03); border-radius: 4px;
}}

/* Diff */
pre.diff {{
  font-family: 'SF Mono', 'Consolas', monospace; font-size: 12px;
  background: var(--surface); padding: 16px; border-radius: 8px;
  overflow-x: auto; white-space: pre; border: 1px solid var(--border);
  line-height: 1.45;
}}

/* Diff block in email card */
details.diff-block {{
  margin-top: 8px; border: 1px solid var(--border); border-radius: 6px;
  overflow: hidden;
}}
details.diff-block > summary {{
  font-size: 12px; padding: 6px 12px; cursor: pointer;
  background: rgba(88,166,255,0.08); color: var(--accent);
  font-weight: 500; border-bottom: 1px solid var(--border);
}}
details.diff-block > summary:hover {{ background: rgba(88,166,255,0.15); }}
details.diff-block > pre.diff {{
  margin: 0; border: none; border-radius: 0;
}}

/* Commit message */
pre.commit-msg {{
  font-size: 13px; white-space: pre-wrap; word-wrap: break-word;
  padding: 12px; background: var(--surface); border-radius: 8px;
  border: 1px solid var(--border); margin: 8px 0;
}}

/* 清单 */
pre.checklist {{
  font-size: 13px; white-space: pre-wrap; word-wrap: break-word;
  padding: 16px; background: var(--surface); border-radius: 8px;
  border: 1px solid var(--border); margin: 16px 0;
}}

/* 统计栏 */
.stats {{
  background: var(--surface); padding: 8px 16px; border-radius: 6px;
  margin: 8px 0 16px; font-size: 13px; color: var(--text-muted);
  display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
}}

/* 线程 */
.thread {{ margin-bottom: 16px; }}

/* 展开/收起全部按钮 */
.controls {{
  margin: 8px 0; display: flex; gap: 8px;
}}
.controls button {{
  padding: 4px 12px; font-size: 12px; border-radius: 4px;
  border: 1px solid var(--border); background: var(--surface);
  color: var(--text-muted); cursor: pointer;
}}
.controls button:hover {{ background: var(--border); color: var(--text); }}

/* 段落对齐网格: 左=英文原文, 右=中文翻译 */
.para-grid {{
  display: grid; grid-template-columns: 1fr 1fr; width: 100%;
  border: 1px solid var(--border); border-radius: 8px; overflow: hidden;
  margin: 4px 0;
}}
.para-grid .pg-cell {{
  padding: 6px 12px; vertical-align: top;
  border-bottom: 1px solid rgba(48,54,61,0.5);
}}
.para-grid .pg-cell:nth-child(odd) {{ border-right: 1px solid var(--border); }}
.para-grid .pg-cell pre {{
  font-family: 'SF Mono','Consolas',monospace; font-size: 13px;
  white-space: pre-wrap; word-wrap: break-word; line-height: 1.5;
  color: var(--text-muted); background: transparent; margin: 0; font-size: 12px;
}}
.para-grid .pg-cell.pg-cn pre {{ color: var(--text); font-size: 13px; }}
.para-grid .pg-left {{
  padding: 6px 12px; border-right: 1px solid var(--border);
  border-bottom: 1px solid rgba(48,54,61,0.5);
}}
.para-grid .pg-left pre {{
  font-family: 'SF Mono','Consolas',monospace; font-size: 13px;
  white-space: pre-wrap; word-wrap: break-word; line-height: 1.5;
  color: var(--text-muted); background: transparent; margin: 0; font-size: 12px;
}}
.para-grid .pg-spacer {{
  border-bottom: 1px solid rgba(48,54,61,0.5);
}}

/* 响应式：窄屏切上下布局 */
@media (max-width: 768px) {{
  .para-grid {{ grid-template-columns: 1fr; }}
  .para-grid .pg-cell:nth-child(odd) {{ border-right: none; }}
  .para-grid .pg-left {{ border-right: none; }}
  .para-grid .pg-spacer {{ display: none; }}
}}

/* Diff 翻译注释行高亮 */
pre.diff .diff-comment-cn {{
  color: var(--green); font-style: italic; opacity: 0.85;
}}

/* ── 聚焦视图 ── */
.focus-btn {{
  background: transparent; border: 1px solid var(--border); border-radius: 4px;
  color: var(--text-muted); cursor: pointer; font-size: 12px; padding: 2px 6px;
  margin-left: 4px; transition: all 0.15s;
}}
.focus-btn:hover {{ background: var(--border); color: var(--accent); }}
.focus-bar {{
  position: sticky; top: 0; z-index: 100;
  background: var(--surface); border-bottom: 2px solid var(--accent);
  padding: 8px 16px; display: none; align-items: center; gap: 10px;
  font-size: 14px; box-shadow: 0 2px 8px rgba(0,0,0,0.4);
}}
.focus-bar.active {{ display: flex; }}
.focus-bar .back-btn {{
  background: var(--accent); color: #fff; border: none; border-radius: 4px;
  padding: 4px 12px; font-size: 12px; cursor: pointer; font-weight: 600;
  white-space: nowrap;
}}
.focus-bar .back-btn:hover {{ opacity: 0.85; }}
.focus-bar .breadcrumb {{ color: var(--text-muted); font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
body.focusing .page-content {{ display: none; }}
#focusContainer {{ display: none; }}
#focusContainer.active {{ display: block; }}
#focusContainer .replies {{ margin-left: 20px; padding-left: 16px; border-left: 2px solid var(--border); }}
</style>
</head>
<body>
<!-- 工具栏 -->
<div class="toolbar" id="toolbar">
  <button class="sidebar-toggle" onclick="toggleSidebar()" title="目录大纲">&#9776; 目录</button>
  <div class="search-box">
    <input type="text" id="searchInput" placeholder="搜索邮件（作者、关键词）..." oninput="onSearchInput(this.value)">
    <button class="clear-btn" onclick="clearSearch()">&#10005;</button>
  </div>
  <div class="filter-group">
    <select id="authorFilter" onchange="applyFilters()">
      <option value="">全部作者</option>
    </select>
    <select id="priorityFilter" onchange="applyFilters()">
      <option value="">全部类型</option>
      <option value="讨论">讨论</option>
      <option value="概述/数据">概述/数据</option>
      <option value="PATCH摘要">PATCH摘要</option>
    </select>
  </div>
  <button class="theme-toggle" id="themeToggle" onclick="toggleTheme()">&#9728; 亮色</button>
</div>

<!-- 侧边栏 -->
<div class="sidebar-overlay" id="sidebarOverlay" onclick="toggleSidebar()"></div>
<div class="sidebar" id="sidebar">
  <button class="close-sidebar" onclick="toggleSidebar()">&#10005;</button>
  <h3>邮件线程目录</h3>
  <div id="tocList"></div>
</div>

<!-- 聚焦栏 -->
<div class="focus-bar" id="focusBar">
  <button class="back-btn" onclick="unfocusEmail()">&#8592; 返回全部</button>
  <span class="breadcrumb" id="focusBreadcrumb"></span>
</div>
<div id="focusContainer"></div>

<!-- 搜索无结果提示 -->
<div class="search-no-result" id="searchNoResult">没有找到匹配的邮件</div>

<!-- 快捷键提示 -->
<div class="kbd-hint" id="kbdHint">
  <kbd>j</kbd>/<kbd>k</kbd> 上下浏览 &nbsp; <kbd>Enter</kbd> 展开/折叠 &nbsp;
  <kbd>f</kbd> 聚焦 &nbsp; <kbd>/</kbd> 搜索 &nbsp; <kbd>Esc</kbd> 关闭 &nbsp;
  <kbd>?</kbd> 帮助
</div>

<div class="page-content">
<h1>{title}</h1>

<h2>Commit 信息</h2>
<table>{commit_rows}</table>
{cm_html}

<h2>代码变更</h2>
{diff_html}

<h2>邮件讨论</h2>
<div class="stats">{stats_html}</div>
<div class="controls">
  <button onclick="document.querySelectorAll('details.reply-thread').forEach(d=>d.open=true)">展开全部回复</button>
  <button onclick="document.querySelectorAll('details.reply-thread').forEach(d=>d.open=false)">收起全部回复</button>
</div>
{threads_html}
</div><!-- .page-content -->

<script>
/* ── 聚焦视图 ── */
function focusEmail(nodeId) {{
  var node = document.getElementById(nodeId);
  if (!node) return;
  window._focusPrevScroll = window.scrollY;
  var container = document.getElementById('focusContainer');
  container.innerHTML = '';
  var clone = node.cloneNode(true);
  clone.removeAttribute('id');
  clone.querySelectorAll('details').forEach(function(d){{ d.open = true; }});
  container.appendChild(clone);
  container.classList.add('active');
  document.body.classList.add('focusing');
  var bar = document.getElementById('focusBar');
  var bc = document.getElementById('focusBreadcrumb');
  var author = node.getAttribute('data-author') || '';
  var subject = node.getAttribute('data-subject') || '';
  bc.textContent = author + ' \u2014 ' + subject;
  bar.classList.add('active');
  window.scrollTo(0, 0);
}}
function unfocusEmail() {{
  document.body.classList.remove('focusing');
  var container = document.getElementById('focusContainer');
  container.innerHTML = '';
  container.classList.remove('active');
  document.getElementById('focusBar').classList.remove('active');
  if (window._focusPrevScroll !== undefined) window.scrollTo(0, window._focusPrevScroll);
}}

/* ── 主题切换 ── */
function toggleTheme() {{
  var body = document.body;
  var btn = document.getElementById('themeToggle');
  if (body.classList.contains('light-theme')) {{
    body.classList.remove('light-theme');
    btn.innerHTML = '&#9728; 亮色';
    localStorage.setItem('theme', 'dark');
  }} else {{
    body.classList.add('light-theme');
    btn.innerHTML = '&#9790; 暗色';
    localStorage.setItem('theme', 'light');
  }}
}}
// 初始化主题
(function() {{
  if (localStorage.getItem('theme') === 'light') {{
    document.body.classList.add('light-theme');
    document.getElementById('themeToggle').innerHTML = '&#9790; 暗色';
  }}
}})();

/* ── 侧边栏目录 ── */
function toggleSidebar() {{
  document.getElementById('sidebar').classList.toggle('active');
  document.getElementById('sidebarOverlay').classList.toggle('active');
}}
function buildTOC() {{
  var tocList = document.getElementById('tocList');
  var threads = document.querySelectorAll('.page-content .thread');
  tocList.innerHTML = '';
  threads.forEach(function(t, idx) {{
    var titleEl = t.querySelector('.thread-title');
    if (!titleEl) return;
    var title = titleEl.textContent.replace(/\\s*\\d+\\s*$/, '').trim();
    var count = t.querySelectorAll('.email-node').length;
    var id = 'thread-toc-' + idx;
    t.setAttribute('id', id);
    var a = document.createElement('a');
    a.className = 'toc-item';
    a.textContent = title;
    a.innerHTML += ' <span class="toc-count">(' + count + ')</span>';
    a.onclick = function() {{
      document.getElementById(id).scrollIntoView({{ behavior: 'smooth', block: 'start' }});
      toggleSidebar();
    }};
    tocList.appendChild(a);
  }});
}}

/* ── 搜索/过滤 ── */
var _searchTimer = null;
function onSearchInput(val) {{
  clearTimeout(_searchTimer);
  _searchTimer = setTimeout(function() {{ applyFilters(); }}, 200);
}}
function clearSearch() {{
  document.getElementById('searchInput').value = '';
  applyFilters();
}}
function applyFilters() {{
  var query = (document.getElementById('searchInput').value || '').toLowerCase().trim();
  var authorVal = document.getElementById('authorFilter').value;
  var prioVal = document.getElementById('priorityFilter').value;
  var nodes = document.querySelectorAll('.page-content .email-node');
  var visibleCount = 0;

  nodes.forEach(function(node) {{
    var author = (node.getAttribute('data-author') || '').toLowerCase();
    var subject = (node.getAttribute('data-subject') || '').toLowerCase();
    var tag = (node.getAttribute('data-tag') || '');
    var bodyText = (node.getAttribute('data-body-preview') || '').toLowerCase();
    var show = true;
    // 关键词过滤
    if (query && !(author.indexOf(query) >= 0 || subject.indexOf(query) >= 0 || bodyText.indexOf(query) >= 0)) {{
      show = false;
    }}
    // 作者过滤
    if (authorVal && author.indexOf(authorVal.toLowerCase()) < 0) {{
      show = false;
    }}
    // 类型过滤
    if (prioVal && tag.indexOf(prioVal) < 0) {{
      show = false;
    }}
    if (show) {{
      node.classList.remove('search-hidden');
      visibleCount++;
    }} else {{
      node.classList.add('search-hidden');
    }}
  }});

  var noResult = document.getElementById('searchNoResult');
  if ((query || authorVal || prioVal) && visibleCount === 0) {{
    noResult.classList.add('active');
  }} else {{
    noResult.classList.remove('active');
  }}
}}
function buildAuthorFilter() {{
  var nodes = document.querySelectorAll('.page-content .email-node');
  var authors = {{}};
  nodes.forEach(function(n) {{
    var a = n.getAttribute('data-author');
    if (a) authors[a] = (authors[a] || 0) + 1;
  }});
  var sel = document.getElementById('authorFilter');
  Object.keys(authors).sort().forEach(function(a) {{
    var opt = document.createElement('option');
    opt.value = a;
    opt.textContent = a + ' (' + authors[a] + ')';
    sel.appendChild(opt);
  }});
}}

/* ── 键盘快捷键 ── */
var _kbIdx = -1;
function getVisibleNodes() {{
  return Array.from(document.querySelectorAll('.page-content .email-node:not(.search-hidden)'));
}}
function kbNavigate(dir) {{
  var nodes = getVisibleNodes();
  if (!nodes.length) return;
  // 清除旧高亮
  if (_kbIdx >= 0 && _kbIdx < nodes.length) nodes[_kbIdx].classList.remove('kb-active');
  _kbIdx += dir;
  if (_kbIdx < 0) _kbIdx = 0;
  if (_kbIdx >= nodes.length) _kbIdx = nodes.length - 1;
  var cur = nodes[_kbIdx];
  cur.classList.add('kb-active');
  cur.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
}}
function kbToggle() {{
  var nodes = getVisibleNodes();
  if (_kbIdx < 0 || _kbIdx >= nodes.length) return;
  var det = nodes[_kbIdx].querySelector(':scope > details.reply-thread');
  if (det) det.open = !det.open;
}}
function kbFocus() {{
  var nodes = getVisibleNodes();
  if (_kbIdx < 0 || _kbIdx >= nodes.length) return;
  var id = nodes[_kbIdx].id;
  if (id) focusEmail(id);
}}
document.addEventListener('keydown', function(e) {{
  // 忽略在 input 中的按键
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA' || e.target.tagName === 'SELECT') {{
    if (e.key === 'Escape') {{
      e.target.blur();
      e.preventDefault();
    }}
    return;
  }}
  switch(e.key) {{
    case 'j': kbNavigate(1); e.preventDefault(); break;
    case 'k': kbNavigate(-1); e.preventDefault(); break;
    case 'Enter': kbToggle(); e.preventDefault(); break;
    case 'f': kbFocus(); e.preventDefault(); break;
    case '/':
      document.getElementById('searchInput').focus();
      e.preventDefault(); break;
    case 'Escape':
      if (document.body.classList.contains('focusing')) {{ unfocusEmail(); }}
      else if (document.getElementById('sidebar').classList.contains('active')) {{ toggleSidebar(); }}
      e.preventDefault(); break;
    case '?':
      document.getElementById('kbdHint').classList.toggle('active');
      e.preventDefault(); break;
  }}
}});

/* ── 初始化 ── */
document.addEventListener('DOMContentLoaded', function() {{
  buildTOC();
  buildAuthorFilter();
}});
</script>
</body>
</html>"""


# ─── HTML 生成 ─────────────────────────────────────────────────────────────────

TAG_COLORS = {
    "[讨论]": ("#dc3545", "讨论"),
    "[概述/数据]": ("#fd7e14", "概述/数据"),
    "[PATCH摘要]": ("#6c757d", "PATCH摘要"),
}


def _esc(text: str) -> str:
    """HTML 转义"""
    return html_module.escape(text, quote=True)


def _esc_diff(text: str) -> str:
    """HTML 转义 diff 文本，对翻译注释行加高亮 span"""
    # 先按行处理，识别特殊标记行
    CN_MARKER = '\x00CN\x00'
    lines = text.split('\n')
    result = []
    for line in lines:
        if line.startswith(CN_MARKER):
            # 翻译注释行：去掉标记，转义内容，用 span 包裹
            content = html_module.escape(line[len(CN_MARKER):], quote=True)
            result.append(f'<span class="diff-comment-cn">{content}</span>')
        else:
            result.append(html_module.escape(line, quote=True))
    return '\n'.join(result)


def _split_body_and_diff(body: str) -> tuple:
    """从邮件 body 中分离出 diff 代码块。

    支持两种格式：
      1. ```diff ... ``` 围栏格式
      2. 裸露 diff（以 '--- a/' 开头的连续 diff 行）

    Returns:
        (text_part, diff_part) — text_part 是正文，diff_part 是 diff 代码
    """
    # 格式1：```diff ... ``` 围栏块
    m = re.search(r'```diff\s*\n(.*?)```', body, re.DOTALL)
    if m:
        text_part = body[:m.start()].rstrip()
        diff_part = m.group(1).strip()
        after = body[m.end():].strip()
        if after:
            text_part = text_part + "\n" + after
        return text_part, diff_part

    # 格式2：裸露 diff — 从 diffstat 或 '--- a/' 开始到末尾
    # 优化策略：先定位 "N file(s) changed" 行，再向上回溯找 diffstat 起始位置
    # 避免在整个大文本上做昂贵的重复组正则匹配
    diff_start = None
    _fc_pat = re.compile(r"^\s*\d+\s+files?\s+changed", re.MULTILINE)
    _fc_match = _fc_pat.search(body)
    if _fc_match:
        # 从 "files changed" 行向上找连续的 "file.ext | NNN" diffstat 行
        _fc_line_start = body.rfind("\n", 0, _fc_match.start())
        _fc_line_start = _fc_line_start + 1 if _fc_line_start >= 0 else 0
        _diffstat_re = re.compile(r"^[ \t]*\S+[\w./]+\s+\|\s+\d+")
        _pos = _fc_line_start
        while _pos > 0:
            _prev_nl = body.rfind("\n", 0, _pos - 1)
            _line_start = _prev_nl + 1 if _prev_nl >= 0 else 0
            _line = body[_line_start:_pos - 1]
            if _diffstat_re.match(_line):
                _pos = _line_start
            else:
                break
        if _pos < _fc_line_start:
            diff_start = _pos
            # 向上吸收 git format-patch 的 "---" 分隔线
            if _pos > 0:
                _sep_nl = body.rfind("\n", 0, _pos - 1)
                _sep_start = _sep_nl + 1 if _sep_nl >= 0 else 0
                _sep_line = body[_sep_start:_pos].rstrip()
                if _sep_line == "---":
                    diff_start = _sep_start
    if diff_start is None:
        # 直接找 '--- a/' 开头
        m2 = re.search(r"^--- a/", body, re.MULTILINE)
        if m2:
            diff_start = m2.start()
            # 同样向上吸收 diffstat（如果有的话）
            if _fc_match and _fc_match.start() < m2.start():
                # diffstat 在 '--- a/' 前面，回退到 diffstat 起始
                _fc_line_start = body.rfind("\n", 0, _fc_match.start())
                _fc_line_start = _fc_line_start + 1 if _fc_line_start >= 0 else 0
                _diffstat_re = re.compile(r"^[ \t]*\S+[\w./]+\s+\|\s+\d+")
                _pos = _fc_line_start
                while _pos > 0:
                    _prev_nl = body.rfind("\n", 0, _pos - 1)
                    _line_start = _prev_nl + 1 if _prev_nl >= 0 else 0
                    _line = body[_line_start:_pos - 1]
                    if _diffstat_re.match(_line):
                        _pos = _line_start
                    else:
                        break
                if _pos < _fc_line_start:
                    diff_start = _pos
                # 向上吸收 "---" 分隔线
                if diff_start > 0:
                    _sep_nl = body.rfind("\n", 0, diff_start - 1)
                    _sep_start = _sep_nl + 1 if _sep_nl >= 0 else 0
                    _sep_line = body[_sep_start:diff_start].rstrip()
                    if _sep_line == "---":
                        diff_start = _sep_start

    if diff_start is not None:
        text_part = body[:diff_start].rstrip()
        diff_part = body[diff_start:].strip()
        return text_part, diff_part

    return body, ""


def _split_paragraphs(text: str) -> List[str]:
    """将正文按空行拆分为段落列表，保留代码/引用块的完整性"""
    if not text:
        return []
    # 按连续空行分段
    raw_paras = re.split(r'\n\n+', text.strip())
    return [p for p in raw_paras if p.strip()]


def _is_untranslatable(para: str) -> bool:
    """判断段落是否不需要翻译（引用、签名行、代码、diff、数据等），应横跨两栏"""
    lines = para.strip().splitlines()
    if not lines:
        return False
    non_empty = [l for l in lines if l.strip()]
    if not non_empty:
        return True

    # 全部是 > 引用行
    if all(l.lstrip().startswith('>') for l in non_empty):
        return True
    # 全部是签名行
    sig_prefixes = ('Signed-off-by:', 'Reviewed-by:', 'Acked-by:',
                    'Tested-by:', 'Cc:', 'Link:', 'Fixes:', 'Reported-by:')
    if all(l.strip().startswith(sig_prefixes) for l in non_empty):
        return True
    # 折叠标记
    s = para.strip()
    if s.startswith('[...') and s.endswith('...]'):
        return True
    # 折叠标记 + 引用行混合（如 "[...N行引用已省略...]\n> xxx"）
    if any(l.strip().startswith('[...') and '...]' in l for l in non_empty):
        other = [l for l in non_empty if not (l.strip().startswith('[...') and '...]' in l.strip())]
        if all(l.lstrip().startswith('>') for l in other):
            return True
    # diff / 代码 / 数据行
    if all(_is_code_or_data_line(l) or l.lstrip().startswith(('>', '+', '-', 'diff ', '@@', 'index ')) or not l.strip() for l in lines):
        return True
    # 代码块标记
    if s.startswith('```'):
        return True
    # 纯 URL 行
    if all(l.strip().startswith(('http://', 'https://', 'git://')) for l in non_empty):
        return True
    # 以 - 开头的数据行（如 "-0.5% 500.perlbench_r"）
    if all(re.match(r'\s*-?\d+\.?\d*%\s+\S+', l.strip()) for l in non_empty):
        return True
    # 多数行是代码/数据/引用 → 整段不翻译
    if len(non_empty) > 1:
        untrans_count = sum(1 for l in non_empty
                           if _is_code_or_data_line(l)
                           or l.lstrip().startswith(('>', '+', '-', 'diff ', '@@', 'index '))
                           or l.strip().startswith(sig_prefixes)
                           or (l.strip().startswith('[...') and '...]' in l.strip()))
        if untrans_count >= len(non_empty) * 0.6:
            return True
    # 段落整体非字母字符比例高（>70%）→ ASCII 图表/数据表
    all_text = ' '.join(non_empty)
    alpha_count = sum(1 for c in all_text if c.isalpha())
    if len(all_text) > 20 and alpha_count < len(all_text) * 0.3:
        return True
    return False


def _is_untranslatable_in_context(para: str, prev_para: str, next_para: str) -> bool:
    """上下文感知版本：判断段落是否不需要翻译。

    除了 _is_untranslatable 的基础判断外，还考虑上下文：
      - 短段落（<= 5 个单词）夹在不可翻译段落之间 → 数据表标题，不翻译
      - 像 "Base: xxx" "EEVDF: xxx" 这样的标签行紧挨着数据段落 → 不翻译
    """
    if _is_untranslatable(para):
        return True

    s = para.strip()
    lines = s.splitlines()
    non_empty = [l for l in lines if l.strip()]
    word_count = len(s.split())

    prev_untrans = bool(prev_para) and _is_untranslatable(prev_para)
    next_untrans = bool(next_para) and _is_untranslatable(next_para)

    # 短段落（<= 5 个单词）夹在不可翻译段落之间 → 数据表标题/小节标题
    if word_count <= 5 and (prev_untrans or next_untrans):
        return True

    # "Label: value" 格式行紧邻数据段落（如 "Base: v6.5-rc4-based kernel"）
    if len(non_empty) <= 2 and all(re.match(r'^\s*\w[\w\s]*:\s+\S', l) for l in non_empty):
        if prev_untrans or next_untrans:
            return True

    return False


def _fuzzy_match_untranslatable(cn_para: str, en_para: str) -> bool:
    """模糊判断中文侧段落是否与英文侧不可翻译段落匹配（用于同步锚点）。

    采用多种策略：
      1. strip 后完全相等
      2. 压缩空白后相等
      3. 行集合高度重叠（>= 80%）
    """
    cs, es = cn_para.strip(), en_para.strip()
    if cs == es:
        return True
    # 压缩连续空白
    cs_compact = re.sub(r'\s+', ' ', cs)
    es_compact = re.sub(r'\s+', ' ', es)
    if cs_compact == es_compact:
        return True
    # 行集合重叠
    cn_lines = set(l.strip() for l in cs.splitlines() if l.strip())
    en_lines = set(l.strip() for l in es.splitlines() if l.strip())
    if en_lines and cn_lines:
        overlap = len(cn_lines & en_lines)
        if overlap >= len(en_lines) * 0.8:
            return True
    return False


def _is_cn_translation_of(cn_para: str, en_para: str) -> bool:
    """判断 cn_para 是否看起来是 en_para 的中文翻译。

    用于渲染阶段验证左右对齐的正确性。
    如果中文段落与英文段落内容过度重叠（未被翻译），返回 False。
    """
    cs = cn_para.strip()
    es = en_para.strip()
    if not cs or not es:
        return False
    # 完全相同 → 未翻译
    if cs == es:
        return False
    # 压缩空白后相同 → 未翻译
    cs_c = re.sub(r'\s+', ' ', cs)
    es_c = re.sub(r'\s+', ' ', es)
    if cs_c == es_c:
        return False
    # 行集合高度重叠 (>= 80%) → 未翻译
    cn_lines = set(l.strip() for l in cs.splitlines() if l.strip())
    en_lines = set(l.strip() for l in es.splitlines() if l.strip())
    if en_lines and cn_lines:
        overlap = len(cn_lines & en_lines)
        if overlap >= len(en_lines) * 0.8:
            return False
    # 中文段落应含有中文字符（或至少与英文不同）
    return True


def _translation_similarity(cn_para: str, en_para: str) -> float:
    """估算 CN 段落与 EN 段落的翻译相似度分数。

    分数越高表示越可能是翻译关系。用于在多个候选 CN 段落中选最佳匹配。
    返回 0.0~1.0 的分数。
    """
    cs = cn_para.strip()
    es = en_para.strip()
    if not cs or not es:
        return 0.0

    # 完全相同或行集合高度重叠 → 不是翻译
    if not _is_cn_translation_of(cs, es):
        return 0.0

    score = 0.0

    # 1. 长度比例相似（中文字符计为2，因为英文单词平均5字符）
    cn_len = sum(2 if '\u4e00' <= c <= '\u9fff' else 1 for c in cs)
    en_len = len(es)
    if max(cn_len, en_len) > 0:
        len_ratio = min(cn_len, en_len) / max(cn_len, en_len)
        if len_ratio < 0.15:
            return 0.0  # 长度差异过大，不可能是翻译
        score += len_ratio * 0.3

    # 2. 语义单元数比例（翻译不会大幅改变语义量）
    #    中文：每个汉字≈0.5个英文单词，再加上空格分割的英文词
    cn_chinese_chars = sum(1 for c in cs if '\u4e00' <= c <= '\u9fff')
    cn_word_est = cn_chinese_chars * 0.5 + len(re.findall(r'[A-Za-z]+', cs))
    en_words = len(es.split())
    if max(cn_word_est, en_words) > 0:
        word_ratio = min(cn_word_est, en_words) / max(cn_word_est, en_words)
        score += word_ratio * 0.25

    # 3. 共享的数字/专有名词/符号 → 翻译中通常保留
    cn_tokens = set(re.findall(r'[A-Z][a-z]+|[A-Z]{2,}|[a-z_]{4,}|\d+', cs))
    en_tokens = set(re.findall(r'[A-Z][a-z]+|[A-Z]{2,}|[a-z_]{4,}|\d+', es))
    if en_tokens:
        overlap = cn_tokens & en_tokens
        # 使用 overlap 数量的对数缩放，减少短段落高比例的偏差
        token_overlap = len(overlap) / len(en_tokens)
        # 额外奖励多个 token 重叠
        if len(overlap) >= 2:
            score += min(token_overlap * 0.4 + 0.05, 0.4)
        else:
            score += token_overlap * 0.35
        # 惩罚：EN有很多专有名词/标识符但CN中一个都没有
        if len(en_tokens) >= 3 and len(overlap) == 0:
            score *= 0.4
    else:
        score += 0.05

    # 4. 含中文字符 → 更可能是翻译结果
    has_chinese = bool(re.search(r'[\u4e00-\u9fff]', cs))
    if has_chinese:
        score += 0.15

    return min(score, 1.0)


def _optimal_alignment(paras_cn: list, paras_en: list, en_untrans: list) -> list:
    """使用动态规划找到最优的顺序一致的段落对齐。

    返回列表，每个元素对应一个 EN 段落：
      - ('pair', cn_idx)    → CN[cn_idx] 与 EN[ei] 配对
      - ('untrans',)        → EN[ei] 是不可翻译段落
      - ('unpaired',)       → EN[ei] 找不到匹配的 CN 段落
    """
    n_cn = len(paras_cn)
    n_en = len(paras_en)

    # 提取可翻译的 EN 段落索引
    trans_en = [ei for ei in range(n_en) if not en_untrans[ei]]

    if not trans_en or n_cn == 0:
        # 无可翻译段落或无 CN 段落
        result = []
        for ei in range(n_en):
            if en_untrans[ei]:
                result.append(('untrans',))
            else:
                result.append(('unpaired',))
        return result

    # 预计算 CN 段落是否不可翻译（引用、签名等不应配对到左侧）
    cn_untrans = [_is_untranslatable(paras_cn[ci]) for ci in range(n_cn)]

    # 计算可翻译 EN 与可翻译 CN 的相似度矩阵
    scores = {}
    for ei in trans_en:
        for ci in range(n_cn):
            if cn_untrans[ci]:
                continue  # CN 侧引用/签名段落不参与配对
            sc = _translation_similarity(paras_cn[ci], paras_en[ei])
            if sc >= 0.15:
                scores[(ci, ei)] = sc

    # N:M 匹配支持：尝试合并相邻 EN 段落后与 CN 段落匹配
    # 这处理翻译引擎将多个EN段落合并为一个CN段落的情况
    for idx in range(len(trans_en) - 1):
        ei1 = trans_en[idx]
        ei2 = trans_en[idx + 1]
        merged_en = paras_en[ei1] + '\n' + paras_en[ei2]
        for ci in range(n_cn):
            if cn_untrans[ci]:
                continue
            sc = _translation_similarity(paras_cn[ci], merged_en)
            # 如果合并后相似度显著高于单独匹配，记录到 scores
            sc_single = max(scores.get((ci, ei1), 0), scores.get((ci, ei2), 0))
            if sc >= 0.4 and sc > sc_single + 0.1:
                # 将较高分赋给第一个 EN，第二个 EN 留空让 DP 自然跳过
                scores[(ci, ei1)] = max(scores.get((ci, ei1), 0), sc)

    # DP: 找到最优顺序一致匹配
    # 在 skip_en 时给予一个小的负分惩罚，鼓励匹配更多段落
    # 这防止错误匹配"消耗"一个好的CN段落而导致后续大量unpaired
    n_te = len(trans_en)
    SKIP_EN_PENALTY = -0.15  # 跳过可翻译EN段落的惩罚
    CONSECUTIVE_BONUS = 0.08  # 连续匹配奖励，鼓励相邻段落连续配对

    # dp[ti+1][ci+1] 表示前 ti 个可翻译EN和前 ci 个CN段落的最优匹配
    dp = [[0.0] * (n_cn + 1) for _ in range(n_te + 1)]
    choice = [[None] * (n_cn + 1) for _ in range(n_te + 1)]

    for ti in range(n_te):
        ei = trans_en[ti]
        for ci in range(n_cn):
            # 选项1: 不匹配 CN[ci]（跳过CN段落，无惩罚）
            if dp[ti + 1][ci] >= dp[ti + 1][ci + 1]:
                dp[ti + 1][ci + 1] = dp[ti + 1][ci]
                choice[ti + 1][ci + 1] = ('skip_cn', ti, ci)

            # 选项2: 不匹配 trans_en[ti]（跳过EN段落，有惩罚）
            skip_score = dp[ti][ci + 1] + SKIP_EN_PENALTY
            if skip_score > dp[ti + 1][ci + 1]:
                dp[ti + 1][ci + 1] = skip_score
                choice[ti + 1][ci + 1] = ('skip_en', ti, ci)

            # 选项3: 匹配 CN[ci] 与 trans_en[ti]
            sc = scores.get((ci, ei), 0.0)
            if sc > 0:
                # 连续匹配奖励：如果上一步 (ti-1, ci-1) 也是 match，给额外加分
                bonus = 0.0
                if ti > 0 and ci > 0 and choice[ti][ci] is not None and choice[ti][ci][0] == 'match':
                    bonus = CONSECUTIVE_BONUS
                total = dp[ti][ci] + sc + bonus
                if total > dp[ti + 1][ci + 1]:
                    dp[ti + 1][ci + 1] = total
                    choice[ti + 1][ci + 1] = ('match', ti, ci)

        # 处理最后一列：跳过 trans_en[ti]（有惩罚）
        skip_score = dp[ti][n_cn] + SKIP_EN_PENALTY
        if skip_score > dp[ti + 1][n_cn]:
            dp[ti + 1][n_cn] = skip_score
            choice[ti + 1][n_cn] = ('skip_en', ti, n_cn - 1)

    # 最后一行：跳过剩余 CN
    for ci in range(n_cn):
        if dp[n_te][ci] >= dp[n_te][ci + 1]:
            dp[n_te][ci + 1] = dp[n_te][ci]
            choice[n_te][ci + 1] = ('skip_cn', n_te - 1, ci)

    # 回溯找匹配对
    matches = {}  # trans_en index -> cn_idx
    ti, ci = n_te, n_cn
    while ti > 0 or ci > 0:
        if ti == 0:
            break
        if ci == 0:
            ti -= 1
            continue
        ch = choice[ti][ci]
        if ch is None:
            break
        if ch[0] == 'match':
            matches[ch[1]] = ch[2]  # trans_en[ch[1]] matched with CN[ch[2]]
            ti -= 1
            ci -= 1
        elif ch[0] == 'skip_cn':
            ci -= 1
        elif ch[0] == 'skip_en':
            ti -= 1

    # 构建结果
    result = []
    for ei in range(n_en):
        if en_untrans[ei]:
            result.append(('untrans',))
        else:
            ti_idx = trans_en.index(ei)
            if ti_idx in matches:
                result.append(('pair', matches[ti_idx]))
            else:
                result.append(('unpaired',))

    # Fallback: 如果匹配率过低（>50% unpaired），降级为简单顺序配对
    paired_count = sum(1 for r in result if r[0] == 'pair')
    unpaired_count = sum(1 for r in result if r[0] == 'unpaired')
    if unpaired_count > 0 and paired_count < unpaired_count:
        # 降级：按顺序将可翻译 CN 段落依次配对到可翻译 EN 段落
        trans_cn = [ci for ci in range(n_cn) if not cn_untrans[ci]]
        result = []
        cn_iter = iter(trans_cn)
        for ei in range(n_en):
            if en_untrans[ei]:
                result.append(('untrans',))
            else:
                try:
                    ci = next(cn_iter)
                    result.append(('pair', ci))
                except StopIteration:
                    result.append(('unpaired',))

    return result


def _filter_aligned_for_text(aligned_paras: list, text_orig: str) -> list:
    """从完整对齐列表中过滤出属于正文部分（非 diff）的段落。

    当 body 包含 diff 时，translate_body_aligned 对整个 body 做了段落翻译，
    但渲染时 diff 独立展示，正文区域只需要非 diff 段落。
    """
    text_paras = set()
    for p in re.split(r'\n\n+', text_orig.strip()):
        if p.strip():
            text_paras.add(p.strip())

    result = []
    for en, cn in aligned_paras:
        # 段落如果出现在 text_orig 中，保留
        if en.strip() in text_paras:
            result.append((en, cn))
        else:
            # diff 行段落通常以 diff --git / --- a/ / +++ b/ 开头
            first_line = en.strip().split('\n')[0] if en.strip() else ""
            if first_line.startswith(('diff --git', '--- a/', '+++ b/', '@@', 'index ')):
                continue  # diff 段落，跳过
            # 兜底：保留（可能是正文中被拆分方式不同的段落）
            result.append((en, cn))

    return result if result else aligned_paras


def _render_bilingual_from_aligned(aligned_paras: list) -> str:
    """从 translate_body_aligned 的输出直接渲染双栏对比 HTML。

    参数:
        aligned_paras: List[(en_para, cn_para_or_None)]，由 translate_body_aligned 返回

    布局：左侧=英文原文，右侧=中文翻译（如有）。段落已在翻译阶段完成对齐。
    """
    rows = []
    for en, cn in aligned_paras:
        if cn is not None:
            # 有翻译 → 左EN右CN
            rows.append(
                f'<div class="pg-cell"><pre>{_esc(en)}</pre></div>'
                f'<div class="pg-cell pg-cn"><pre>{_esc(cn)}</pre></div>'
            )
        else:
            # 无翻译（代码/引用/签名等）→ 横跨整行
            rows.append(
                f'<div class="pg-left"><pre>{_esc(en)}</pre></div>'
                f'<div class="pg-spacer"></div>'
            )

    if not rows:
        return ""

    return '<div class="para-grid">\n' + '\n'.join(rows) + '\n</div>'


def _render_bilingual_body(text_cn: str, text_orig: str) -> str:
    """将中英文正文按段落对齐，生成左右对比的 HTML 网格。

    布局：左侧=英文原文，右侧=中文翻译。
    此函数为旧接口兼容（text_cn 为翻译后拼接的字符串），使用 DP 对齐。
    新代码应使用 _render_bilingual_from_aligned() 替代。
    """
    paras_cn = _split_paragraphs(text_cn)
    paras_en = _split_paragraphs(text_orig)

    # 预计算每个英文段落是否不可翻译（上下文感知）
    en_untrans = []
    for idx, en in enumerate(paras_en):
        prev_p = paras_en[idx - 1] if idx > 0 else ""
        next_p = paras_en[idx + 1] if idx < len(paras_en) - 1 else ""
        en_untrans.append(_is_untranslatable_in_context(en, prev_p, next_p))

    # 全局最优对齐
    alignment = _optimal_alignment(paras_cn, paras_en, en_untrans)

    rows = []
    for ei, en in enumerate(paras_en):
        action = alignment[ei]
        if action[0] == 'untrans':
            rows.append(
                f'<div class="pg-left"><pre>{_esc(en)}</pre></div>'
                f'<div class="pg-spacer"></div>'
            )
        elif action[0] == 'pair':
            ci = action[1]
            rows.append(
                f'<div class="pg-cell"><pre>{_esc(en)}</pre></div>'
                f'<div class="pg-cell pg-cn"><pre>{_esc(paras_cn[ci])}</pre></div>'
            )
        else:
            rows.append(
                f'<div class="pg-left"><pre>{_esc(en)}</pre></div>'
                f'<div class="pg-spacer"></div>'
            )

    if not rows:
        rows.append(
            f'<div class="pg-left"><pre>{_esc(text_orig)}</pre></div>'
            f'<div class="pg-spacer"></div>'
        )

    grid = '<div class="para-grid">\n' + '\n'.join(rows) + '\n</div>'
    return grid


def _render_bilingual_commit(cm_cn: str, cm_orig: str) -> str:
    """渲染 commit message 的段落对齐对比: 左EN右CN"""
    paras_cn = _split_paragraphs(cm_cn)
    paras_en = _split_paragraphs(cm_orig)

    en_untrans = []
    for idx, en in enumerate(paras_en):
        prev_p = paras_en[idx - 1] if idx > 0 else ""
        next_p = paras_en[idx + 1] if idx < len(paras_en) - 1 else ""
        en_untrans.append(_is_untranslatable_in_context(en, prev_p, next_p))

    alignment = _optimal_alignment(paras_cn, paras_en, en_untrans)

    rows = []
    for ei, en in enumerate(paras_en):
        action = alignment[ei]
        if action[0] == 'untrans':
            rows.append(
                f'<div class="pg-left"><pre>{_esc(en)}</pre></div>'
                f'<div class="pg-spacer"></div>'
            )
        elif action[0] == 'pair':
            ci = action[1]
            rows.append(
                f'<div class="pg-cell"><pre>{_esc(en)}</pre></div>'
                f'<div class="pg-cell pg-cn"><pre>{_esc(paras_cn[ci])}</pre></div>'
            )
        else:
            rows.append(
                f'<div class="pg-left"><pre>{_esc(en)}</pre></div>'
                f'<div class="pg-spacer"></div>'
            )
    return '<div class="para-grid">\n' + '\n'.join(rows) + '\n</div>'


def _html_email_node(
    node: ThreadNode, idx_map: dict, translated_bodies: dict,
    is_root: bool = False, _visited: set = None,
) -> str:
    """递归渲染一个 ThreadNode 为 HTML（带循环检测）"""
    if _visited is None:
        _visited = set()
    nid = id(node)
    if nid in _visited:
        return ""
    _visited.add(nid)

    em = node.email
    i = idx_map.get(id(em))
    tag = em.get("tag", "")
    color, label = TAG_COLORS.get(tag, ("#999", ""))

    author = em.get("from", "").split("<")[0].strip() or em.get("from", "")
    date = em.get("date", "")
    initial = (author[0].upper() if author else "?")

    body_data = translated_bodies.get(f"email_{i}") if i is not None else None
    body_orig = em.get("body", "")

    # 分离 diff 代码块
    text_orig, diff_orig = _split_body_and_diff(body_orig)
    diff_code = translated_bodies.get(f"diff_{i}", diff_orig) if i is not None else diff_orig

    # 判断翻译数据格式：list = 新对齐列表，str = 旧翻译文本
    aligned_paras = None
    has_translation = False
    text_cn = ""
    if isinstance(body_data, list):
        # 新格式：translate_body_aligned 返回的 [(en, cn_or_None)]
        # 过滤掉 diff 部分的段落（只保留正文段落）
        text_orig_stripped = text_orig.strip()
        if text_orig_stripped != body_orig.strip():
            # body 含 diff，需要重新对齐正文部分
            aligned_paras = _filter_aligned_for_text(body_data, text_orig)
        else:
            aligned_paras = body_data
        has_translation = any(cn is not None for _, cn in aligned_paras)
    elif isinstance(body_data, str) and body_data:
        has_translation = body_data != body_orig
        if has_translation:
            text_cn, _ = _split_body_and_diff(body_data)

    # 节点唯一 ID
    node_id = f'email-node-{i}' if i is not None else f'email-node-x{nid}'

    # 邮件卡片内容
    card = []
    card.append(f'<div class="email-card">')
    card.append(f'  <div class="email-header">')
    card.append(f'    <span class="avatar" style="background:{color}">{_esc(initial)}</span>')
    card.append(f'    <span class="author">{_esc(author)}</span>')
    card.append(f'    <span class="tag" style="background:{color}">{_esc(label)}</span>')
    card.append(f'    <span class="date">{_esc(date)}</span>')
    card.append(f'    <button class="focus-btn" onclick="focusEmail(\'{node_id}\')" title="聚焦此邮件（全宽显示）">&#128269;</button>')
    card.append(f'  </div>')

    if aligned_paras is not None and has_translation:
        # 新格式：直接使用对齐列表渲染
        card.append(f'  {_render_bilingual_from_aligned(aligned_paras)}')
    elif has_translation and text_cn:
        # 旧格式兼容：DP 对齐
        card.append(f'  {_render_bilingual_body(text_cn, text_orig)}')
    else:
        # 无翻译时：内容放在左侧栏
        card.append(f'  <div class="para-grid">')
        card.append(f'<div class="pg-left"><pre>{_esc(text_orig)}</pre></div><div class="pg-spacer"></div>')
        card.append(f'  </div>')

    # diff 代码块：独立可折叠
    if diff_code:
        card.append(f'  <details class="diff-block"><summary>代码变更 (diff)</summary>')
        card.append(f'    <pre class="diff">{_esc_diff(diff_code)}</pre>')
        card.append(f'  </details>')

    card.append(f'</div>')

    # 子节点
    children_html = ""
    if node.children:
        parts = []
        for child in node.children:
            parts.append(_html_email_node(child, idx_map, translated_bodies, _visited=_visited))
        children_html = '<div class="replies">' + "\n".join(parts) + "</div>"

    # 根邮件直接展开，回复用 details 折叠
    subj_esc = _esc(em.get("subject", "")[:80])
    author_esc = _esc(author)
    tag_esc = _esc(label)
    body_preview = _esc(text_orig[:200].replace('\n', ' ')) if text_orig else ""
    data_attrs = (
        f'data-author="{author_esc}" data-subject="{subj_esc}" '
        f'data-tag="{tag_esc}" data-body-preview="{body_preview}"'
    )
    if is_root:
        return (
            f'<div class="email-node" id="{node_id}" {data_attrs}>'
            + "\n".join(card) + "\n" + children_html
            + '</div>'
        )
    else:
        n = node.total_count()
        count_badge = f' <span class="count-badge">{n}</span>' if n > 1 else ""
        summary = f'{author_esc} — {subj_esc}{count_badge}'
        inner = "\n".join(card) + "\n" + children_html
        return (
            f'<div class="email-node" id="{node_id}" {data_attrs}>'
            f'<details class="reply-thread">'
            f'<summary>{summary}</summary>'
            f'{inner}'
            f'</details>'
            f'</div>'
        )


def generate_html(
    commit: dict, diff: str, email_header: str,
    emails: List[dict], checklist: str, translated_bodies: dict,
    source_hash: str = "",
) -> str:
    """生成自包含 HTML 文档，邮件按线程树状组织"""
    subject = _esc(commit.get("subject", "Unknown Commit"))

    # 邮件统计
    stats_html = ""
    stat_m = re.search(r"原始 (\d+) 封.*过滤 (\d+) 封.*保留 (\d+) 封", email_header)
    prio_m = re.search(r"HIGH:(\d+)\s+MEDIUM:(\d+)\s+LOW:(\d+)", email_header)
    if stat_m:
        stats_html += f'<span>原始 {stat_m.group(1)} 封 → 过滤 {stat_m.group(2)} 封机器通知 → 保留 {stat_m.group(3)} 封</span>'
    if prio_m:
        stats_html += (
            f' &nbsp;|&nbsp; '
            f'<span class="tag" style="background:#dc3545">HIGH {prio_m.group(1)}</span> '
            f'<span class="tag" style="background:#fd7e14">MED {prio_m.group(2)}</span> '
            f'<span class="tag" style="background:#6c757d">LOW {prio_m.group(3)}</span>'
        )

    # Commit 信息表
    commit_rows = ""
    for key, label in [
        ("hash", "Hash"), ("author", "Author"), ("date", "Date"),
        ("files", "Files"), ("subsys", "Subsystem"), ("patchset", "Patchset"),
    ]:
        val = commit.get(key, "")
        if val:
            commit_rows += f"<tr><th>{label}</th><td>{_esc(val)}</td></tr>\n"
    lore = commit.get("lore", "")
    if lore:
        commit_rows += f'<tr><th>Lore</th><td><a href="{_esc(lore)}" target="_blank">{_esc(lore)}</a></td></tr>\n'

    # Commit message
    cm = commit.get("commit_message", "")
    cm_cn = translated_bodies.get("__commit_message__", "")
    cm_html = ""
    if cm:
        if cm_cn:
            cm_html = _render_bilingual_commit(cm_cn, cm)
        else:
            cm_html = f'<pre class="commit-msg">{_esc(cm)}</pre>'

    # Diff（使用预翻译注释版本）
    diff_html = ""
    if diff:
        diff_display = translated_bodies.get("__diff__", diff)
        diff_html = f'<pre class="diff">{_esc_diff(diff_display)}</pre>'

    # 邮件线程树
    threads_html = ""
    if emails:
        idx_map = {id(em): i for i, em in enumerate(emails)}
        threads = _build_thread_tree(emails)

        for thread in threads:
            bs = _esc(thread["base_subject"])
            count = thread["count"]
            roots = thread["roots"]

            thread_parts = [f'<div class="thread">']
            thread_parts.append(f'<h3 class="thread-title">{bs} <span class="count-badge">{count}</span></h3>')
            for root_node in roots:
                thread_parts.append(_html_email_node(root_node, idx_map, translated_bodies, is_root=True))
            thread_parts.append("</div>")
            threads_html += "\n".join(thread_parts) + "\n"

    return _HTML_TEMPLATE.format(
        title=subject,
        source_hash=source_hash,
        commit_rows=commit_rows,
        cm_html=cm_html,
        diff_html=diff_html,
        stats_html=stats_html,
        threads_html=threads_html,
    )


# ─── 主程序 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="将 context_full.txt 翻译并整理为可读文档（HTML/Markdown）"
    )
    parser.add_argument("input", help="输入的 context_full.txt 文件路径")
    parser.add_argument("-o", "--output", help="输出文件路径（默认自动生成）")
    parser.add_argument(
        "--format", default="html", choices=["html", "md"],
        help="输出格式：html（默认，推荐）或 md",
    )
    parser.add_argument(
        "--backend", default="google", choices=["google", "youdao", "api"],
        help="翻译后端（默认 google）",
    )
    parser.add_argument("--provider", default="deepseek", help="API 服务商（api 后端专用）")
    parser.add_argument("--api-key", default="", help="API 密钥（api 后端专用）")
    parser.add_argument("--model", default="", help="API 模型名（api 后端专用）")
    parser.add_argument("--skip-low", action="store_true", help="跳过 LOW 优先级邮件的翻译")
    parser.add_argument("--dry-run", action="store_true", help="只解析不翻译")
    parser.add_argument("--proxy", default="", help="代理地址（如 127.0.0.1:7897），仅翻译请求使用")
    parser.add_argument("--workers", type=int, default=1, help="并行翻译线程数（默认 1，建议 4-8）")
    parser.add_argument("--no-cache", action="store_true", help="禁用翻译缓存（默认启用缓存）")
    parser.add_argument("--force", action="store_true", help="强制重新翻译，忽略已有输出文件")
    parser.add_argument("--no-dashboard", action="store_true", help="跳过 Dashboard 重建")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"错误: 文件不存在: {input_path}")
        sys.exit(1)

    text = input_path.read_text(encoding="utf-8")
    print(f"读取: {input_path}  ({len(text) // 1024} KB)")

    # 计算输入文件 hash，用于增量翻译检测
    source_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]

    # 推算输出路径（提前计算，用于增量检测）
    fmt = args.format
    ext = ".html" if fmt == "html" else ".md"
    if args.output:
        out_path = Path(args.output)
    else:
        stem = input_path.stem.replace("_context_full", "").replace("_full", "")
        out_path = input_path.parent / f"{stem}_translated{ext}"

    # 增量翻译检测：如果输出文件已存在且 source hash 匹配，跳过
    if not args.force and not args.dry_run and out_path.exists():
        try:
            existing = out_path.read_text(encoding="utf-8")
            m = re.search(r'<meta\s+name="source-hash"\s+content="([^"]+)"', existing)
            if m and m.group(1) == source_hash:
                print(f"\n  输出文件已是最新（source hash 匹配: {source_hash}），跳过翻译")
                print(f"  如需强制重新翻译，请使用 --force 参数")
                print(f"  已有输出: {out_path}")
                return
            else:
                print(f"  检测到已有输出，但输入已变更，将重新翻译")
        except Exception:
            pass

    # 解析
    print("\n[1/3] 解析文档结构...")
    commit = parse_commit_section(text)
    diff = parse_diff_section(text)
    email_header, emails = parse_emails(text)
    checklist = parse_analysis_checklist(text)
    print(f"  Commit: {commit.get('subject', '?')}")
    print(f"  Diff: {len(diff)} 字符")
    print(f"  邮件: {len(emails)} 封")
    print(f"  分析清单: {'有' if checklist else '无'}")

    if args.dry_run:
        print("\n[dry-run] 解析完成，跳过翻译")
        threads = _build_thread_tree(emails)
        print(f"  线程: {len(threads)} 个")
        for t in threads:
            bs = t["base_subject"][:60]
            print(f"    [{t['count']}封] {bs}")
            for root in t["roots"]:
                _print_tree(root, indent=3)
        return

    # 翻译
    print(f"\n[2/3] 翻译邮件内容（后端: {args.backend}）...")
    proxy = args.proxy or None
    if proxy:
        print(f"  使用代理: {proxy}")
    if args.backend == "api":
        if not args.api_key:
            print("错误: --backend api 需要 --api-key 参数")
            sys.exit(1)
        translator = create_translator(
            "api", api_key=args.api_key, provider=args.provider,
            model=args.model or None, proxy=proxy,
        )
    else:
        translator = create_translator(args.backend, proxy=proxy)

    # 包装翻译缓存
    if not args.no_cache:
        cache = TranslationCache()
        translator = CachedTranslator(translator, cache, args.backend)
        cache_size = cache.size()
        if cache_size > 0:
            print(f"  翻译缓存: {cache_size} 条已缓存")
    else:
        cache = None

    translated = {}
    cm = commit.get("commit_message", "")
    if cm and should_translate(cm):
        print("  翻译 commit message...")
        result = translator.translate_email({"subject": "", "body": cm})
        translated["__commit_message__"] = result.get("body_cn", "")

    total = len(emails)
    workers = max(1, args.workers)

    # 收集需要翻译的任务列表
    tasks = []  # (index, email)
    skipped = 0
    for i, em in enumerate(emails):
        tag = em.get("tag", "")
        body = em.get("body", "")
        if args.skip_low and tag == "[PATCH摘要]":
            skipped += 1
            continue
        if not should_translate(body):
            skipped += 1
            continue
        tasks.append((i, em))

    done = 0
    if workers <= 1:
        # 串行模式
        for i, em in tasks:
            tag = em.get("tag", "")
            body = em.get("body", "")
            done += 1
            print(f"  [{i+1}/{total}] {tag} {em.get('subject', '')[:50]}")
            translated[f"email_{i}"] = translate_body_aligned(translator, body)
            if done % 5 == 0:
                time.sleep(1)
    else:
        # 多线程并行模式
        from concurrent.futures import ThreadPoolExecutor, as_completed
        import threading
        print(f"  并行翻译: {workers} 线程")
        lock = threading.Lock()

        def _translate_one(idx_em):
            i, em = idx_em
            body = em.get("body", "")
            result = translate_body_aligned(translator, body)
            return i, em, result

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_translate_one, t): t for t in tasks}
            for future in as_completed(futures):
                i, em, result = future.result()
                tag = em.get("tag", "")
                translated[f"email_{i}"] = result
                done += 1
                with lock:
                    print(f"  [{done}/{len(tasks)}] {tag} {em.get('subject', '')[:50]}")

    print(f"  翻译完成: {done} 封, 跳过: {skipped} 封")
    if cache is not None:
        stats = cache.stats()
        if stats.get("total", 0) > 0:
            print(f"  缓存统计: 命中 {stats['hits']}, 未命中 {stats['misses']}, 命中率 {stats['hit_rate']}")

    # 翻译 diff 中的注释
    print("  翻译 diff 注释...")
    # 全局 diff
    if diff:
        translated["__diff__"] = _translate_diff_comments(translator, diff)

    # 每封邮件中的 diff（也可并行）
    diff_tasks = []
    for i, em in enumerate(emails):
        _, em_diff = _split_body_and_diff(em.get("body", ""))
        if em_diff:
            diff_tasks.append((i, em_diff))

    if workers <= 1 or len(diff_tasks) < 2:
        for i, em_diff in diff_tasks:
            translated[f"diff_{i}"] = _translate_diff_comments(translator, em_diff)
    else:
        def _translate_diff_one(idx_diff):
            i, d = idx_diff
            return i, _translate_diff_comments(translator, d)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_translate_diff_one, t) for t in diff_tasks]
            for future in as_completed(futures):
                i, result = future.result()
                translated[f"diff_{i}"] = result

    diff_done = len(diff_tasks)
    print(f"  diff 注释翻译完成: {diff_done + (1 if diff else 0)} 个")

    # 生成
    print(f"\n[3/3] 生成 {fmt.upper()} 文档...")

    if fmt == "html":
        output = generate_html(commit, diff, email_header, emails, checklist, translated, source_hash=source_hash)
    else:
        output = generate_html(commit, diff, email_header, emails, checklist, translated, source_hash=source_hash)
        # 未来可恢复旧 generate_markdown，目前统一用 HTML

    out_path.write_text(output, encoding="utf-8")
    print(f"\n  已保存: {out_path}")
    print(f"  大小: {len(output) // 1024} KB  |  {len(output.splitlines())} 行")

    # 重建 Dashboard
    if not args.no_dashboard:
        try:
            from build_dashboard import generate_dashboard
            generate_dashboard()
        except Exception as e:
            print(f"  [Dashboard] 重建失败（可忽略）: {e}")


def _print_tree(node: ThreadNode, indent: int = 0):
    """调试用：打印线程树"""
    prefix = "  " * indent
    author = node.author[:20]
    depth_str = f"Re:x{node.depth}" if node.depth else "ROOT"
    print(f"{prefix}├─ [{depth_str}] {author} — {node.subject[:50]}")
    for child in node.children:
        _print_tree(child, indent + 1)


if __name__ == "__main__":
    main()
