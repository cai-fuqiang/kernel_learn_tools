#!/usr/bin/env python3
"""快速测试 aliyun qwen API 连通性"""
import sys
sys.path.insert(0, ".")

from email_translator.translator import APITranslator

api = APITranslator(
    api_key="sk-ae19a6f47bf94122ae253ded970a6b9d",
    provider="aliyun",
    model="qwen-turbo",
    timeout=30,
)

print("=== 测试 aliyun qwen-turbo API ===")
text, err = api._call("请用一句话回答: 1+1等于几?")
if err:
    print(f"ERROR: {err}")
    sys.exit(1)
else:
    print(f"OK: {text}")
    print("\nAPI 连通性测试通过!")