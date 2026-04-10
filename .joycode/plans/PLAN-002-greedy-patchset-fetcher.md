# PLAN-002: 贪心预取 — 规则驱动的 patchset 完整抓取

## 任务概述

以 git commit 为入口，用纯规则脚本（无 AI 循环）尽可能多地把相关原材料抓齐：
完整 Lore 邮件线程 + patchset 同系列所有 patch + 同期相关 commit，
最终打包成一份上下文文件，一次性喂给 OpenClaw。

## TODO: Lore 线程完整抓取

- [x] 新建 `email_translator/lore_thread_fetcher.py`
- [x] 给定 Lore URL（如 `https://lore.kernel.org/r/<msgid>`），获取原始 mbox
- [x] 解析 mbox：提取所有邮件（包含回复）组成线程列表
- [x] 支持从 commit message 的 `Link:` 行自动提取 Lore URL
- [x] 修复 Anubis PoW 处理：支持 challenge=null 降级
- [x] 修复 datetime naive/aware 排序 bug
- [x] 添加多策略降级：mbox.gz → raw 单封 → 本地 JSON 缓存

## TODO: patchset 识别与批量抓取

- [x] 在 `commit_analyzer.py` 中解析 subject 的 `[PATCH N/M]` 格式
- [x] 通过 Lore 线程 URL 的父线程（cover letter）找到完整 patchset
- [x] 用 git log 按作者+日期窗口+文件范围过滤同系列 commit，补全未合入的部分

## TODO: 更新 pack_for_openclaw.py

- [x] 集成 `lore_thread_fetcher`：自动拉取完整邮件线程
- [x] 集成 patchset 抓取：同系列所有 patch 的 diff 都打包进去
- [x] 上下文中加入 patchset 全览表（编号 / subject / 合入状态）
- [x] 输出文件大小超过阈值时自动截断低优先级内容
- [x] 添加 `--email-json` 参数：Lore 无法访问时的降级方案
- [x] 添加本地 data/emails/ 自动缓存搜索

## TODO: 验证测试

- [x] 用 `d07f09a1f99c`（sched/fair: Propagate enqueue flags）端到端测试
- [x] 验证 Lore 线程能抓到完整讨论（含 review 回复）— 112 封邮件
- [x] 验证 patchset 能识别出同系列所有 patch
- [x] 检查最终 context.txt 内容完整性 — 97KB