import re
text = open("data/output/d07f09a1f99c_context_full.txt").read()
subs = re.findall(r"^## \[[^\]]+\]\s*(.+)$", text, re.MULTILINE)
base = {}
for s in subs:
    b = re.sub(r"^(Re:\s*)+", "", s).strip()
    base.setdefault(b, []).append(s)
print(f"total: {len(subs)}, threads: {len(base)}")
for b, rs in sorted(base.items(), key=lambda x: -len(x[1]))[:8]:
    d = max(s.count("Re:") for s in rs)
    print(f"  [{len(rs)}封 d{d}] {b[:65]}")