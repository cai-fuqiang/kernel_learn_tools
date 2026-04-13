#!/usr/bin/env python3
"""测试改造后的 LoreClient 搜索"""
import sys, logging
sys.path.insert(0, ".")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-5s %(message)s", datefmt="%H:%M:%S")

from email_translator.lore_client import LoreClient

client = LoreClient(timeout=30, delay=1.0, max_retries=2)

# 测试1: EEVDF (已知有结果)
print("\n=== 测试1: EEVDF 2024 ===")
results = client.search_emails(topic="EEVDF", list_name="all", max_emails=5,
                                date_from="2024-01-01", date_to="2024-06-30")
for r in results[:5]:
    print(f"  [{r.get('date','')}] {r.get('subject','')[:80]}")
    print(f"    From: {r.get('from','')[:60]}")
    print(f"    MsgID: {r.get('message_id','')[:60]}")
print(f"共 {len(results)} 封\n")

# 测试2: sched fair
print("=== 测试2: sched/fair sleep 2024 ===")
results2 = client.search_emails(topic="sched fair sleep", list_name="all", max_emails=5,
                                 date_from="2024-01-01", date_to="2024-12-31")
for r in results2[:5]:
    print(f"  [{r.get('date','')}] {r.get('subject','')[:80]}")
    print(f"    From: {r.get('from','')[:60]}")
print(f"共 {len(results2)} 封")