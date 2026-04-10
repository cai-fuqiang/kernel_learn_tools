# Changelog

本文件记录项目的所有重要变更。格式基于 [Keep a Changelog](https://keepachangelog.com/)。

---

## [v2] - 2026-04-10

### 主要变更：中英文左右栏对比翻译布局

**变更文件**: `translate_context.py`

将 HTML 翻译报告从「上下布局（翻译在上、原文折叠在下）」全面升级为「左右栏对比布局」，
支持段落级对齐和灵活的面板折叠交互，大幅提升中英文对照阅读体验。

### 新增

#### CSS 样式
- **`.bilingual`** — 双栏容器，flex 布局，左右各占 50%
- **`.bi-panel`** — 面板组件，支持 `.collapsed` 折叠态（收缩为 36px 标题栏）
- **`.bi-hdr`** — 面板标题栏，显示「中文翻译」/「English」标签及折叠按钮
- **`.bi-toggle`** — 折叠/展开按钮（«/»），点击切换面板显示状态
- **`.para-grid`** — 段落对齐网格，CSS Grid 两列布局
- **`.pg-cell`** — 段落单元格，左列中文、右列英文，垂直顶对齐
- **`.pg-cell.pg-orig`** — 英文原文段落（字体稍小、颜色偏灰，视觉区分）
- **`.pg-full`** — 横跨两栏的段落（用于引用、签名等不翻译内容）
- **响应式适配** — `@media (max-width: 768px)` 自动切换为上下堆叠布局
- **`.controls button.active`** — 全局按钮激活态样式

#### JavaScript 交互
- **`biToggle(btn)`** — 单个面板折叠/展开事件处理
- **`biView(mode)`** — 全局视图切换：
  - `'both'` — 双栏对比（默认）
  - `'cn'` — 仅显示中文翻译（折叠英文栏）
  - `'en'` — 仅显示英文原文（折叠中文栏）

#### Python 函数
- **`_split_paragraphs(text)`** — 按空行（`\n\n`）将正文拆分为段落列表
- **`_is_untranslatable(para)`** — 判断段落是否不需要翻译（引用行、签名行、折叠标记），
  此类段落在段落对齐视图中横跨两栏显示
- **`_render_bilingual_body(text_cn, text_orig)`** — 核心渲染函数：
  1. 将中英文正文按段落拆分
  2. 一一配对，段落数不等时短方补空
  3. 生成双栏面板（`.bilingual`）+ 段落对齐网格（`.para-grid`）
  4. 段落对齐网格通过 `<details>` 折叠，默认收起
- **`_render_bilingual_commit(cm_cn, cm_orig)`** — Commit Message 双栏对比渲染

#### 全局控制按钮
- HTML 模板顶部新增三个视图切换按钮：「双栏对比」「仅翻译」「仅原文」
- 按钮带 `.active` 状态指示当前模式

### 变更

#### 邮件正文渲染 (`_html_email_node`)
- **之前**: 有翻译时显示中文 `<pre>` + `<details>` 折叠原文
- **之后**: 有翻译时调用 `_render_bilingual_body()` 生成左右双栏 + 段落对齐
- 无翻译时保持原有单栏显示不变

#### Commit Message 渲染 (`generate_html`)
- **之前**: 翻译 `<pre>` + `<details>` 折叠原文
- **之后**: 调用 `_render_bilingual_commit()` 生成左右双栏

#### 页面布局
- `max-width` 从 `1100px` 扩大到 `1400px`，为双栏提供足够宽度

### 示例输出

- **v1 (旧版)**: `doc/example_output/v1/d07f09a1f99c_translated.html` (428 KB)
  - 上下布局，翻译在上、原文折叠在下
- **v2 (新版)**: `doc/example_output/v2/d07f09a1f99c_translated.html` (810 KB)
  - 左右栏对比，段落对齐，面板可折叠
  - 100 个双栏容器、716 个对齐段落单元格、3 个横跨两栏段落
  - 全局视图切换按钮（双栏/仅翻译/仅原文）

### 技术细节

#### 段落对齐算法
1. 中英文正文各自按 `\n\n`（空行）拆分为段落列表
2. 取两方段落数的最大值，短方末尾补空段落
3. 逐对生成 CSS Grid 的左右单元格
4. 引用行（`> ...`）、签名行（`Signed-off-by:` 等）、折叠标记（`[...已省略...]`）
   识别为「不可翻译段落」，横跨两栏显示

#### 面板折叠机制
- CSS `flex` 布局 + `.collapsed` 类切换
- 折叠态: `flex: 0 0 36px`，隐藏 `.bi-body`
- 展开态: `flex: 1 1 50%`，显示全部内容
- 过渡动画: `transition: flex 0.3s ease`

---

## [v1] - 初始版本

- 完整的 LKML 知识提取器功能
- Commit 驱动的邮件搜索与抓取
- 邮件线程树构建与可视化
- 多翻译后端支持（Google/有道/API/OpenClaw）
- AI 智能摘要与交互式问答
- 邮件预处理（分级/去噪/引用去重）
- HTML/Markdown 报告生成
- 翻译以上下布局呈现（翻译在上，原文通过 `<details>` 折叠在下）