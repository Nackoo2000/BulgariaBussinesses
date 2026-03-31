"""
Merge BRRA address results into commercial_register_enriched.csv.
Run locally after downloading artifacts from GitHub Actions.

Writes formatted full address to the Address column:
  "гр. Бургас, ул. Славянска 14, п.к. 8000"

Only fills Address for rows where it is currently empty.

If success rate < 95%, notifies with error and aborts — do not apply.

Usage: python merge_addresses.py <results_folder>
  results_folder: folder containing address_results_*.json or all_addresses.json
"""
import csv, json, os, sys, glob, subprocess
sys.stdout.reconfigure(encoding='utf-8')

SUCCESS_THRESHOLD = 0.95   # abort if fewer than 95% of queried EIKs returned a street

BASE      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_FILE  = os.path.join(BASE, "commercial_register_enriched.csv")
DONE_FILE = os.path.join(BASE, "address_done.json")

results_folder = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.abspath(__file__))

def notify(title, message):
    ps = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(8000, '{title}', '{message}', [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 9
$n.Dispose()
"""
    subprocess.Popen(["powershell", "-WindowStyle", "Hidden", "-Command", ps],
                     creationflags=0x08000000)

def format_address(settlement, street, post_code):
    """Formats: 'гр. Бургас, ул. Славянска 14, п.к. 8000'"""
    if not street:
        return ""
    parts = []
    if settlement:
        parts.append(settlement)
    parts.append(street)
    if post_code:
        parts.append(f"п.к. {post_code}")
    return ", ".join(parts)

# ── Load all result JSON files ──────────────────────────────────────────────
addr_map = {}

result_files  = glob.glob(os.path.join(results_folder, "address_results_*.json"))
result_files += glob.glob(os.path.join(results_folder, "**", "address_results_*.json"), recursive=True)
result_files += glob.glob(os.path.join(results_folder, "all_addresses.json"))
result_files += glob.glob(os.path.join(results_folder, "**", "all_addresses.json"), recursive=True)
result_files  = list(set(result_files))

print(f"Found {len(result_files)} result file(s)")
for rfile in sorted(result_files):
    with open(rfile, encoding="utf-8") as f:
        batch = json.load(f)
    addr_map.update(batch)
    streets = sum(1 for v in batch.values() if v.get("street"))
    print(f"  {os.path.basename(rfile)}: {len(batch):,} EIKs, {streets:,} with street")

total_queried     = len(addr_map)
total_with_street = sum(1 for v in addr_map.values() if v.get("street"))
api_responded     = sum(1 for v in addr_map.values() if v.get("street") or v.get("settlement"))
success_rate      = api_responded / total_queried if total_queried > 0 else 0

print(f"\nTotal queried:   {total_queried:,}")
print(f"API responded:   {api_responded:,} ({success_rate*100:.1f}%)")
print(f"With street:     {total_with_street:,} ({total_with_street/total_queried*100:.1f}%) — rest have no street in registry")

# ── Abort if API success rate too low ─────────────────────────────────────────
if success_rate < SUCCESS_THRESHOLD:
    msg = (f"ABORTED — only {success_rate*100:.1f}% of EIKs returned a street "
           f"(threshold {SUCCESS_THRESHOLD*100:.0f}%). Check for API errors.")
    print(f"\n{msg}")
    notify("BRRA Address Merge — ERROR", msg)
    sys.exit(1)

# ── Load CSV ──────────────────────────────────────────────────────────────────
rows       = []
fieldnames = None
with open(CSV_FILE, encoding="utf-8-sig", newline="") as f:
    reader     = csv.DictReader(f)
    fieldnames = list(reader.fieldnames)
    for row in reader:
        rows.append(row)

row_map = {r["EIK"]: r for r in rows}

# ── Apply addresses ───────────────────────────────────────────────────────────
applied = 0
for eik, data in addr_map.items():
    if eik not in row_map:
        continue
    row = row_map[eik]
    if row.get("Address", "").strip():
        continue   # already filled — never overwrite
    formatted = format_address(
        data.get("settlement", ""),
        data.get("street", ""),
        data.get("post_code", ""),
    )
    if formatted:
        row["Address"] = formatted
        applied += 1

# ── Save CSV ──────────────────────────────────────────────────────────────────
tmp = CSV_FILE + ".tmp"
with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(rows)
os.replace(tmp, CSV_FILE)

# ── Update done file ──────────────────────────────────────────────────────────
done = set()
if os.path.exists(DONE_FILE):
    with open(DONE_FILE) as f:
        done = set(json.load(f))
done.update(addr_map.keys())
with open(DONE_FILE, "w") as f:
    json.dump(list(done), f)

# ── Summary & notification ────────────────────────────────────────────────────
total      = len(rows)
with_addr  = sum(1 for r in rows if r.get("Address", "").strip())
overall_pct = with_addr / total * 100

# ETA: remaining EIKs / 3600 per batch × 35 min per batch (30min work + 5min turnaround)
total_done_so_far = len(done)
total_need_addr   = sum(1 for r in rows if not r.get("Address", "").strip())
remaining_eiks    = total_need_addr  # after this merge
remaining_batches = -(-remaining_eiks // 3600)  # ceiling division
eta_hours         = remaining_batches * 35 / 60

print(f"\nApplied {applied:,} new addresses")
print(f"Overall Address coverage: {with_addr:,} / {total:,} ({overall_pct:.1f}%)")
print(f"Remaining without address: {remaining_eiks:,} (~{remaining_batches} batches, ~{eta_hours:.1f}h)")
print(f"Saved → {CSV_FILE}")

summary = (f"Batch OK {success_rate*100:.0f}% | "
           f"Scraped: {total_done_so_far:,}/{total:,} | "
           f"Address: {with_addr:,}/{total:,} ({overall_pct:.1f}%) | "
           f"Left: {remaining_eiks:,} ~{eta_hours:.1f}h")
notify("BRRA Address Merge", summary)
