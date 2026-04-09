"""
Email Translator Package
LKML 知识提取器 - lore.kernel.org 全文搜索 + 线程构建 + 翻译 + 摘要 + 代码关联
"""
__version__ = "4.0.0"

from .lore_client import LoreClient
from .lkml_client import LKMLClient
from .thread_builder import build_threads, Thread
from .translator import create_translator
from .summarizer import OpenClawSummarizer, InteractiveQA
from .code_analyzer import CodeAnalyzer
from .output_generator import OutputGenerator