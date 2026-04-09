"""Summarizer - 使用 OpenClaw 对邮件线程进行智能摘要和交互式问答"""
import json, logging, os, subprocess, tempfile, textwrap
from typing import Dict, List

logger = logging.getLogger(__name__)

class OpenClawSummarizer:
    """调用 OpenClaw 生成线程摘要"""
    def __init__(self, model="gpt-4o-mini", base_url=None):
        self.model = model
        self.base_url = base_url

    def summarize_thread(self, thread: Dict) -> Dict:
        """
        对邮件线程生成摘要
        :param thread: thread.to_dict() 结果
        :return: 包含摘要和关键信息的字典
        """
        # 构建提示文本
        prompt = self._build_prompt(thread)
        # 调用 OpenClaw
        summary_text = self._call_openclaw(prompt)
        # 解析摘要
        return self._parse_summary(summary_text, thread)

    def _build_prompt(self, thread: Dict) -> str:
        root = thread['root']
        replies = thread['replies']
        participants = thread['participants']
        date_from, date_to = thread['date_range']

        # 构建邮件内容摘要
        emails_summary = []
        emails_summary.append(f"主题: {root['subject']}")
        emails_summary.append(f"作者: {root['from']} ({root['date']})")
        emails_summary.append(f"内容: {self._trim_body(root['body'], 500)}")

        for reply in replies[:5]:  # 最多取前5个回复
            emails_summary.append(f"\n回复: {reply['from']} ({reply['date']})")
            emails_summary.append(self._trim_body(reply['body'], 300))

        if len(replies) > 5:
            emails_summary.append(f"\n... 还有 {len(replies) - 5} 个回复省略")

        emails_text = "\n".join(emails_summary)

        prompt = f"""
请对以下 Linux 内核邮件讨论线程进行摘要，重点关注：
1. 讨论的核心问题或主题
2. 主要观点和分歧
3. 达成的共识或结论
4. 涉及的代码文件或函数
5. 后续行动项或补丁

参与讨论的人员: {', '.join(participants)}
讨论时间范围: {date_from} 到 {date_to}

邮件内容:
{emails_text}

请以 JSON 格式返回摘要，包含以下字段：
{{
  "topic": "讨论主题",
  "summary": "简要摘要",
  "key_points": ["要点1", "要点2", ...],
  "consensus": "共识或结论",
  "code_references": ["文件路径1", "函数名1", ...],
  "action_items": ["后续行动1", ...]
}}
"""
        return prompt.strip()

    def _trim_body(self, body: str, max_chars: int) -> str:
        """截取邮件正文"""
        if len(body) <= max_chars:
            return body
        return body[:max_chars] + "..."

    def _call_openclaw(self, prompt: str) -> str:
        """调用 OpenClaw CLI"""
        cmd = ["openclaw", "chat", "--prompt", prompt]
        if self.base_url:
            cmd.extend(["--base-url", self.base_url])
        if self.model:
            cmd.extend(["--model", self.model])

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=60
            )
            if result.returncode != 0:
                logger.warning("OpenClaw 调用失败: %s", result.stderr)
                return f"摘要生成失败: {result.stderr}"
            return result.stdout.strip()
        except subprocess.TimeoutExpired:
            logger.error("OpenClaw 调用超时")
            return "摘要生成超时"
        except Exception as e:
            logger.error("OpenClaw 调用异常: %s", e)
            return f"摘要生成异常: {e}"

    def _parse_summary(self, summary_text: str, thread: Dict) -> Dict:
        """解析 OpenClaw 返回的摘要"""
        try:
            # 尝试解析 JSON
            if summary_text.startswith('```json'):
                summary_text = summary_text[7:-3].strip()
            summary_data = json.loads(summary_text)
            return {
                'thread': thread,
                'summary': summary_data,
                'raw_text': summary_text
            }
        except json.JSONDecodeError:
            logger.warning("无法解析 OpenClaw 返回的 JSON，使用原始文本")
            return {
                'thread': thread,
                'summary': {
                    'topic': thread['root']['subject'],
                    'summary': summary_text,
                    'key_points': [],
                    'consensus': '',
                    'code_references': [],
                    'action_items': []
                },
                'raw_text': summary_text
            }


class InteractiveQA:
    """将邮件内容注入 OpenClaw 记忆，支持交互式问答"""

    def __init__(self, model="gpt-4o-mini", base_url=None):
        self.model = model
        self.base_url = base_url

    def start_session(self, threads: List[Dict], topic: str):
        """
        将邮件线程写入临时文件，启动 OpenClaw 交互式会话。
        
        :param threads: 线程字典列表
        :param topic: 搜索主题
        """
        # 1. 将所有邮件内容写入临时文件
        context_file = self._build_context_file(threads, topic)
        
        logger.info("邮件上下文已写入: %s", context_file)
        logger.info("启动 OpenClaw 交互式问答...")
        print(f"\n{'='*60}")
        print(f"  LKML 交互式问答 - 主题: {topic}")
        print(f"  已加载 {len(threads)} 个线程的邮件讨论")
        print(f"  输入问题进行提问，输入 'quit' 或 'exit' 退出")
        print(f"{'='*60}\n")

        # 2. 交互循环
        try:
            while True:
                question = input("Q> ").strip()
                if not question:
                    continue
                if question.lower() in ('quit', 'exit', 'q'):
                    print("退出交互式问答。")
                    break

                answer = self._ask(context_file, question)
                print(f"\nA> {answer}\n")
        except (KeyboardInterrupt, EOFError):
            print("\n退出交互式问答。")
        finally:
            # 清理临时文件
            try:
                os.unlink(context_file)
            except OSError:
                pass

    def _build_context_file(self, threads: List[Dict], topic: str) -> str:
        """将邮件线程内容写入临时文件"""
        fd, path = tempfile.mkstemp(suffix=".txt", prefix="lkml_context_")
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(f"# Linux 内核邮件讨论 - 主题: {topic}\n\n")
            f.write(f"共 {len(threads)} 个讨论线程。\n\n")

            for i, thread in enumerate(threads, 1):
                root = thread['root']
                replies = thread['replies']
                f.write(f"{'='*50}\n")
                f.write(f"## 线程 {i}: {root['subject']}\n")
                f.write(f"参与者: {', '.join(thread['participants'])}\n\n")

                # 根邮件
                f.write(f"--- 发件人: {root['from']} ({root['date']}) ---\n")
                f.write(f"主题: {root['subject']}\n")
                f.write(root.get('body', '')[:3000])
                f.write("\n\n")

                # 回复
                for reply in replies[:10]:  # 最多10个回复
                    f.write(f"--- 回复: {reply['from']} ({reply['date']}) ---\n")
                    f.write(reply.get('body', '')[:2000])
                    f.write("\n\n")

                if len(replies) > 10:
                    f.write(f"... 省略了 {len(replies) - 10} 个回复\n\n")

        return path

    def _ask(self, context_file: str, question: str) -> str:
        """调用 OpenClaw 进行问答"""
        prompt = (
            f"基于以下 Linux 内核邮件讨论内容回答问题。"
            f"请用中文回答。\n\n问题: {question}"
        )
        cmd = ["openclaw", "chat", "--file", context_file, "--prompt", prompt]
        if self.base_url:
            cmd.extend(["--base-url", self.base_url])
        if self.model:
            cmd.extend(["--model", self.model])

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120
            )
            if result.returncode != 0:
                return f"[问答失败] {result.stderr.strip()}"
            return result.stdout.strip() or "[无回答]"
        except subprocess.TimeoutExpired:
            return "[问答超时]"
        except FileNotFoundError:
            return "[未找到 openclaw 命令，请确保已安装]"
        except Exception as e:
            return f"[问答异常] {e}"