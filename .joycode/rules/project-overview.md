# 项目概述: Kernel Email (LKML 知识提取器)

## 定位
从 Linux 内核 Git Commit 出发，一键抓取、构建、翻译、摘要内核邮件列表(LKML)讨论。

## 核心工作流

```
Git Commit → 搜索词提炼 → Lore/LKML 邮件抓取 → 预处理分级 → 翻译 → HTML 报告 → Dashboard
```

## 三个入口脚本

| 脚本 | 用途 | 典型用法 |
|------|------|----------|
| `main.py` | 搜索+线程构建+报告 | `python main.py --commit HEAD --repo /path/to/linux` |
| `pack_for_openclaw.py` | 打包 commit+patchset+邮件线程给 AI | `python pack_for_openclaw.py <hash> --repo /path --full` |
| `translate_context.py` | 翻译 context_full.txt → HTML | `python translate_context.py data/output/xxx_context_full.txt --workers 4` |
| `build_dashboard.py` | 扫描 data/ 生成 Dashboard 索引页 | `python build_dashboard.py` |

## 核心模块 (`email_translator/`)

| 模块 | 职责 |
|------|------|
| `commit_analyzer.py` | Git Commit 解析、搜索词提炼 |
| `lore_client.py` | lore.kernel.org 全文搜索 |
| `lkml_client.py` | lkml.org 爬取 |
| `lore_thread_fetcher.py` | Lore 完整线程下载 (含 Anubis PoW 绕过) |
| `thread_builder.py` | 邮件线程树构建 (Message-ID/In-Reply-To/References) |
| `email_preprocessor.py` | 邮件预处理 (分级 HIGH/MEDIUM/LOW/DROP、去噪) |
| `translator.py` | 多后端翻译 (Google/有道/API) |
| `translation_cache.py` | SQLite 翻译缓存 (key=sha256(backend+body)) |
| `summarizer.py` | AI 摘要 + 交互式问答 |
| `output_generator.py` | main.py 专用的报告生成 |
| `config.py` | 项目配置 (目录常量: DATA_DIR/EMAILS_DIR/OUTPUT_DIR) |

## translate_context.py 内部结构 (~2400行单文件)

这是最大最复杂的文件，包含:

1. **解析器**: `parse_commit_section()`, `parse_diff_section()`, `parse_emails()`, `parse_analysis_checklist()`
2. **翻译管线**: `translate_body()`, `should_translate()`, `_translate_diff_comments()`, `CachedTranslator`
3. **代码检测**: `_is_code_or_data_line()` — 判断是否代码/数据行，防止误翻译
4. **段落对齐**: `_optimal_alignment()` — DP 全局最优匹配中英文段落
5. **辅助判断**: `_is_untranslatable()`, `_is_untranslatable_in_context()`, `_fuzzy_match_untranslatable()`
6. **HTML 模板**: `_HTML_TEMPLATE` — 自包含 HTML 模板 (暗色/亮色主题、搜索/过滤、侧边栏目录、键盘快捷键)
7. **HTML 渲染**: `_render_bilingual_body()`, `_html_email_node()`, `generate_html()`
8. **线程树**: `ThreadNode`, `_build_thread_tree()`
9. **主程序**: `main()` — 解析→翻译(多线程)→生成HTML→重建Dashboard

## 数据目录

```
data/
├── emails/        ← 原始邮件 JSON
├── output/        ← context_full.txt / _translated.html / dashboard.html
└── .cache/        ← SQLite 翻译缓存
```

## 测试数据

- `doc/example_output/v2/d07f09a1f99c_context_full.txt` (248KB) — 标准测试输入
- `tests/test_code_detection.py` — 60+ 样本的代码行检测回归测试
- `_test_html_interactive.py` — 100封邮件+多线程翻译的端到端测试

## HTML 报告功能特性

### 翻译报告 (_translated.html)
- 中英文左右栏对比 (段落级 DP 对齐)
- 邮件线程树状缩进 + details 折叠
- 聚焦视图 (单封邮件全宽展示)
- **搜索/过滤**: 工具栏搜索框、作者下拉、类型下拉
- **侧边栏目录**: 右侧滑出、线程跳转
- **键盘快捷键**: j/k 上下、Enter 折叠、f 聚焦、/ 搜索、? 帮助
- **主题切换**: 暗色/亮色，localStorage 持久化
- email-node 上的 data 属性: data-author, data-subject, data-tag, data-body-preview

### Dashboard (dashboard.html)
- 卡片网格展示所有缓存报告
- 状态标识: 已翻译/已打包/原始
- 搜索 + 子系统筛选 + 状态筛选 + 排序
- 预览模态框、暗色/亮色主题
- 由 `build_dashboard.py` 的 `generate_dashboard()` 生成
- 三个工作流完成后自动重建 (--no-dashboard 跳过)
- 链接路径相对于 dashboard.html 所在目录 (data/output/)

## 关键技术参数

- 翻译并发: `--workers N` (默认1, 建议4-8)
- 翻译缓存: SQLite, `--no-cache` 强制刷新
- 增量检测: HTML 中 `<meta name="source-hash">` 匹配输入文件 hash, `--force` 跳过
- 翻译后端: `--backend google|youdao|api`
- 代理: `--proxy 127.0.0.1:7897`