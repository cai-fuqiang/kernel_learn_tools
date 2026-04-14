---
globs: *
alwaysApply: true
---

# 项目规则: Kernel Learn Tools (LKML 知识提取器)

## 1. 项目背景

从 Linux 内核 Git Commit 出发，一键抓取、构建、翻译、摘要内核邮件列表 (LKML) 讨论。支持批量采集邮件构建知识库。

- **类型**: 后端工具链 (CLI + 本地 HTTP 服务)
- **语言**: Python 3.11+，纯 CPython 标准库为主
- **外部依赖**: 仅 `requests` (HTTP client) + `chardet` (编码检测)，无 Web 框架
- **运行环境**: Linux 远端服务器 (home_pc)，通过 SSH 连接
- **数据存储**: SQLite (knowledge.db + translation_cache.db)，JSON 文件缓存

## 2. 目录结构与文件组织

```
.
├── email_translator/          # 核心库包 (所有可复用模块)
│   ├── config.py              # 常量: DATA_DIR / EMAILS_DIR / OUTPUT_DIR
│   ├── lore_client.py         # lore.kernel.org 全文搜索 (含 Anubis PoW)
│   ├── lore_thread_fetcher.py # Lore 完整线程 mbox 下载 (含 Anubis PoW)
│   ├── lkml_client.py         # lkml.org 备选爬取
│   ├── email_client.py        # IMAP 邮件搜索 (遗留，较少使用)
│   ├── thread_builder.py      # Message-ID/In-Reply-To 线程树构建
│   ├── email_preprocessor.py  # 邮件预处理: 分级/去噪/引用去重
│   ├── translator.py          # 多后端翻译 (Google/有道/API)
│   ├── translation_cache.py   # SQLite 翻译缓存
│   ├── knowledge_db.py        # SQLite 知识库存储层
│   ├── commit_analyzer.py     # Git Commit 解析 → 搜索词提炼
│   ├── code_analyzer.py       # 邮件中代码引用提取
│   ├── summarizer.py          # AI 摘要 + 交互式问答
│   └── output_generator.py    # Markdown 报告生成
├── main.py                    # 入口: 搜索+线程构建+翻译报告
├── pack_for_openclaw.py       # 入口: commit+patchset+邮件打包
├── translate_context.py       # 入口: 翻译 context → HTML (~2400行)
├── batch_collect.py           # 入口: 批量采集邮件入知识库
├── batch_process.py           # 入口: 批量摘要+综合分析+HTML导出
├── build_dashboard.py         # 入口: 扫描 data/ 生成 Dashboard
├── kb_web.py                  # 入口: 知识库 HTTP 浏览器 (SPA)
├── query_kb.py                # 入口: CLI 知识库查询
├── trim_context.py            # 入口: 裁剪 context_full.txt
├── check_threads.py           # 工具: 检查线程完整性
├── check_anubis.py            # 工具: Anubis 挑战页面调试
├── _test_*.py                 # 测试脚本 (下划线前缀)
├── data/                      # 运行时数据 (gitignore)
│   ├── knowledge.db           # SQLite 知识库
│   ├── emails/                # 原始邮件 JSON
│   ├── output/                # 生成产物 (HTML/TXT/Dashboard)
│   └── .cache/                # 翻译缓存
└── config.example.json        # 配置模板
```

## 3. 核心工作流与调用关系

### 工作流 1: 单 Commit 分析
```
main.py --commit <hash>
  → CommitAnalyzer.analyze()        # 提取搜索词/子系统/日期范围
  → LoreClient.search_emails()     # lore 全文搜索 (含 Anubis PoW)
  → build_threads()                 # 构建线程树
  → OutputGenerator.generate_report() → Markdown 报告
```

### 工作流 2: 贪心打包 + 翻译
```
pack_for_openclaw.py <hash> --full
  → CommitAnalyzer + LoreThreadFetcher → context_full.txt
translate_context.py <context_full.txt>
  → 解析 → 多线程翻译 (CachedTranslator) → 自包含 HTML
```

### 工作流 3: 批量知识库
```
batch_collect.py --keywords "..." --date-from ... --date-to ...
  → 流式生产者/消费者并行：
    搜索/规则预筛/AI精筛/入队  ||  下载线程/入库 并行执行
  → collect_jobs.progress 记录“每关键词独立游标”用于断点续传
  → collect_queue 持久化队列，支持失败重试与中断恢复
  → max-emails/max-threads 作为单轮预算，任务可保持 running 并 --resume 继续
batch_process.py --summarize
  → KnowledgeDB 读取未处理线程 → AI 摘要 → 写回 DB
batch_process.py --translate --backend google --workers 32
  → 批量翻译线程邮件 → 生成双语 HTML → 写回 DB (translated_html_path)
  → 多线程模式带动态超时保护 (120s + 每封邮件60s，上限1800s)
kb_web.py
  → 从 knowledge.db 读取 → HTTP 服务展示 SPA
  → 内置翻译功能: 网页端可直接触发翻译 (POST /api/translate)
  → TranslateManager 后台线程翻译，前端实时轮询进度
```

### 模块依赖链
```
main.py ─→ lore_client / lkml_client → thread_builder → output_generator → translator
pack_for_openclaw.py ─→ commit_analyzer → lore_thread_fetcher → email_preprocessor
translate_context.py ─→ translator → translation_cache
batch_collect.py ─→ lore_client → lore_thread_fetcher → thread_builder → knowledge_db
batch_process.py ─→ knowledge_db → translator (AI)
kb_web.py ─→ knowledge_db (SQLite 直接查询) → translator (网页端翻译)
```

## 4. 代码风格与命名约定

### 命名规范
- **模块文件**: `snake_case.py` (如 `lore_thread_fetcher.py`)
- **类名**: `PascalCase` (如 `LoreThreadFetcher`, `KBHandler`)
- **函数/变量**: `snake_case` (如 `fetch_thread`, `email_count`)
- **私有方法**: `_` 前缀 (如 `_fetch_mbox_with_anubis()`)
- **常量**: `UPPER_SNAKE_CASE` (如 `_LORE_MBOX_URL`, `DEFAULT_RELEVANCE_KEYWORDS`)
- **入口脚本**: 根目录下独立 `.py`，以 `if __name__ == "__main__"` 启动
- **测试文件**: `_test_` 前缀 (如 `_test_lore_mbox.py`)

### 编码风格
- 字符串用双引号为主，f-string 格式化
- logging 使用 `%s` 占位符 (不用 f-string)
- 类型注解: `typing` 模块 (Dict, List, Optional, Tuple)
- 文件头部: 三引号 docstring 描述模块用途+用法示例
- 每个类/函数有 docstring 说明
- 缩进 4 空格，行宽不超过 100 字符

### 设计模式
- **策略降级**: 网络请求多策略自动降级 (mbox.gz → raw → 本地 JSON)
- **Session + Cookie**: requests.Session 保持 Anubis cookie
- **缓存层**: SQLite 缓存翻译结果，key=sha256(backend+text)
- **生产者/消费者**: ThreadPoolExecutor 并发搜索/翻译/下载
- **自包含 HTML**: 所有前端输出为单文件 HTML (CSS+JS 内嵌)
- **SPA 模式**: kb_web.py 的前端使用原生 JS 实现 SPA，API 分离

## 5. 数据库结构 (knowledge.db)

```sql
emails          — 邮件元数据+正文 (message_id UNIQUE, relevance_score, body)
threads         — 邮件线程 + AI 摘要 (summary_zh, key_points, consensus)
knowledge_reports — 跨线程综合分析报告
collect_jobs    — 采集任务记录 (断点续传)
email_fts       — FTS5 全文搜索虚拟表
```

## 6. 关键技术约束

### Anubis PoW 防护
- lore.kernel.org 全站部署 Anubis 机器人防护
- `LoreClient` 和 `LoreThreadFetcher` 都已集成 Anubis PoW 求解
- 算法: SHA256(randomData + nonce)，要求前 N 字节为 0
- 高并发时 503 频率显著增加，建议 `--workers 1~2`，加延迟
- `batch_collect.py` 每个 worker 独立 Session/Cookie，并发过高会触发更严限流

### 翻译后端
- Google/有道翻译: 免费，走 urllib，支持 `--proxy`
- API 翻译: OpenAI 兼容 REST，支持 deepseek/kimi/siliconflow/aliyun 等
- 翻译缓存: `data/.cache/translation_cache.db`，用 `--no-cache` 强制刷新

### 邮件数据
- 邮件按 `message_id` 全局去重
- 线程展开: 下载完整 mbox 线程后所有邮件都入库 (含不相关回复)
- 邮件预处理分级: DROP(0) / LOW(500字) / MEDIUM(2000字) / HIGH(5000字)

## 7. 开发注意事项

- **远端执行**: 项目运行在远端 Linux 服务器，通过 SSH 操作，命令需用 `execute_remote_command`
- **不要创建文档**: 除非用户明确要求，不主动创建 README/CHANGELOG 等
- **数据安全**: `config.json` / `apikey.json` 含 API 密钥，不可提交到 Git
- **大文件**: `translate_context.py` 约 2400 行，是最大最复杂的文件，修改时注意定位准确
- **HTML 模板**: 嵌入在 Python 字符串中 (`PAGE_HTML`, `_HTML_TEMPLATE`)，修改时注意转义
- **SQLite 并发**: knowledge_db.py 使用 WAL 模式，多线程写入需加锁 (`db_lock`)
- **网络重试**: 所有 Lore 请求都有重试+UA 轮换机制，失败是正常的（Anubis 防护）

## 8. 常用命令

```bash
# 单 commit 分析
python main.py --commit <hash> --repo /path/to/linux --source lore

# 打包+翻译
python pack_for_openclaw.py <hash> --repo /path/to/linux --full
python translate_context.py data/output/<hash>_context_full.txt --workers 4

# 批量采集
python batch_collect.py --keywords "latency,QoS,SCHED_DEADLINE" \
  --date-from 2017-01-01 --date-to 2018-12-31 --no-ai

# 知识库浏览
python kb_web.py --host 0.0.0.0 --port 8765
python query_kb.py                    # 统计
python query_kb.py search "fair"      # 搜索

# Dashboard
python build_dashboard.py
```