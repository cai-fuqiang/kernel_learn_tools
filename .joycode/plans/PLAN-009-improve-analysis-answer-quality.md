# PLAN-009: 提升 context_full 分析报告生成质量

## 任务概述
基于对 `d07f09a1f99c_context_full.txt` 模板输出的自评，识别出当前 LLM 生成的分析报告在三个维度上存在不足：patchset 全局视角、maintainer 讨论深度、关联 commit 索引。需要优化提示词（prompt）和模板结构，使生成结果更好地服务于「理解 patchset 整体作用」「把握 maintainer 关注点」「追溯历史与未来 commit」三个核心目标。

---

## TODO: 1. 模板结构增强

- [ ] 在「背景与问题」之前新增「Patchset 全景」小节，要求列出本 commit 在 patchset 中的位置及前后 patch 的演进关系链
- [ ] 在「后续关注」中新增「相关 Commit 索引」子节，要求从邮件正文中提取所有 commit hash/链接并标注方向（过去依赖 / 未来修复）
- [ ] 在「邮件讨论精华」中将讨论点上限从 3-5 个扩展为 5-7 个，并增加「接口设计争论」「理论 vs 实践」等分类标签

## TODO: 2. Prompt 优化 — patchset 全局视角

- [ ] 在分析任务清单的提示词中增加显式指令：「从 PATCH 摘要列表中提取 patchset 演进链，用箭头图说明本 commit 的上下游依赖」
- [ ] 增加要求：「如果模板中包含 [PATCH xx/yy] 摘要，必须在分析开头给出 patchset 总览表（编号、标题、核心改动一句话）」

## TODO: 3. Prompt 优化 — maintainer 讨论深度

- [ ] 增加提示词要求对以下类型讨论做专项提取：
  - 性能数据对比（要求结构化表格）
  - 理论分析 vs 工程实现的 gap（如 EEVDF 论文 Theorem 与离散系统的差异）
  - 接口/API 设计争论（如 latency-nice vs sched_runtime）
  - 算法正确性 bug 讨论（如 pick_eevdf 遍历遗漏）
  - 激励扭曲问题（如 spin vs sleep 的 perverse incentive）
- [ ] 增加要求：「引用邮件原文时必须包含发件人和日期，便于溯源」

## TODO: 4. Prompt 优化 — 关联 commit 追溯

- [ ] 增Fixes: 标签、Lore 链接」加显式指令：「扫描所有邮件正文，提取出现的 commit hash（12位+缩写或完整40位）、
- [ ] 要求分类输出：`历史依赖`（本 patch 基于或修复的）、`后续修复`（邮件中讨论到的已知问题的修复）、`待完成`（TODO / 讨论中提议但未实现的）
- [ ] 增加对 sysctl 残留清理、feature flag 变更等「非代码类 TODO」的提取

## TODO: 5. 性能数据结构化

- [ ] 在模板中增加「性能数据汇总」可选节，要求以 Markdown 表格输出
- [ ] 提示词中增加：「如果邮件中包含 benchmark 数据，必须提取为表格，列明：测试人、测试环境、基线、对比项、关键指标、结论」

## TODO: 6. 验证与迭代

- [ ] 用 `d07f09a1f99c_context_full.txt` 作为测试输入，对比改进前后的输出质量
- [ ] 检查改进后的输出是否仍控制在 2000-4000 字范围内（避免信息过载）
- [ ] 确认 patchset 演进链、commit 索引、性能表格三项是否都出现在输出中