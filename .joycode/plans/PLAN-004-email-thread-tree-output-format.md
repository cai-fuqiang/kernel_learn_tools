# PLAN-004: 邮件线程树输出格式改进

## 问题分析

当前 Markdown 输出存在两个核心问题：

1. **无法展示 Re: 回复的树状关系** — 当前按 base_subject 分组，但组内所有回复是扁平排列的，丢失了 A→B→C 的回复链
2. **Markdown `<details>` 嵌套渲染不稳定** — 多层 details 嵌套在不同渲染器表现不一致，且邮件正文含 HTML 特殊字符会破坏结构

## 方案对比

### 方案A: 改进 Markdown（缩进 + blockquote 层级）
- 用 `>` 引用层级模拟回复嵌套
- 问题：超过 2 层就不可读，正文含 `>` 引用行会冲突

### 方案B: 输出为 HTML 文件
- 完全控制渲染，CSS 控制缩进、折叠、颜色
- 可用 `margin-left` 展示回复层级，`<details>` 在 HTML 中行为确定
- 可加 CSS 线条连接父子邮件（类似 GitHub Issues 时间线）
- 浏览器直接打开，无需额外工具
- **推荐方案**

### 方案C: 输出为 JSON + 独立 HTML 查看器
- 数据与展示分离，灵活但复杂度高
- 过度工程化，不适合当前场景

## 选定方案：B — HTML 输出

### 设计要点

1. **线程树构建**：利用 `In-Reply-To` / `References` 头（如果 context_full 中有），或退化为 Re: 前缀匹配
2. **树状缩进渲染**：每层回复 `margin-left: 24px`，左侧带竖线连接
3. **折叠控制**：
   - 根邮件默认展开
   - 回复默认折叠（`<details>`），点击展开
   - 每封邮件的"原文"折叠在底部
4. **视觉标识**：
   - 优先级颜色标签（HIGH=红, MEDIUM=橙, LOW=灰）
   - 作者头像占位（首字母圆形）
   - 时间戳右对齐
5. **结构**：单个自包含 HTML 文件（CSS/JS 内联），无外部依赖

## TODO: 实现步骤

- [x] 1. 改进 `_build_threads()` 支持真正的树状结构（parent-child）
  - ThreadNode 递归树结构替换旧的扁平 {root, replies}
  - Re: 层级匹配 + _normalize_subject() 模糊分组（60字符截断）
  - depth_buckets 按层级构建，按时间匹配 parent-child
- [x] 2. 新建 HTML 模板（内联 CSS）
  - CSS 变量暗色主题 + 响应式布局
  - margin-left + border-left 树缩进
  - details 折叠样式（根展开/回复折叠）
  - 优先级颜色标签 + 首字母头像
- [x] 3. 重写 `generate_markdown()` → `generate_html()`
  - _html_email_node() 递归渲染 ThreadNode
  - commit 表格 + diff 代码块 + 线程树 + 分析清单
- [x] 4. 更新 `translate_context.py` 主程序
  - --format html(默认)/md 参数
  - 输出 .html 后缀
- [x] 5. 端到端测试 ✓
  - HTML 验证通过：details 180/180 平衡、20 线程、100 卡片、0 乱码
  - PATCH 按编号有序排列（00→15）
- [x] 6. 修复四大问题（额外追加）
  - 乱码修复：lore_thread_fetcher 多编码解码 → U+FFFD: 0
  - PATCH 代码补充：email_preprocessor 保留 _raw_body + pack 追加 diff
  - 排序修复：_patch_sort_key() 按 PATCH 编号排序
  - 游离邮件合并：_normalize_subject() 截断对齐 → 24→20 线程

## 关键技术细节

### 回复树构建逻辑
```
emails 列表 → 按 base_subject 分组 → 组内按 Re: 层数排序
→ 层数 0 = root, 层数 1 = root 的直接回复, 层数 2 = 回复的回复
→ 递归渲染: render_node(email) → 自身 + [render_node(child) for child in children]
```

### HTML 树缩进结构
```html
<div class="email root">...</div>
<div class="replies" style="margin-left:24px; border-left:2px solid #ddd">
  <details><summary>Author B — Re: subject</summary>
    <div class="email">...</div>
    <div class="replies" style="margin-left:24px; border-left:2px solid #ddd">
      <details><summary>Author C — Re: Re: subject</summary>
        <div class="email">...</div>
      </details>
    </div>
  </details>
</div>
```