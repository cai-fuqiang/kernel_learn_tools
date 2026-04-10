# PLAN-005: 中英文左右栏对比翻译布局（含段落对齐）

## 任务概述
将 `translate_context.py` 的 HTML 报告改为左右栏对比布局，左栏中文翻译、右栏英文原文，支持独立折叠/展开。并通过**逐段落对齐**让中英文内容行对行对应，方便精确对照。

## 段落对齐方案
**核心思路**：将邮件正文按空行分段（`\n\n`），中英文段落一一对应放进同一行的左右单元格中。
- 实现方式：用 HTML `<table>` 或 CSS Grid，每段一行，左列中文、右列英文
- 段落数不等时：多余段落在另一侧留空（中英文段落数不一致是翻译引擎可能的异常情况）
- 代码块/diff/签名行等不翻译内容：合并为单独一行横跨两栏显示
- 这不需要修改翻译逻辑，只在渲染阶段做段落拆分和配对

## TODO: 修改 translate_context.py

### 1. CSS 样式新增
- [ ] `.bilingual-container`：左右两栏 flex 布局（各 50%），含折叠交互
- [ ] `.bilingual-panel` + `.panel-header`：面板样式，带折叠按钮
- [ ] `.collapsed` 折叠态：宽度收缩为标题栏，另一栏自动扩展
- [ ] `.para-table`：段落对齐表格样式（每段一行、垂直顶对齐）
- [ ] 响应式处理：窄屏自动切换上下布局

### 2. 段落对齐渲染函数
- [ ] 新增 `_render_bilingual_paragraphs(text_cn, text_orig)` 函数
- [ ] 按 `\n\n` 拆分中英文段落，一一配对
- [ ] 每对段落渲染为左右两列（table row 或 grid row）
- [ ] 段落数不等时，短边补空单元格

### 3. 邮件正文渲染改造 (`_html_email_node`)
- [ ] 有翻译时：用 `.bilingual-container` 包裹，内部调用段落对齐渲染
- [ ] 无翻译时：保持原有单栏显示不变

### 4. Commit Message 渲染改造 (`generate_html`)
- [ ] commit message 有翻译时同样改为左右栏 + 段落对齐

### 5. JavaScript 交互
- [ ] 面板折叠/展开按钮事件
- [ ] 全局控制按钮：双栏 / 仅翻译 / 仅原文

### 6. 验证
- [ ] 用现有 `doc/example_output/` 数据验证渲染效果