#!/usr/bin/env python3
"""检查 Lore 搜索返回的实际内容格式"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")

from email_translator.lore_client import LoreClient

client = LoreClient(timeout=30, delay=1.0, max_retries=2)

# 直接调用 _get 看返回内容
url = "https://lore.kernel.org/all/?q=EEVDF&x=A&o=0"
print(f"请求: {url}\n")
raw = client._get(url, accept="text/plain, application/mbox")

if raw:
    print(f"返回长度: {len(raw)} chars")
    print(f"前 2000 字符:")
    print(raw[:2000])
    print(f"\n--- 检查 mbox From 行 ---")
    import re
    froms = re.findall(r'^From \S+ \w{3} \w{3} +\d+ \d+:\d+:\d+ \d{4}$', raw, re.MULTILINE)
    print(f"找到 {len(froms)} 个 From 行")
    for f in froms[:5]:
        print(f"  {f}")
else:
    print("返回 None!")