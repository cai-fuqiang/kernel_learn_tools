"""Output Generator - 生成线程化的中英文对照 Markdown 报告"""
import logging, os
from datetime import datetime
from typing import Dict, List
from .translator import create_translator

logger = logging.getLogger(__name__)

class OutputGenerator:
    """生成线程化的中英文对照 Markdown 报告"""
    def __init__(self, output_dir: str, translator_backend: str = "google", **translator_kwargs):
        self.output_dir = output_dir
        self.translator = create_translator(translator_backend, **translator_kwargs)
        os.makedirs(output_dir, exist_ok=True)

    def generate_report(self, threads: List[Dict], topic: str, with_summary: bool = False, code_report: str = ""):
        """
        生成完整的线程化报告
        :param threads: 线程列表（来自 thread_builder）
        :param topic: 搜索主题（用于文件名）
        :param with_summary: 是否包含 OpenClaw 摘要
        :param code_report: 代码关联分析报告文本
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 修复路径分隔符问题，避免主题中的斜杠造成路径错误
        safe_topic = topic.replace('/', '_').replace(' ', '_')
        filename = f"{safe_topic}_{timestamp}.md"
        filepath = os.path.join(self.output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(self._generate_header(topic, len(threads)))
            f.write(self._generate_overview(threads))
            
            # 代码关联分析
            if code_report:
                f.write(code_report)
                f.write("\n---\n\n")
            
            for i, thread in enumerate(threads, 1):
                f.write(self._generate_thread_section(thread, i, with_summary))
                f.write("\n---\n\n")

        logger.info("报告已生成: %s", filepath)
        return filepath

    def _generate_overview(self, threads: List[Dict]) -> str:
        """生成邮件列表总览统计"""
        total_emails = sum(1 + len(t['replies']) for t in threads)
        all_participants = set()
        for t in threads:
            all_participants.update(t['participants'])
        
        # 统计最活跃的参与者
        from collections import Counter
        participant_counter = Counter()
        for t in threads:
            for p in t['participants']:
                participant_counter[p] += 1
        top_participants = participant_counter.most_common(10)
        
        overview = "## 总览统计\n\n"
        overview += f"| 指标 | 数值 |\n|------|------|\n"
        overview += f"| 线程数 | {len(threads)} |\n"
        overview += f"| 总邮件数 | {total_emails} |\n"
        overview += f"| 参与者数 | {len(all_participants)} |\n\n"
        
        if top_participants:
            overview += "### 主要参与者\n\n"
            overview += "| 参与者 | 参与线程数 |\n|--------|------------|\n"
            for name, count in top_participants:
                overview += f"| {name} | {count} |\n"
            overview += "\n"
        
        overview += "---\n\n"
        return overview

    def _generate_header(self, topic: str, thread_count: int) -> str:
        """生成报告头部"""
        return f"""# Linux 内核邮件讨论报告
## 主题: {topic}
## 线程数: {thread_count}
## 生成时间: {datetime.now().strftime("%Y年%m月%d日 %H:%M")}

---

"""

    def _generate_thread_section(self, thread: Dict, index: int, with_summary: bool) -> str:
        """生成单个线程的章节"""
        root = thread['root']
        replies = thread['replies']
        participants = thread['participants']
        date_from, date_to = thread['date_range']

        section = f"## 线程 {index}: {root['subject']}\n\n"
        section += f"**参与者**: {', '.join(participants)}\n\n"
        section += f"**时间范围**: {date_from} 至 {date_to}\n\n"

        # OpenClaw 摘要
        if with_summary and 'summary' in thread:
            summary = thread['summary']
            section += "### 📋 智能摘要\n\n"
            section += f"**主题**: {summary.get('topic', 'N/A')}\n\n"
            section += f"**摘要**: {summary.get('summary', 'N/A')}\n\n"
            
            if summary.get('key_points'):
                section += "**要点**:\n"
                for point in summary['key_points']:
                    section += f"- {point}\n"
                section += "\n"
            
            if summary.get('consensus'):
                section += f"**共识**: {summary['consensus']}\n\n"
            
            if summary.get('code_references'):
                section += "**代码引用**:\n"
                for ref in summary['code_references']:
                    section += f"- `{ref}`\n"
                section += "\n"
            
            if summary.get('action_items'):
                section += "**行动项**:\n"
                for item in summary['action_items']:
                    section += f"- {item}\n"
                section += "\n"

        # 根邮件
        section += self._generate_email_content(root, is_root=True)
        
        # 回复邮件
        if replies:
            section += f"\n### 回复 ({len(replies)} 条)\n\n"
            for reply in replies:
                section += self._generate_email_content(reply, is_root=False)

        return section

    def _generate_email_content(self, email: Dict, is_root: bool) -> str:
        """生成单封邮件的内容（中英文对照）"""
        level = "###" if is_root else "####"
        title = "原邮件" if is_root else f"回复: {email['from']}"
        
        content = f"{level} {title}\n\n"
        content += f"**作者**: {email['from']}\n"
        content += f"**日期**: {email['date']}\n"
        content += f"**主题**: {email['subject']}\n\n"

        # 翻译主题和正文
        try:
            # 创建邮件信息字典
            email_info = {
                'subject': email['subject'],
                'body': email['body'],
                'from': email['from'],
                'date': email['date']
            }
            
            # 根据翻译器类型调用相应方法
            if hasattr(self.translator, 'translate_email'):
                # GoogleTranslator 和 YoudaoTranslator 使用 translate_email
                translated_info = self.translator.translate_email(email_info)
                translated_subject = translated_info.get('subject_cn', '')
                translated_body = translated_info.get('body_cn', '')
            else:
                # BaseTranslator 使用 translate
                translated_subject = self.translator.translate(email['subject'])
                translated_body = self.translator.translate(email['body'])
                
            content += f"**主题翻译**: {translated_subject}\n\n"
        except Exception as e:
            logger.warning("主题翻译失败: %s", e)
            content += "**主题翻译**: [翻译失败]\n\n"

        # 翻译正文
        try:
            if 'translated_body' not in locals():
                if hasattr(self.translator, 'translate_email'):
                    translated_info = self.translator.translate_email(email_info)
                    translated_body = translated_info.get('body_cn', '')
                else:
                    translated_body = self.translator.translate(email['body'])
            
            # 格式化原文和翻译文本，提高可读性
            formatted_body = self._format_email_body(email['body'])
            formatted_translation = self._format_email_body(translated_body)
            
            content += "**原文**:\n```\n"
            content += formatted_body[:2000]  # 限制长度
            if len(formatted_body) > 2000:
                content += "\n... [内容过长，已截断]"
            content += "\n```\n\n"
            
            content += "**翻译**:\n```\n"
            content += formatted_translation
            content += "\n```\n\n"
        except Exception as e:
            logger.warning("正文翻译失败: %s", e)
            content += "**正文翻译**: [翻译失败]\n\n"

        return content

    def _format_email_body(self, body: str) -> str:
        """格式化邮件正文，提高可读性"""
        if not body:
            return ""
        
        # 处理 diff/patch 代码块，保持原有格式
        lines = body.split('\n')
        formatted_lines = []
        
        for line in lines:
            # 保持代码块格式（diff、patch、代码片段）
            if (line.startswith('diff --git') or 
                line.startswith('@@ ') or 
                line.startswith('+') or 
                line.startswith('-') or
                line.startswith('index ') or
                line.startswith('---') or
                line.startswith('+++')):
                formatted_lines.append(line)
            # 处理长行文本，适当断行（但保留代码格式）
            elif len(line) > 80 and not line.startswith('\t') and not line.lstrip().startswith('>'):
                # 对于普通文本长行，尝试在合适位置断行
                words = line.split(' ')
                current_line = ""
                for word in words:
                    if len(current_line + word) > 80:
                        if current_line:
                            formatted_lines.append(current_line)
                            current_line = word
                        else:
                            # 单词本身超过80字符，直接添加
                            formatted_lines.append(word)
                    else:
                        current_line += (" " + word if current_line else word)
                if current_line:
                    formatted_lines.append(current_line)
            else:
                formatted_lines.append(line)
        
        return '\n'.join(formatted_lines)

    def generate_simple_report(self, threads: List[Dict], topic: str, code_report: str = ""):
        """生成简化版报告（无翻译）"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        # 修复路径分隔符问题，避免主题中的斜杠造成路径错误
        safe_topic = topic.replace('/', '_').replace(' ', '_')
        filename = f"{safe_topic}_simple_{timestamp}.md"
        filepath = os.path.join(self.output_dir, filename)

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(f"# Linux 内核邮件讨论报告 (简化版)\n")
            f.write(f"## 主题: {topic}\n")
            f.write(f"## 线程数: {len(threads)}\n")
            f.write(f"## 生成时间: {datetime.now().strftime('%Y年%m月%d日 %H:%M')}\n\n")
            f.write(self._generate_overview(threads))

            # 代码关联分析
            if code_report:
                f.write(code_report)
                f.write("\n---\n\n")

            for i, thread in enumerate(threads, 1):
                root = thread['root']
                f.write(f"## 线程 {i}: {root['subject']}\n")
                f.write(f"**作者**: {root['from']}\n")
                f.write(f"**日期**: {root['date']}\n")
                f.write(f"**回复数**: {len(thread['replies'])}\n")
                f.write(f"**参与者**: {', '.join(thread['participants'])}\n\n")

        logger.info("简化版报告已生成: %s", filepath)
        return filepath