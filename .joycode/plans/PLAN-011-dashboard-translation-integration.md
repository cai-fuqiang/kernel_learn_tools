# PLAN-011: 知识库线程 → 翻译 HTML 联通

## 概述
复用现有 translate_context.py 翻译工具链，在 batch_process.py 中新增
`--translate` 批量翻译模式：从知识库取线程邮件 → 构造 context → 调用翻译 →
生成翻译 HTML 文件 → 路径写回 threads 表。kb_web.py 前端直接链接翻译产物。

## 核心思路: 预翻译 + 存储路径

**不做实时翻译**，而是:
1. `batch_process.py --translate` 批量遍历线程
2. 每个线程: 从 knowledge.db 取邮件 → 组装成 translate_context 输入格式 → 翻译 → 生成 HTML
3. 翻译 HTML 存到 `data/output/thread_<id>_translated.html`
4. 路径写回 `threads.translated_html_path` 字段
5. kb_web.py / Dashboard 展示时，有翻译的线程显示「查看翻译」按钮，直接打开 HTML

## TODO: 实现步骤

### 1. DB 扩展 (knowledge_db.py)
- [ ] threads 表新增 `translated_html_path TEXT DEFAULT ''` 字段 (ALTER TABLE)
- [ ] 新增 `update_thread_translated_path(thread_id, path)` 方法

### 2. 批量翻译入口 (batch_process.py)
- [ ] 新增 `--translate` 模式
- [ ] `run_translate(args, db)` 函数:
  - 取未翻译线程 (translated_html_path 为空)
  - 对每个线程: get_thread_emails → 组装邮件列表
  - 构造 translate_context.py 需要的输入格式 (emails list + 空 commit)
  - 调用翻译 (复用 translator + translation_cache)
  - 调用 generate_html() 生成 HTML 文件
  - 路径写回 DB
- [ ] 支持 `--backend google/api` `--workers N` 等翻译参数
- [ ] 支持 `--thread-id <id>` 指定单线程翻译

### 3. 翻译桥接 (核心逻辑)
- [ ] 将 knowledge.db 邮件格式转换为 translate_context 邮件格式
  - DB 格式: {message_id, subject, from_name, date, body, in_reply_to, ...}
  - translate 格式: {from, subject, date, body, message_id, in_reply_to, tag, ...}
- [ ] 复用 translate_context.py 的:
  - `_build_thread_tree()` 线程树构建
  - `_html_email_node()` HTML 渲染
  - `_HTML_TEMPLATE` 页面模板
  - `generate_html()` 完整生成
- [ ] 翻译用 CachedTranslator (命中缓存零成本)

### 4. kb_web.py 前端联动
- [ ] `/api/threads` 返回 translated_html_path 字段
- [ ] 新增 `/api/thread/<id>/html` — 返回翻译 HTML 文件内容 (静态文件服务)
- [ ] Threads 列表: 有翻译路径的行显示「查看翻译」按钮
- [ ] 点击「查看翻译」→ 新窗口打开翻译 HTML

### 5. Dashboard 联动 (build_dashboard.py)
- [ ] 扫描时检测 thread_*_translated.html 文件
- [ ] 卡片关联翻译产物

## 关键细节

### 邮件格式转换 (DB → translate_context)
```python
def db_email_to_translate_format(db_email: dict) -> dict:
    return {
        "from": db_email.get("from_name", "") or db_email.get("from_email", ""),
        "subject": db_email.get("subject", ""),
        "date": db_email.get("date", ""),
        "body": db_email.get("body", ""),
        "message_id": db_email.get("message_id", ""),
        "in_reply_to": db_email.get("in_reply_to", ""),
        "tag": _infer_priority_tag(db_email),  # 从 priority 字段推断
    }
```

### 翻译文件命名
`data/output/thread_{thread_id_safe}_translated.html`
(thread_id 中的特殊字符替换为 `_`)

### 增量翻译
- 只翻译 `translated_html_path` 为空的线程
- `--force` 参数可强制重新翻译
- 翻译缓存 (translation_cache.db) 保证已翻译的邮件正文不重复调用