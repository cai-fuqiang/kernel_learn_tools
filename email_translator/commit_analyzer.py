"""Commit Analyzer - 从 git commit 中提取搜索线索

以 git commit 为入口，自动提炼：
  - 邮件列表搜索关键词
  - 涉及的子系统 / 文件
  - 作者、时间范围
  - 供 AI 理解的上下文摘要
"""
import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class CommitInfo:
    """一个 git commit 的结构化信息"""
    hash: str
    subject: str
    author: str
    author_email: str
    date: datetime
    body: str                        # commit message 正文（去掉 subject 行）
    files: List[str] = field(default_factory=list)   # 修改的文件
    diff: str = ""                   # 完整 diff 文本

    # 自动提炼的搜索线索
    search_terms: List[str] = field(default_factory=list)
    subsystems: List[str] = field(default_factory=list)
    date_from: str = ""              # 建议的搜索起始日期
    date_to: str = ""                # 建议的搜索截止日期
    lore_url: str = ""               # commit message 中的 lore.kernel.org 直链

    # patchset 信息（从 subject 解析）
    patch_index: int = 0             # 当前 patch 在系列中的编号（0 = 未知）
    patch_total: int = 0             # patchset 总数（0 = 未知）
    patch_version: str = ""          # 版本号，如 "v2"
    is_cover_letter: bool = False    # 是否是 cover letter（[PATCH 0/N]）

    def summary(self) -> str:
        """返回供 AI / 人读的简要摘要"""
        lines = [
            f"Commit : {self.hash[:12]}",
            f"Subject: {self.subject}",
            f"Author : {self.author} <{self.author_email}>",
            f"Date   : {self.date.strftime('%Y-%m-%d')}",
            f"Files  : {', '.join(self.files[:5])}" + (
                f" ... 共{len(self.files)}个" if len(self.files) > 5 else ""),
            f"Search : {' | '.join(self.search_terms)}",
        ]
        if self.patch_total > 0:
            ver = f" {self.patch_version}" if self.patch_version else ""
            lines.append(f"Patchset: [PATCH{ver} {self.patch_index}/{self.patch_total}]")
        if self.lore_url:
            lines.append(f"Lore   : {self.lore_url}  ← 直达邮件线程")
        if self.body.strip():
            lines.append(f"\n--- Commit Message ---\n{self.body.strip()[:600]}")
        return "\n".join(lines)


class CommitAnalyzer:
    """解析 git commit，提炼邮件列表搜索线索"""

    # Linux 内核子系统前缀映射
    _SUBSYSTEM_MAP = {
        "kernel/sched": "sched",
        "mm/": "mm",
        "fs/": "fs",
        "net/": "netdev",
        "drivers/": "drivers",
        "arch/": "arch",
        "security/": "security",
        "include/linux": "kernel",
        "block/": "block",
        "crypto/": "crypto",
    }

    def __init__(self, repo_path: str = "."):
        self.repo_path = repo_path

    # ------------------------------------------------------------------
    # 公开接口
    # ------------------------------------------------------------------

    def analyze(self, commit_ref: str) -> CommitInfo:
        """
        解析一个 commit（hash / tag / HEAD~N 等均可）。

        Args:
            commit_ref: git 可识别的 commit 引用

        Returns:
            CommitInfo 结构
        """
        raw = self._git_show(commit_ref)
        info = self._parse_commit(raw)
        info.search_terms = self._extract_search_terms(info)
        info.subsystems = self._extract_subsystems(info.files)
        info.date_from, info.date_to = self._suggest_date_range(info.date)
        info.lore_url = self._extract_lore_url(info.body)
        info.patch_index, info.patch_total, info.patch_version, info.is_cover_letter = \
            self._parse_patch_tag(info.subject)
        return info

    def analyze_range(self, since: str, until: str = "HEAD",
                      max_commits: int = 20) -> List[CommitInfo]:
        """
        批量解析一段范围内的 commit。

        Args:
            since:  起始引用（如 HEAD~10、某个 hash）
            until:  结束引用（默认 HEAD）
            max_commits: 最多处理数量
        """
        hashes = self._git_log(since, until, max_commits)
        results = []
        for h in hashes:
            try:
                results.append(self.analyze(h))
            except Exception as e:
                logger.warning("解析 commit %s 失败: %s", h, e)
        return results

    # ------------------------------------------------------------------
    # 内部：git 命令
    # ------------------------------------------------------------------

    def _git_show(self, ref: str) -> str:
        """运行 git show，获取完整输出"""
        result = subprocess.run(
            ["git", "show", "--stat", ref],
            cwd=self.repo_path,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"git show {ref!r} 失败: {result.stderr.strip()}"
            )
        return result.stdout

    def _git_diff(self, ref: str) -> str:
        """获取 commit 的 diff 内容（不含 stat）"""
        result = subprocess.run(
            ["git", "show", "--no-stat", "-p", ref],
            cwd=self.repo_path,
            capture_output=True, text=True, timeout=30
        )
        return result.stdout if result.returncode == 0 else ""

    def _git_log(self, since: str, until: str, max_n: int) -> List[str]:
        """获取一段范围内的 commit hash 列表"""
        result = subprocess.run(
            ["git", "log", "--format=%H", f"{since}..{until}", f"-{max_n}"],
            cwd=self.repo_path,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            raise RuntimeError(f"git log 失败: {result.stderr.strip()}")
        return [h.strip() for h in result.stdout.splitlines() if h.strip()]

    # ------------------------------------------------------------------
    # 内部：解析 git show 输出
    # ------------------------------------------------------------------

    def _parse_commit(self, raw: str) -> CommitInfo:
        """解析 git show --stat 的输出"""
        lines = raw.splitlines()

        commit_hash = ""
        author = ""
        author_email = ""
        date = datetime.now()
        subject = ""
        body_lines = []
        files = []

        i = 0
        # 解析头部字段
        while i < len(lines):
            line = lines[i]
            if line.startswith("commit "):
                commit_hash = line.split()[1]
            elif line.startswith("Author:"):
                m = re.match(r"Author:\s+(.+?)\s+<(.+?)>", line)
                if m:
                    author, author_email = m.group(1), m.group(2)
                else:
                    author = line[7:].strip()
            elif line.startswith("Date:"):
                date_str = line[5:].strip()
                try:
                    from email.utils import parsedate_to_datetime
                    date = parsedate_to_datetime(date_str)
                    # 转为 naive datetime 避免时区问题
                    date = date.replace(tzinfo=None)
                except Exception:
                    try:
                        date = datetime.strptime(date_str[:25], "%a %b %d %H:%M:%S %Y")
                    except Exception:
                        date = datetime.now()
            elif line == "" and not subject:
                # 空行之后开始 message
                i += 1
                if i < len(lines):
                    subject = lines[i].strip()
                i += 1
                # 收集 body（直到 stat 分隔线或文件列表）
                while i < len(lines):
                    l = lines[i]
                    # stat 输出开始（含 | 的行，或 "N files changed"）
                    if re.match(r'\s+\S+.*\|', l) or re.match(r'\s*\d+ files? changed', l):
                        break
                    body_lines.append(l)
                    i += 1
                continue
            # 解析 stat 文件列表
            m = re.match(r'\s+([\w/._-]+(?:\.[chS\w]+)?)\s+\|', line)
            if m:
                files.append(m.group(1))
            i += 1

        body = "\n".join(body_lines).strip()

        return CommitInfo(
            hash=commit_hash,
            subject=subject,
            author=author,
            author_email=author_email,
            date=date,
            body=body,
            files=files,
        )

    # ------------------------------------------------------------------
    # 内部：提炼搜索线索
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_lore_url(body: str) -> str:
        """从 commit message 中提取 lore.kernel.org 直链"""
        m = re.search(r'https://lore\.kernel\.org/\S+', body)
        return m.group(0).rstrip(')>.,') if m else ""

    @staticmethod
    def _parse_patch_tag(subject: str) -> tuple:
        """
        解析 subject 中的 [PATCH v2 N/M] 标签。
        返回 (patch_index, patch_total, version, is_cover_letter)
        """
        m = re.search(
            r'\[PATCH(?:\s+(v\d+))?(?:\s+\d+/\d+)*\s*(\d+)/(\d+)\]',
            subject, re.IGNORECASE
        )
        if m:
            version = m.group(1) or ""
            idx     = int(m.group(2))
            total   = int(m.group(3))
            is_cover = (idx == 0)
            return idx, total, version, is_cover
        return 0, 0, "", False

    def find_related_commits(self, commit_info: CommitInfo,
                              extra_days: int = 7) -> List[str]:
        """
        在本地仓库中查找同系列相关 commit：
        - 同作者
        - 时间窗口：commit 前后 extra_days 天
        - 涉及相同文件

        Returns:
            相关 commit hash 列表（不含自身）
        """
        if not self.repo_path or not commit_info.files:
            return []

        from datetime import timedelta
        date_after  = (commit_info.date - timedelta(days=extra_days)).strftime("%Y-%m-%d")
        date_before = (commit_info.date + timedelta(days=extra_days)).strftime("%Y-%m-%d")

        # git log 过滤：同作者 + 时间窗口
        cmd = [
            "git", "log", "--format=%H",
            f"--author={commit_info.author}",
            f"--after={date_after}",
            f"--before={date_before}",
            "--",
        ] + commit_info.files

        result = subprocess.run(
            cmd, cwd=self.repo_path,
            capture_output=True, text=True, timeout=30
        )
        if result.returncode != 0:
            return []

        hashes = [h.strip() for h in result.stdout.splitlines() if h.strip()]
        # 去掉自身
        return [h for h in hashes if not h.startswith(commit_info.hash[:8])]

    def _extract_search_terms(self, info: CommitInfo) -> List[str]:
        """
        从 commit 信息中提炼 3~5 个搜索关键词，优先级：
        1. subject 中的核心词（去掉 [PATCH ...] 前缀）
        2. subject 中的子系统标签（如 sched/fair:）
        3. body 中出现的函数名
        4. 修改的主要文件（取基名）
        """
        terms = []

        # 1. 清理 subject，提取核心词
        clean_subject = re.sub(r'^\[PATCH[^\]]*\]\s*', '', info.subject).strip()
        if clean_subject:
            terms.append(clean_subject)

        # 2. 子系统标签（形如 "sched/fair:" 或 "mm/vmalloc:"）
        m = re.match(r'^([\w/]+):\s', clean_subject)
        if m:
            subsys = m.group(1)
            if subsys not in terms:
                terms.append(subsys)

        # 3. body + subject 中的函数名（snake_case，至少含一个下划线）
        text = info.subject + "\n" + info.body
        funcs = re.findall(r'\b([a-z][a-z0-9]*(?:_[a-z0-9]+){2,})\b', text)
        # 取出现频率最高的 2 个
        from collections import Counter
        top_funcs = [f for f, _ in Counter(funcs).most_common(2)]
        for f in top_funcs:
            if f not in terms:
                terms.append(f)

        # 4. 修改文件的基名（去掉扩展名）
        for fpath in info.files[:2]:
            basename = fpath.split("/")[-1]
            name = re.sub(r'\.\w+$', '', basename)
            if name and name not in terms and len(name) > 3:
                terms.append(name)

        return terms[:5]

    def _extract_subsystems(self, files: List[str]) -> List[str]:
        """根据修改的文件路径推断涉及的子系统"""
        subsystems = set()
        for f in files:
            for prefix, name in self._SUBSYSTEM_MAP.items():
                if f.startswith(prefix):
                    subsystems.add(name)
                    break
        return sorted(subsystems)

    @staticmethod
    def _suggest_date_range(commit_date: datetime,
                            before_days: int = 60,
                            after_days: int = 14) -> Tuple[str, str]:
        """
        建议搜索日期范围：
        - 从 commit 前 60 天开始（讨论通常早于合并）
        - 到 commit 后 14 天结束（可能有后续讨论）
        """
        date_from = (commit_date - timedelta(days=before_days)).strftime("%Y-%m-%d")
        date_to   = (commit_date + timedelta(days=after_days)).strftime("%Y-%m-%d")
        return date_from, date_to