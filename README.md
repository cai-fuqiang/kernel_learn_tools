# LKML 知识提取器 (Kernel Email)

> **从 Linux 内核 Git Commit 出发，一键抓取、构建、翻译、摘要内核邮件列表讨论，助你深入理解每一行内核代码背后的设计决策。**

---

## 项目初衷

Linux 内核的开发流程高度依赖邮件列表（LKML）。每一个 patch 的提交、review、讨论、修改都发生在邮件中。然而对于内核开发者和学习者来说，阅读这些邮件存在巨大障碍：

1. **信息分散**：讨论散落在 `lore.kernel.org`、`lkml.org` 等多个归档站点，手动查找效率极低
2. **语言障碍**：邮件全部为英文，对非英语母语的开发者不友好
3. **线程混乱**：一个 patchset 可能有数十封 review 邮件，手动理清讨论脉络非常困难
4. **上下文缺失**：看到一个 commit，往往不知道背后经历了怎样的讨论和修改历程

**本项目正是为了解决这些痛点而生。** 它以 Git Commit 为入口，自动化完成以下全链路工作：

- 从 commit message 中智能提炼搜索关键词
- 在 lore.kernel.org / lkml.org 上搜索相关邮件讨论
- 自动构建邮件线程树，还原讨论脉络
- 将英文邮件翻译为中文（支持多种免费/付费翻译后端）
- 使用 AI 对讨论内容生成智能摘要
- 提取邮件中的代码引用（patch 文件、函数名等）
- 生成结构清晰的 Markdown/HTML 报告

## 核心特性

| 特性 | 说明 |
|------|------|
| **Commit 驱动** | 以 `git commit` 为入口，自动解析 subject、author、files，提炼搜索词 |
| **多数据源** | 支持 `lore.kernel.org`（全文搜索）和 `lkml.org`（公开归档爬取） |
| **完整线程抓取** | 从 Lore 直链下载完整 mbox 线程，含所有 review 回复 |
| **Anubis PoW 绕过** | 自动求解 Lore 的 Anubis 机器人防护 challenge |
| **邮件线程构建** | 基于 Message-ID / In-Reply-To / References 构建线程树 |
| **多翻译后端** | Google 翻译（免费）、有道翻译（免费）、OpenAI 兼容 API、OpenClaw CLI |
| **智能摘要** | 调用 AI 对线程讨论生成结构化摘要（主题、要点、共识、代码引用） |
| **交互式问答** | 搜索邮件后进入交互式 AI 问答模式 |
| **代码关联分析** | 自动提取邮件中的 diff/patch 文件路径和函数名 |
| **邮件预处理** | 智能分级（DROP/LOW/MEDIUM/HIGH）、去噪、引用去重、签名去除 |
| **灵活输出** | Markdown 报告、HTML 报告（树状缩进+折叠交互）、AI 上下文打包 |

## 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                          入口脚本                                │
│  main.py       pack_for_openclaw.py    translate_context.py     │
│  (搜索+报告)   (打包AI上下文)          (翻译context文档)          │
└──────────┬──────────────┬───────────────────┬───────────────────┘
           │              │                   │
┌──────────▼──────────────▼───────────────────▼───────────────────┐
│                     email_translator 核心包                       │
│                                                                  │
│  commit_analyzer.py    ← Git Commit 解析与搜索词提炼              │
│  lore_client.py        ← lore.kernel.org 全文搜索客户端           │
│  lkml_client.py        ← lkml.org 公开归档爬取客户端              │
│  lore_thread_fetcher.py← Lore 完整线程下载（含 Anubis PoW）       │
│  thread_builder.py     ← 邮件线程树构建                           │
│  email_preprocessor.py ← 邮件预处理（分级/去噪/引用去重）          │
│  translator.py         ← 多后端翻译（Google/有道/API/OpenClaw）    │
│  summarizer.py         ← AI 智能摘要 + 交互式问答                 │
│  code_analyzer.py      ← 代码关联分析（提取diff文件/函数名）       │
│  output_generator.py   ← 报告生成（Markdown 中英文对照）           │
│  email_client.py       ← IMAP 邮件客户端（直连邮箱）               │
│  config.py             ← 全局配置                                 │
└──────────────────────────────────────────────────────────────────┘
```

## 快速开始

### 1. 环境要求

- Python 3.10+
- Git（用于 commit 分析）
- （可选）`requests` 库（Lore 线程抓取用到）
- （可选）`openclaw` CLI（AI 摘要和交互式问答用到）

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

`requirements.txt` 中仅包含可选增强依赖：
- `chardet>=5.0.0` — 自动检测邮件编码（多语言兼容性）

大部分功能仅依赖 Python 标准库（`imaplib`、`email`、`json`、`subprocess`、`urllib`）。

### 3. 配置（可选）

复制示例配置文件并根据需要修改：

```bash
cp config.example.json config.json
```

配置文件字段说明：

```json
{
  "email":      "your@email.com",        // IMAP 邮箱地址
  "password":   "your_email_app_password",// 邮箱授权码
  "provider":   "gmail",                 // 邮箱服务商 (gmail/outlook/qq/163/aliyun)

  "backend":      "api",                 // 翻译后端 (api/openclaw/google/youdao)
  "api_key":      "sk-xxxxxxxx",         // API 密钥（使用 api 后端时必填）
  "api_provider": "deepseek",            // API 服务商 (openai/deepseek/kimi/siliconflow/aliyun)
  "api_base_url": "",                    // 自定义 API 地址（留空使用默认）
  "model":        "",                    // 模型名称（留空使用默认）

  "max_emails": 20,                      // 最大邮件数
  "days":       30                       // 搜索天数范围
}
```

> **提示**: 使用 Google 翻译或有道翻译后端时无需 API Key，开箱即用。

## 使用方法

### 方式一：以 Git Commit 为入口（推荐）

这是最常用的方式。指定一个内核 commit，工具会自动提炼搜索词，查找相关邮件讨论。

```bash
# 分析 HEAD commit，在当前仓库搜索
python main.py --commit HEAD --repo /path/to/linux --source lore

# 指定具体 commit hash
python main.py --commit abc1234 --repo /path/to/linux

# 使用第二个候选搜索词（默认使用第一个）
python main.py --commit HEAD --repo /path/to/linux --search-term-index 1
```

工具会输出 commit 分析结果，包括：
- 自动识别的子系统（如 sched、mm、fs）
- 建议的搜索日期范围（commit 前 60 天 ~ 后 14 天）
- 多个候选搜索词（按优先级排序）
- Lore 直链（如果 commit message 中包含）

### 方式二：直接指定搜索主题

```bash
# 基本搜索
python main.py --topic "sched/fair" --source lore --max-emails 20

# 多关键词组合（AND/OR）
python main.py --topic "sched AND fair" --source lore

# 指定日期范围和作者
python main.py --topic "FAIR_SLEEPING" --date-from 2024-01-01 --date-to 2024-06-30 --author "Peter"

# 指定邮件列表
python main.py --topic "mm/vmalloc" --list linux-mm
```

### 方式三：打包 AI 上下文（pack_for_openclaw.py）

将 commit + 完整 patchset + 邮件线程打包为一份文本文件，一次性喂给 AI 进行分析。

```bash
# 最简模式（离线，只包含 commit + diff）
python pack_for_openclaw.py <hash> --repo /path/to/linux

# 完整模式（在线抓取 Lore 线程 + 同系列 commit）
python pack_for_openclaw.py <hash> --repo /path/to/linux --full

# 使用本地已保存的邮件 JSON
python pack_for_openclaw.py <hash> --repo /path/to/linux --full --email-json data/emails/xxx.json
```

### 方式四：翻译已有的 context 文件（translate_context.py）

将 `pack_for_openclaw.py` 生成的完整版 context 文件翻译为可读文档。

```bash
# 翻译为 HTML（默认，带树状缩进和折叠交互）
python translate_context.py data/output/xxxx_context_full.txt

# 翻译为 Markdown
python translate_context.py data/output/xxxx_context_full.txt --format md

# 使用有道翻译后端
python translate_context.py data/output/xxxx_context_full.txt --backend youdao

# 只解析不翻译（调试用）
python translate_context.py data/output/xxxx_context_full.txt --dry-run
```

### 方式五：裁剪 context 文件（trim_context.py）

将完整版 context 按字符数裁剪，生成适合喂给 AI 模型的精简版。

```bash
# 默认裁剪到 100K 字符
python trim_context.py data/output/xxxx_context_full.txt

# 指定最大字符数
python trim_context.py data/output/xxxx_context_full.txt --max-size 200000
```

## 高级功能

### AI 智能摘要

使用 `--summarize` 参数，调用 OpenClaw 对每个讨论线程生成结构化摘要：

```bash
python main.py --topic "sched/fair" --summarize --openclaw-model gpt-4o-mini
```

摘要包含：讨论主题、核心要点、共识/结论、涉及的代码引用、后续行动项。

### 交互式问答

使用 `--interactive` 参数，搜索邮件后进入交互式 AI 问答模式：

```bash
python main.py --topic "FAIR_SLEEPING" --source lore --interactive
```

进入问答后可以自由提问，AI 会基于搜索到的邮件内容回答。

### 代码关联分析

使用 `--code-analysis` 参数，自动提取邮件中的代码引用：

```bash
python main.py --topic "sched/fair" --code-analysis --kernel-src /path/to/linux
```

分析结果包含：涉及的源码文件、函数名、Patch 邮件列表，并可验证本地源码中是否存在对应文件。

### 翻译后端选择

```bash
# Google 翻译（默认，免费）
python main.py --topic "mm/vmalloc" --translator google

# 有道翻译（免费备选）
python main.py --topic "mm/vmalloc" --translator youdao

# OpenAI 兼容 API（需要 key）
python main.py --topic "mm/vmalloc" --translator api \
  --api-base-url https://api.deepseek.com/v1 \
  --api-key sk-xxx --api-model deepseek-chat

# OpenClaw CLI
python main.py --topic "mm/vmalloc" --translator openclaw
```

支持的 API 服务商：
| 服务商 | base_url | 默认模型 |
|--------|----------|----------|
| OpenAI | `https://api.openai.com/v1` | gpt-4o-mini |
| DeepSeek | `https://api.deepseek.com/v1` | deepseek-chat |
| Kimi (Moonshot) | `https://api.moonshot.cn/v1` | moonshot-v1-8k |
| 硅基流动 | `https://api.siliconflow.cn/v1` | deepseek-ai/DeepSeek-V3 |
| 阿里百炼 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | qwen-turbo |

## 输出说明

所有输出文件保存在 `data/output/` 目录：

| 文件类型 | 说明 |
|----------|------|
| `*_context_full.txt` | 完整版 AI 上下文（commit + diff + 邮件线程） |
| `*_translated.html` | 翻译后的 HTML 文档（带折叠交互） |
| `*_YYYYMMDD_HHMMSS.md` | 中英文对照 Markdown 报告 |
| `*_simple_*.md` | 简化版报告（无翻译） |

原始邮件数据保存在 `data/emails/` 目录（使用 `--save-emails` 参数时）。

## 邮件预处理策略

工具内置了智能邮件预处理管线，对邮件进行分级和去噪：

| 优先级 | 说明 | 字符预算 |
|--------|------|----------|
| **DROP** | tip-bot2 通知、自动化测试通知 | 直接丢弃 |
| **LOW** | PATCH 正文（去掉 diff，只保留 commit message） | 500 字符 |
| **MEDIUM** | Cover letter、含性能数据的测试报告 | 2000 字符 |
| **HIGH** | review 讨论（包含实质内容的回复） | 5000 字符 |

处理流水线：分类 → 过滤 → PATCH 去 diff → 引用去重 → 签名去除

## 项目结构

```
kernel_email/
├── main.py                     # 主程序入口（搜索 + 翻译 + 报告）
├── pack_for_openclaw.py        # AI 上下文打包脚本
├── translate_context.py        # context 文件翻译脚本
├── trim_context.py             # context 文件裁剪脚本
├── config.example.json         # 示例配置文件
├── requirements.txt            # Python 依赖
├── email_translator/           # 核心功能包
│   ├── __init__.py
│   ├── config.py               # 全局配置
│   ├── commit_analyzer.py      # Git Commit 解析器
│   ├── lore_client.py          # lore.kernel.org 搜索客户端
│   ├── lkml_client.py          # lkml.org 爬取客户端
│   ├── lore_thread_fetcher.py  # Lore 完整线程下载器
│   ├── email_client.py         # IMAP 邮件客户端
│   ├── email_preprocessor.py   # 邮件预处理器
│   ├── thread_builder.py       # 邮件线程构建器
│   ├── translator.py           # 多后端翻译模块
│   ├── summarizer.py           # AI 摘要与问答
│   ├── code_analyzer.py        # 代码关联分析
│   └── output_generator.py     # 报告生成器
├── data/
│   ├── emails/                 # 原始邮件存储
│   └── output/                 # 输出报告存储
└── logs/                       # 日志目录
```

## 常见问题

### Q: Lore 抓取被 Anubis 拦截怎么办？

工具内置了 Anubis PoW（Proof of Work）自动求解器。如果遇到 challenge，会自动求解 SHA256 PoW 并获取 cookie。如果 challenge 为 null（直接拒绝），会降级到其他抓取策略。

### Q: 翻译质量不满意？

建议优先使用 API 翻译后端（如 DeepSeek），翻译质量远优于免费的 Google/有道翻译。Google 翻译适合快速预览，API 翻译适合正式阅读。

### Q: 搜索不到相关邮件？

- 尝试切换数据源：`--source lore`（推荐）或 `--source lkml`
- 调整搜索词：使用 `--search-term-index` 切换候选搜索词
- 扩大日期范围：使用 `--date-from` 和 `--date-to`
- 增加最大邮件数：`--max-emails 50`

### Q: 如何处理大型 patchset？

使用 `pack_for_openclaw.py --full` 模式，它会：
1. 自动从 Lore 下载完整线程（含所有 review）
2. 查找同系列相关 commit
3. 智能预处理（去噪/去重/分级）
4. 打包为一份完整的上下文文件

然后使用 `trim_context.py` 裁剪到适合 AI 模型的大小。

## 许可证

本项目仅供学习和研究使用。