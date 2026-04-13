#!/usr/bin/env python3
"""测试 lore 搜索用 x=m (mbox) 格式"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")

from email_translator.lore_client import LoreClient

client = LoreClient(timeout=30, delay=1.0, max_retries=2)

# 用 x=m 请求 mbox 格式
url = "https://lore.kernel.org/all/?q=EEVDF&x=m&o=0"
print(f"请求: {url}\n")
raw = client._get(url, accept="text/plain, application/mbox")

if raw:
    print(f"返回长度: {len(raw)} chars")
    print(f"前 1500 字符:")
    print(raw[:1500])
    print(f"\n--- 检查 mbox From 行 ---")
    import re
    # 标准 mbox From 行
    froms = re.findall(r'^From \S+ \w{3} \w{3} +\d+ \d+:\d+:\d+ \d{4}$', raw, re.MULTILINE)
    print(f"标准 mbox From 行: {len(froms)}")
    # 更宽松的 From 行
    froms2 = re.findall(r'^From .+$', raw, re.MULTILINE)
    print(f"宽松 From 行: {len(froms2)}")
    for f in froms2[:5]:
        print(f"  {f[:100]}")
else:
    print("返回 None!")