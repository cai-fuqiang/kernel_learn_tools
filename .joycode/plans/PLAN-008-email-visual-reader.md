# PLAN-008: 多邮件缓存可视化阅读器

## 问题分析

项目当前以 commit 为入口，每次运行会在 `data/` 下产生多种缓存文件：
- `data/emails/*.json` — 原始邮件 JSON
- `data/output/*_context_full.txt` — 打包上下文
- `data/output/*_translated.html` — 翻译后的单文件 HTML 报告

**痛点**：多次运行后缓存了多个 commit 的邮件数据，但没有统一入口来浏览、检索和管理这些报告。用户需手动在目录中找文件、逐个打开，缺乏：
1. **全局索引** — 不知道已缓存了哪些 commit 的邮件
2. **快速预览** — 无法不打开文件就了解每份报告的概要
3. **跨报告检索** — 无法按作者/主题/子系统搜索已有缓存
4. **状态追踪** — 不知道哪些已翻译、哪些只有原始数据

## 方案选型

| 方案 | 描述 | 优缺点 |
|------|------|--------|
| A. 静态索引 HTML | 生成一个 `index.html` 汇总页 | 简单，零依赖，但无动态交互 |
| B. 本地 Web 服务 | Python HTTP server + 前端 SPA | 交互强，但需要启动服务 |
| **C. 自包含 Dashboard HTML** | 单个 HTML 文件内嵌 JSON 数据 + JS 交互 | **推荐**：零依赖、离线可用、交互丰富 |

**选择方案 C**：生成一个自包含的 `dashboard.html`，内嵌所有缓存报告的索引元数据，提供筛选/搜索/预览/跳转功能。

## 核心功能设计

### 1. Dashboard 索引页（dashboard.html）
- **卡片列表**：每个 commit/报告一张卡片，显示：
  - Commit Hash（短）+ Subject
  - 作者、日期、子系统标签
  - 邮件数量、线程数、参与者数
  - 状态标识：📧原始 / 📝已翻译 / 📄已打包
  - 文件大小、生成时间
- **搜索/过滤**：
  - 全局关键词搜索（匹配 subject/作者/commit hash）
  - 按子系统、状态、日期范围筛选
  - 按时间/邮件数排序
- **快速操作**：
  - 点击卡片 → 打开对应的 `_translated.html`
  - 预览按钮 → 在 Dashboard 内展示邮件列表摘要
  - 原始数据链接 → 跳转到 context_full.txt

### 2. 数据收集脚本（build_dashboard.py）
- 扫描 `data/output/` 和 `data/emails/` 目录
- 解析每个文件提取元数据（不解析全文，只取头部信息）
- 关联同一 commit 的多种产物（json、txt、html）
- 输出 `data/output/dashboard.html`

### 3. 集成到现有工作流
- `main.py` / `pack_for_openclaw.py` / `translate_context.py` 执行完后自动重建 Dashboard
- 新增 `--no-dashboard` 参数跳过

## TODO: 实现步骤

### Phase 1: 数据收集与元数据提取
- [x] 新建 `build_dashboard.py` 入口脚本
- [x] 实现 `scan_output_dir()` 扫描 data/output/ 下所有产物文件
- [x] 实现 `scan_emails_dir()` 扫描 data/emails/ 下原始邮件 JSON
- [x] 实现 `extract_metadata()` 从文件名和文件头提取 commit hash、subject、日期等
- [x] 实现 `correlate_artifacts()` 关联同一 commit 的 json/txt/html 文件
- [x] 验证：扫描现有 data/ 目录，确认元数据提取正确

### Phase 2: Dashboard HTML 生成
- [x] 设计 Dashboard HTML 模板（内联 CSS + JS）
- [x] 卡片组件：commit 摘要 + 状态标签 + 操作按钮
- [x] 搜索框 + 筛选栏（子系统标签、状态、日期）
- [x] 排序功能（按时间、邮件数、文件大小）
- [x] 响应式布局（桌面网格 + 移动端列表）
- [x] 将元数据序列化为内嵌 JSON，JS 动态渲染卡片
- [x] 验证：生成 dashboard.html 并在浏览器中验证交互

### Phase 3: 集成到现有工作流
- [x] 在 main.py 报告生成后调用 Dashboard 重建
- [x] 在 translate_context.py 翻译完成后调用 Dashboard 重建
- [x] 在 pack_for_openclaw.py 打包完成后调用 Dashboard 重建
- [x] 添加 `--no-dashboard` 参数支持
- [x] 添加独立命令 `python build_dashboard.py` 手动重建

## 技术细节

### 元数据结构
```json
{
  "commit_hash": "d07f09a1f99c",
  "subject": "sched/fair: Propagate enqueue flags",
  "author": "Peter Zijlstra",
  "date": "2023-05-31",
  "subsystem": "sched",
  "artifacts": {
    "emails_json": "data/emails/xxx.json",
    "context_txt": "data/output/xxx_context_full.txt",
    "translated_html": "data/output/xxx_translated.html"
  },
  "stats": {
    "email_count": 15,
    "thread_count": 3,
    "file_size_kb": 401
  },
  "status": "translated",
  "created_at": "2026-04-09T22:09:16"
}
```

### Dashboard 视觉风格
- 延续现有 HTML 报告的暗色主题 CSS 变量
- 卡片 hover 效果 + 子系统彩色标签
- 顶部统计栏：总 commit 数 / 总邮件数 / 总翻译数