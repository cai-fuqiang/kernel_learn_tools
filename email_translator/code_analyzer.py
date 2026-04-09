"""Code Analyzer - 从邮件讨论中提取代码引用，关联内核源码"""
import logging
import os
import re
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# 预编译正则
_RE_DIFF = re.compile(r'diff --git a/([\w/._-]+) b/([\w/._-]+)')
_RE_PATCH = re.compile(r'^(?:---|\+\+\+) [ab]/([\w/._-]+\.\w+)', re.MULTILINE)
_RE_HUNK = re.compile(r'^@@[^@]+@@\s+(.+)$', re.MULTILINE)
_RE_KPATH = re.compile(
    r'(?<!\w)((?:kernel|drivers|include|mm|fs|net|arch|block|crypto|'
    r'lib|security|sound|tools|scripts|init|ipc)/[\w/._-]+\.[chS])(?!\w)'
)
_VALID_EXT = re.compile(r'\.[chS]$')


class CodeAnalyzer:
    """从邮件中提取 patch/diff 中的文件路径和函数名，可选关联本地内核源码"""

    def __init__(self, kernel_src: Optional[str] = None):
        self.kernel_src = kernel_src

    def extract_code_references(self, threads: List[Dict]) -> Dict:
        files: Set[str] = set()
        functions: Set[str] = set()
        patches: List[Dict] = []

        for thread in threads:
            for email in [thread['root']] + thread['replies']:
                body = email.get('body', '')
                subject = email.get('subject', '')

                for m in _RE_DIFF.finditer(body):
                    files.add(m.group(2))

                for m in _RE_PATCH.finditer(body):
                    files.add(m.group(1))

                for m in _RE_KPATH.finditer(body):
                    files.add(m.group(1))

                for m in _RE_HUNK.finditer(body):
                    func = m.group(1).strip()
                    if func:
                        functions.add(func)

                if _RE_DIFF.search(body) or '[PATCH' in subject:
                    patches.append({
                        'subject': subject,
                        'from': email.get('from', ''),
                        'date': email.get('date', ''),
                        'files': list(_RE_DIFF.findall(body)),
                    })

        # 过滤掉不合法路径
        clean_files = {f for f in files if _VALID_EXT.search(f)}

        return {
            'files': sorted(clean_files),
            'functions': sorted(functions),
            'patches': patches,
        }

    def generate_code_report(self, threads: List[Dict]) -> str:
        refs = self.extract_code_references(threads)
        report = "## 代码关联分析\n\n"

        if refs['files']:
            report += "### 涉及的源码文件\n\n"
            for f in refs['files']:
                if self.kernel_src:
                    full_path = os.path.join(self.kernel_src, f)
                    tag = "  (本地存在)" if os.path.exists(full_path) else "  (本地不存在)"
                else:
                    tag = ""
                report += f"- `{f}`{tag}\n"
            report += "\n"

        if refs['functions']:
            report += "### 涉及的函数\n\n"
            show = refs['functions'][:30]
            for func in show:
                report += f"- `{func}`\n"
            if len(refs['functions']) > 30:
                report += f"- ... 共 {len(refs['functions'])} 个函数\n"
            report += "\n"

        if refs['patches']:
            report += f"### Patch 邮件 ({len(refs['patches'])} 个)\n\n"
            report += "| 主题 | 作者 | 日期 |\n|------|------|------|\n"
            for p in refs['patches']:
                subj = p['subject'][:60]
                if len(p['subject']) > 60:
                    subj += '...'
                report += f"| {subj} | {p['from']} | {p['date']} |\n"
            report += "\n"

        if not refs['files'] and not refs['functions'] and not refs['patches']:
            report += "*未检测到代码引用*\n\n"

        return report