#!/usr/bin/env python3
"""LKML 知识提取器 - 主程序

使用示例:
    # 以 git commit 为入口，自动提炼搜索词并查找相关邮件讨论
    python main.py --commit HEAD --kernel-src /path/to/linux --source lkml

    # 指定 commit hash
    python main.py --commit abc1234 --kernel-src /path/to/linux

    # 使用 lkml.org 搜索（备选方案）+ 多关键词组合
    python main.py --topic "sched AND fair" --source lkml --max-emails 10

    # 生成带 OpenClaw 摘要的报告
    python main.py --topic "sched/fair" --summarize --max-emails 30

    # 搜索后进入 OpenClaw 交互问答
    python main.py --topic "FAIR_SLEEPING" --source lkml --interactive

    # 包含代码关联分析
    python main.py --topic "sched/fair" --source lkml --max-emails 5 --code-analysis
"""
import argparse, json, logging, os, sys
from datetime import datetime

from email_translator.lore_client import LoreClient
from email_translator.lkml_client import LKMLClient
from email_translator.thread_builder import build_threads
from email_translator.summarizer import OpenClawSummarizer, InteractiveQA
from email_translator.code_analyzer import CodeAnalyzer
from email_translator.commit_analyzer import CommitAnalyzer
from email_translator.output_generator import OutputGenerator
from email_translator.config import OUTPUT_DIR, EMAILS_DIR

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def main():
    parser = argparse.ArgumentParser(description='LKML 知识提取器')

    # ── 入口模式（二选一）──────────────────────────────────────────────
    input_group = parser.add_mutually_exclusive_group(required=True)
    input_group.add_argument(
        '--commit',
        metavar='REF',
        help='以 git commit 为入口（hash / HEAD / HEAD~N 等），自动提炼搜索词'
    )
    input_group.add_argument(
        '--topic',
        help='直接指定搜索主题，支持 AND/OR 组合 (如: "sched AND fair")'
    )

    # commit 模式专用参数
    parser.add_argument('--repo', default='.',
                        help='本地 git 仓库路径（--commit 模式使用，默认当前目录）')
    parser.add_argument('--search-term-index', type=int, default=0,
                        help='使用第 N 个自动提炼的搜索词（默认 0 = 第一个）')
    parser.add_argument('--list', default='all', help='邮件列表名称 (如: linux-kernel, linux-mm, linux-sched)')
    parser.add_argument('--source', choices=['lore', 'lkml'], default='lore', help='数据源')
    parser.add_argument('--max-emails', type=int, default=20, help='最大邮件数')
    parser.add_argument('--author', help='作者过滤')
    parser.add_argument('--date-from', help='开始日期 (YYYY-MM-DD)')
    parser.add_argument('--date-to', help='结束日期 (YYYY-MM-DD)')
    
    # 翻译参数
    parser.add_argument('--translator', choices=['google', 'youdao', 'api', 'openclaw'], 
                        default='google', help='翻译后端')
    parser.add_argument('--api-base-url', help='API 翻译的基础URL')
    parser.add_argument('--api-key', help='API 翻译的密钥')
    parser.add_argument('--api-model', default='gpt-4o-mini', help='API 翻译的模型')
    
    # 摘要参数
    parser.add_argument('--summarize', action='store_true', help='使用 OpenClaw 生成摘要')
    parser.add_argument('--openclaw-model', default='gpt-4o-mini', help='OpenClaw 模型')
    parser.add_argument('--openclaw-base-url', help='OpenClaw 基础URL')
    
    # 交互式问答
    parser.add_argument('--interactive', action='store_true',
                        help='启动 OpenClaw 交互式问答（搜索邮件后进入问答模式）')
    
    # 代码关联分析
    parser.add_argument('--code-analysis', action='store_true',
                        help='提取邮件中的代码引用（patch文件、函数名等）')
    parser.add_argument('--kernel-src', help='本地内核源码路径（用于代码关联验证）')
    
    # 输出参数
    parser.add_argument('--output-dir', default=OUTPUT_DIR, help='输出目录')
    parser.add_argument('--simple', action='store_true', help='生成简化版报告（无翻译）')
    parser.add_argument('--save-emails', action='store_true', help='保存原始邮件数据')
    parser.add_argument('--no-dashboard', action='store_true', help='跳过 Dashboard 重建')
    
    args = parser.parse_args()
    
    try:
        # 步骤0: 如果是 --commit 模式，先解析 commit 提炼搜索词
        commit_info = None
        if args.commit:
            logger.info("解析 commit: %s (repo: %s)", args.commit, args.repo)
            ca = CommitAnalyzer(repo_path=args.repo)
            commit_info = ca.analyze(args.commit)

            print("\n" + "="*60)
            print("  [Commit 分析结果]")
            print("="*60)
            print(commit_info.summary())
            print(f"\n  子系统   : {', '.join(commit_info.subsystems) or '未识别'}")
            print(f"  搜索日期 : {commit_info.date_from} ~ {commit_info.date_to}")
            print(f"  候选搜索词:")
            for idx, term in enumerate(commit_info.search_terms):
                marker = " ◀ 使用中" if idx == args.search_term_index else ""
                print(f"    [{idx}] {term}{marker}")
            print("="*60 + "\n")

            # 用选定的搜索词和日期范围覆盖 args
            idx = min(args.search_term_index, len(commit_info.search_terms) - 1)
            args.topic = commit_info.search_terms[idx]
            if not args.date_from:
                args.date_from = commit_info.date_from
            if not args.date_to:
                args.date_to = commit_info.date_to

            # 如果 commit 里有 lore 直链，打印提示
            if commit_info.lore_url:
                print(f"  [提示] 发现 Lore 直链，将优先使用: {commit_info.lore_url}\n")

            logger.info("使用搜索词: %r  日期: %s ~ %s",
                        args.topic, args.date_from, args.date_to)

        # 步骤1: 搜索邮件
        logger.info("开始搜索邮件...")
        if args.source == 'lore':
            client = LoreClient()
            emails = client.search_emails(
                topic=args.topic,
                list_name=args.list,
                max_emails=args.max_emails,
                author=args.author,
                date_from=args.date_from,
                date_to=args.date_to
            )
        else:
            client = LKMLClient()
            emails = client.search_emails(
                topic=args.topic,
                max_emails=args.max_emails,
                author=args.author,
                date_from=args.date_from,
                date_to=args.date_to
            )
        
        if not emails:
            logger.warning("未找到相关邮件")
            return
        
        logger.info("找到 %d 封邮件", len(emails))
        
        # 保存原始邮件数据（可选）
        if args.save_emails:
            os.makedirs(str(EMAILS_DIR), exist_ok=True)
            safe_topic = args.topic.replace('/', '_').replace(' ', '_')
            emails_file = os.path.join(str(EMAILS_DIR), f"{safe_topic}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")
            with open(emails_file, 'w', encoding='utf-8') as f:
                json.dump(emails, f, ensure_ascii=False, indent=2)
            logger.info("原始邮件数据已保存: %s", emails_file)
        
        # 步骤2: 构建线程
        logger.info("构建邮件线程...")
        threads = build_threads(emails)
        logger.info("构建完成：%d 个线程", len(threads))
        
        # 步骤3: 生成摘要（可选）
        if args.summarize:
            logger.info("生成 OpenClaw 摘要...")
            summarizer = OpenClawSummarizer(
                model=args.openclaw_model,
                base_url=args.openclaw_base_url
            )
            
            for thread in threads:
                thread_dict = thread.to_dict()
                summary_result = summarizer.summarize_thread(thread_dict)
                thread.summary = summary_result['summary']
                logger.debug("线程摘要生成完成: %s", thread.root['subject'])
        
        # 步骤4: 转换为 dict 列表
        thread_dicts = []
        for thread in threads:
            td = thread.to_dict()
            if hasattr(thread, 'summary'):
                td['summary'] = thread.summary
            thread_dicts.append(td)
        
        # 步骤5: 代码关联分析（可选）
        code_report = ""
        if args.code_analysis:
            logger.info("执行代码关联分析...")
            analyzer = CodeAnalyzer(kernel_src=args.kernel_src)
            code_report = analyzer.generate_code_report(thread_dicts)
            logger.info("代码关联分析完成")
        
        # 步骤6: 生成报告
        logger.info("生成报告...")
        translator_kwargs = {}
        if args.translator == 'api':
            if args.api_base_url:
                translator_kwargs['base_url'] = args.api_base_url
            if args.api_key:
                translator_kwargs['api_key'] = args.api_key
            if args.api_model:
                translator_kwargs['model'] = args.api_model
        
        generator = OutputGenerator(
            output_dir=str(args.output_dir),
            translator_backend=args.translator,
            **translator_kwargs
        )
        
        if args.simple:
            filepath = generator.generate_simple_report(thread_dicts, args.topic, code_report=code_report)
        else:
            filepath = generator.generate_report(
                thread_dicts, args.topic,
                with_summary=args.summarize,
                code_report=code_report
            )
        
        logger.info("报告生成完成: %s", filepath)

        # 重建 Dashboard
        if not args.no_dashboard:
            try:
                from build_dashboard import generate_dashboard
                generate_dashboard()
            except Exception as e:
                logger.debug("Dashboard 重建失败（可忽略）: %s", e)

        # 如果是 commit 模式，在报告末尾追加 commit 摘要
        if commit_info:
            with open(filepath, 'a', encoding='utf-8') as f:
                f.write("\n---\n\n## Commit 入口信息\n\n")
                f.write(f"```\n{commit_info.summary()}\n```\n\n")
                f.write(f"**子系统**: {', '.join(commit_info.subsystems) or '未识别'}  \n")
                f.write(f"**使用搜索词**: `{args.topic}`  \n")
                f.write(f"**搜索日期范围**: {args.date_from} ~ {args.date_to}  \n")
                f.write(f"**所有候选搜索词**: {' | '.join(commit_info.search_terms)}\n")

        # 步骤7: 交互式问答（可选，最后执行）
        if args.interactive:
            qa = InteractiveQA(
                model=args.openclaw_model,
                base_url=args.openclaw_base_url
            )
            qa.start_session(thread_dicts, args.topic)
        
    except Exception as e:
        logger.error("程序执行失败: %s", e)
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == '__main__':
    main()