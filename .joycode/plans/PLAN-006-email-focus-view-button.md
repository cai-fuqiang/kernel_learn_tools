# PLAN-006: 邮件聚焦视图（Focus View）按钮

## 任务摘要
邮件线程树中深层嵌套的回复因 `.replies` 的 `margin-left` 持续缩进，导致内容区域（x轴）越来越窄。需要增加"聚焦"按钮，点击后将该邮件及其子线程提升为顶层视图（移除所有父级缩进），并提供"返回"按钮恢复原始视图。

## TODO: 实现聚焦视图功能

### 1. CSS 样式修改（translate_context.py _HTML_TEMPLATE）
- [ ] 添加 `.focus-btn` 按钮样式（小图标按钮，放在邮件卡片 header 中）
- [ ] 添加 `.back-btn` 返回按钮样式（固定在页面顶部或聚焦区域顶部）
- [ ] 添加 `.focused-view` 容器样式（全宽，移除缩进）
- [ ] 添加 `.hidden-by-focus` 样式（隐藏非聚焦内容）

### 2. JavaScript 交互逻辑（_HTML_TEMPLATE 中添加 script）
- [ ] 实现 `focusEmail(cardId)` 函数：隐藏其他内容，将目标邮件卡片及子线程提升为顶层
- [ ] 实现 `unfocusEmail()` 函数：恢复原始视图
- [ ] 聚焦时自动展开该邮件的所有子回复
- [ ] 聚焦时在顶部显示面包屑导航（显示当前聚焦邮件的作者/主题 + 返回按钮）

### 3. HTML 结构修改（_html_email_node 函数）
- [ ] 为每个 `.email-card` 添加唯一 id（基于邮件索引）
- [ ] 在非根邮件的 `.email-header` 中添加聚焦按钮 `🔍`
- [ ] 包裹每个邮件节点为可定位的容器（含子回复）

### 4. 验证
- [ ] 用现有 example output HTML 测试聚焦/返回功能
- [ ] 确认深层嵌套邮件聚焦后宽度恢复正常
- [ ] 确认返回后视图完整恢复