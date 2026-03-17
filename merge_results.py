"""
Merge GitHub Actions city results back into commercial_register_final.csv.
Run locally after downloading all result artifacts from GitHub.

Usage: python merge_results.py <results_folder>
  results_folder: folder containing result_0.json, result_1.json, ...
                  (download & extract all artifacts into one folder)
"""
import csv, json, os, sys, glob
sys.stdout.reconfigure(encoding='utf-8')

BASE     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_FILE = os.path.join(BASE, "commercial_register_final.csv")
DONE_FILE = os.path.join(BASE, "epzeu_done.json")

results_folder = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))

# Load all result JSON files
city_map = {}
result_files = glob.glob(os.path.join(results_folder, "results_*.json"))
result_files += glob.glob(os.path.join(results_folder, "**", "results_*.json"), recursive=True)
result_files = list(set(result_files))

print(f"Found {len(result_files)} result file(s)")
for rfile in sorted(result_files):
    with open(rfile, encoding="utf-8") as f:
        batch = json.load(f)
    city_map.update(batch)
    print(f"  {os.path.basename(rfile)}: {len(batch):,} cities")

print(f"\nTotal cities to apply: {len(city_map):,}")

# Load CSV
companies, fieldnames = {}, None
with open(CSV_FILE, encoding="utf-8-sig") as f:
    reader = csv.DictReader(f)
    fieldnames = reader.fieldnames
    for row in reader:
        companies[row["EIK"]] = row

# Apply cities
applied = 0
for eik, city in city_map.items():
    if eik in companies and not companies[eik].get("Registered Address", "").strip():
        companies[eik]["Registered Address"] = city
        applied += 1

# Save
tmp = CSV_FILE + ".tmp"
with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    for eik in sorted(companies):
        writer.writerow(companies[eik])
os.replace(tmp, CSV_FILE)

# Update done file
done = set()
if os.path.exists(DONE_FILE):
    with open(DONE_FILE) as f:
        done = set(json.load(f))
done.update(city_map.keys())
with open(DONE_FILE, "w") as f:
    json.dump(list(done), f)

total = len(companies)
with_city = sum(1 for r in companies.values() if r.get("Registered Address", "").strip())
print(f"\nApplied {applied:,} new cities")
print(f"Total with city: {with_city:,} / {total:,} ({with_city/total*100:.1f}%)")
print(f"Still missing:   {total - with_city:,}")
print(f"Saved → {CSV_FILE}")
