"""
Generates missing_eiks.txt — the list of EIKs that still need a city.
Run this locally before pushing to GitHub.
"""
import csv, json, os, sys
sys.stdout.reconfigure(encoding='utf-8')

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_FILE  = os.path.join(BASE, "commercial_register_final.csv")
DONE_FILE = os.path.join(BASE, "epzeu_done.json")
OUT_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "missing_eiks.txt")

done = set()
if os.path.exists(DONE_FILE):
    with open(DONE_FILE) as f:
        done = set(json.load(f))

missing = []
with open(CSV_FILE, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        if not row.get("Registered Address", "").strip() and row["EIK"] not in done:
            missing.append(row["EIK"])

with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write("\n".join(missing))

print(f"Written {len(missing):,} missing EIKs to {OUT_FILE}")
print(f"Already done (skipped): {len(done):,}")
