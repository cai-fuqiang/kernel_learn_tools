# PLAN-003: 上下文质量优化 — 提高信息密度

## 问题诊断
当前 context.txt 99KB/112封邮件，但信息密度极低：
- 每封邮件被截断到 ~800 字符，review 讨论关键论点被砍
- 16封 PATCH 邮件含完整 diff，与 commit diff 区域重复
- 10封 tip-bot2 合入通知与 patch 正文高度重复，近零价值
- Re: 回复中大量 `>` 引用嵌套，重复已有内容
- 所有邮件一视同仁，无优先级区分

## TODO: 邮件预处理 — 去噪过滤
- [x] 识别并过滤 tip-bot2 自动通知（From含tip-bot2@linutronix.de）
- [x] 识别 PATCH 正文邮件（subject含[PATCH，body含diff --git），只保留 commit message 部分，去掉 diff
- [x] 去除 `>` 引用嵌套（保留最外层回复正文，去掉引用的已有内容）
- [x] 去除邮件签名（-- 之后的内容）

## TODO: 邮件分级 — 按价值分配预算
- [x] 高价值：Re: 讨论邮件（包含 review 意见、争议、NAK/Acked-by）
- [x] 中价值：Cover letter (PATCH 00/N)、Tested-by/性能数据邮件
- [x] 低价值：PATCH 正文（仅保留 commit message）
- [x] 零价值：tip-bot2 通知 → 直接丢弃

## TODO: 截断策略重构
- [x] 高价值邮件：最多 5000 字符（完整保留讨论）
- [x] 中价值邮件：最多 2000 字符
- [x] 低价值邮件：最多 500 字符（只保留 subject + commit message 摘要）
- [x] 全部预算用完后才开始丢弃邮件（从低价值开始丢）

## TODO: 引用去重
- [x] 解析 `>` / `>>` 引用层级
- [x] 仅保留最终回复者的新增内容
- [x] 保留被引用的关键行（紧邻回复内容上方的1-3行上下文引用）

## TODO: 验证
- [x] 用 d07f09a1f99c 重新生成，对比前后信息量
- [x] 确认高价值讨论（如 Benjamin Segall 的 perverse incentive 论点）完整保留

## 验证结果
- 旧版：101KB，94/112封，88处截断，所有邮件 ~800字符上限
- 新版：100KB，100/112封（丢弃12封tip-bot2），24处截断
- HIGH:36封(5000字符) MEDIUM:37封(2000字符) LOW:27封(500字符)
- 引用去重：99处长引用块被折叠
- SPEC CPU回归数据完整保留，Benjamin Segall讨论完整保留