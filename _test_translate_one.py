#!/usr/bin/env python3
"""最小化测试：翻译一段文本，定位卡在哪里"""
import sys, time
sys.path.insert(0, ".")

print("1. 创建翻译器...", flush=True)
from email_translator.translator import create_translator
translator = create_translator("google")

print("2. 翻译一段简单文本...", flush=True)
t0 = time.time()
result = translator.translate_email({
    "subject": "",
    "body": "The scheduler needs to handle latency constraints properly."
})
print(f"   耗时: {time.time()-t0:.1f}s", flush=True)
print(f"   结果: {result.get('body_cn', '')[:100]}", flush=True)

print("3. 测试第二段...", flush=True)
t0 = time.time()
result = translator.translate_email({
    "subject": "",
    "body": "Add an onmax hist trigger action which is invoked whenever an event exceeds the current maximum."
})
print(f"   耗时: {time.time()-t0:.1f}s", flush=True)
print(f"   结果: {result.get('body_cn', '')[:100]}", flush=True)

print("4. 完成!", flush=True)