"""
Generates missing_addresses_eiks.txt — EIKs that still need a full address.
Run this locally before pushing to GitHub to trigger fill_addresses.yml.

Skips EIKs already processed in previous runs (tracked via address_done.json).
"""
import csv, json, os, sys
sys.stdout.reconfigure(encoding='utf-8')

BASE      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_FILE  = os.path.join(BASE, "commercial_register_enriched.csv")
DONE_FILE = os.path.join(BASE, "address_done.json")
OUT_FILE  = os.path.join(os.path.dirname(os.path.abspath(__file__)), "missing_addresses_eiks.txt")

done = set()
if os.path.exists(DONE_FILE):
    with open(DONE_FILE) as f:
        done = set(json.load(f))

missing = []
with open(CSV_FILE, encoding="utf-8-sig") as f:
    for row in csv.DictReader(f):
        eik = (row.get("EIK") or "").strip()
        if not eik:
            continue
        if row.get("Address", "").strip():
            continue          # already has a full address
        if eik in done:
            continue          # processed before (API returned nothing)
        missing.append(eik)

with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write("\n".join(missing))

print(f"Written {len(missing):,} missing EIKs to {OUT_FILE}")
print(f"Already done (skipped): {len(done):,}")
