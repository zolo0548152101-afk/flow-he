"""יוצר חבילת עדכון לתיקיית updates/ של Flow-HE: חלקים של עד 25MB + manifest.json (נכתב אחרון).
שימוש: python tools/make_update.py <Flow-HE.exe> <version> <out_dir>"""
import hashlib
import json
import os
import sys

exe, version, out = sys.argv[1], sys.argv[2], sys.argv[3]
os.makedirs(out, exist_ok=True)
data = open(exe, "rb").read()
size = 25 * 1024 * 1024
parts = []
for i in range(0, len(data), size):
    name = f"Flow-HE.{version}.part{len(parts):02d}"
    with open(os.path.join(out, name), "wb") as f:
        f.write(data[i:i + size])
    parts.append(name)
manifest = {"version": version, "sha256": hashlib.sha256(data).hexdigest(), "parts": parts}
with open(os.path.join(out, "manifest.json"), "w", encoding="utf-8") as f:
    json.dump(manifest, f, indent=2)
print(json.dumps(manifest))
