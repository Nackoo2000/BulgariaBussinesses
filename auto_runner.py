"""
Automated pipeline runner — EPZEU via GitHub Actions.

Rules:
  - Batch runs for 15 min (90 EIKs × 20 workers = 1,800 EIKs per batch)
  - After each batch:
      * 0 cities found        → STOP and notify (something is broken)
      * success rate > 90%    → auto-start next batch
      * success rate <= 90%   → STOP and notify (investigate before continuing)
"""
import sys, os, json, time, csv, glob, zipfile, io, subprocess, requests, math

sys.stdout.reconfigure(encoding='utf-8', write_through=True)

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gh_token.txt")
with open(TOKEN_FILE) as f:
    GITHUB_TOKEN = f.read().strip()

REPO            = "Nackoo2000/BulgariaBussinesses"
WORKFLOW        = "fill_cities.yml"
BRANCH          = "main"

BASE_DIR        = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GH_DIR          = os.path.dirname(os.path.abspath(__file__))
CSV_FILE        = os.path.join(BASE_DIR, "commercial_register_final.csv")
DONE_FILE       = os.path.join(BASE_DIR, "epzeu_done.json")
MISSING_FILE    = os.path.join(GH_DIR, "missing_eiks.txt")
STATE_FILE      = os.path.join(GH_DIR, "runner_state.json")

POLL_INTERVAL   = 60       # seconds between status checks
MAX_WAIT_HOURS  = 1        # max wait per batch before timeout
TOTAL_BATCHES   = 300      # safe upper bound
WORKERS         = 20
MAX_PER_WORKER  = 90       # must match worker.py
EIKS_PER_BATCH  = WORKERS * MAX_PER_WORKER   # 1,800
SUCCESS_THRESH  = 0.90     # auto-continue only if >= 90% of EIKs got a city

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ── Windows notification ────────────────────────────────────────────────────

def notify(title, message):
    t = title.replace("'", "`'")
    m = message.replace("'", "`'")
    ps = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(8000, '{t}', '{m}', [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 9
$n.Dispose()
"""
    subprocess.Popen(
        ["powershell", "-WindowStyle", "Hidden", "-Command", ps],
        creationflags=0x08000000,
    )

# ── State ──────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"next_batch": 0, "last_run_id": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── GitHub API ─────────────────────────────────────────────────────────────

def api_get(path):
    r = requests.get(f"{API}{path}", headers=HEADERS, timeout=30)
    r.raise_for_status()
    return r.json()

def trigger_workflow(batch_number):
    r = requests.post(
        f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
        headers=HEADERS,
        json={"ref": BRANCH, "inputs": {"batch_number": str(batch_number)}},
        timeout=30,
    )
    return r.status_code == 204

def get_latest_run_id():
    data = api_get(f"/repos/{REPO}/actions/runs?per_page=1")
    runs = data.get("workflow_runs", [])
    return runs[0]["id"] if runs else None

def wait_for_run(run_id):
    deadline = time.time() + MAX_WAIT_HOURS * 3600
    while time.time() < deadline:
        data       = api_get(f"/repos/{REPO}/actions/runs/{run_id}")
        status     = data["status"]
        conclusion = data.get("conclusion")
        print(f"  Run {run_id}: {status} / {conclusion}", flush=True)
        if status == "completed":
            return conclusion
        time.sleep(POLL_INTERVAL)
    return "timeout"

def download_artifact(run_id, batch_number):
    data      = api_get(f"/repos/{REPO}/actions/runs/{run_id}/artifacts")
    artifacts = data.get("artifacts", [])

    target = f"all_cities_batch_{batch_number}"
    art    = next((a for a in artifacts if a["name"] == target), None)
    if art:
        return fetch_zip(art["archive_download_url"])

    # Fall back to individual worker artifacts
    print("  No merged artifact — collecting worker artifacts...", flush=True)
    city_map = {}
    for a in artifacts:
        if a["name"].startswith("results-worker-"):
            city_map.update(fetch_zip(a["archive_download_url"]))
    return city_map

def fetch_zip(url):
    r = requests.get(url, headers=HEADERS, timeout=120)
    r.raise_for_status()
    city_map = {}
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        for name in zf.namelist():
            if name.endswith(".json"):
                with zf.open(name) as f:
                    try:
                        data = json.load(f)
                        if isinstance(data, dict):
                            city_map.update(data)
                    except Exception:
                        pass
    return city_map

# ── CSV helpers ────────────────────────────────────────────────────────────

def load_csv():
    companies, fieldnames = {}, None
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        reader     = csv.DictReader(f)
        fieldnames = reader.fieldnames
        for row in reader:
            companies[row["EIK"]] = row
    return companies, fieldnames

def save_csv(companies, fieldnames):
    tmp = CSV_FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for eik in sorted(companies):
            writer.writerow(companies[eik])
    os.replace(tmp, CSV_FILE)

def apply_cities(companies, fieldnames, city_map):
    """
    city_map values can be:
      - str  (old format): just a city name
      - dict (new format): {"city": ..., "email": ...}
    Adds Email and Email Verified columns to CSV if not already present.
    """
    # Ensure Email columns exist
    for col in ("Email", "Email Verified", "Email Scraped"):
        if col not in fieldnames:
            fieldnames.append(col)
    for row in companies.values():
        row.setdefault("Email", "")
        row.setdefault("Email Verified", "")
        row.setdefault("Email Scraped", "N")

    applied = 0
    for eik, value in city_map.items():
        if eik not in companies:
            continue
        row = companies[eik]

        if isinstance(value, dict):
            city  = value.get("city",  "") or ""
            email = value.get("email", "") or ""
        else:
            city  = value or ""
            email = ""

        if city and not row.get("Registered Address", "").strip():
            row["Registered Address"] = city
            applied += 1
        if email and not row.get("Email", "").strip():
            row["Email"] = email
        row["Email Scraped"] = "Y"  # mark as scraped regardless of result

    return applied

def update_done(city_map):
    done = set()
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            done = set(json.load(f))
    done.update(city_map.keys())
    with open(DONE_FILE, "w") as f:
        json.dump(list(done), f)

def regenerate_missing():
    done = set()
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            done = set(json.load(f))
    missing = []
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            if not row.get("Registered Address", "").strip() and row["EIK"] not in done:
                missing.append(row["EIK"])
    with open(MISSING_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(missing))
    return len(missing)

def git_push():
    for cmd in [
        ["git", "-C", GH_DIR, "add", "missing_eiks.txt", "runner_state.json"],
        ["git", "-C", GH_DIR, "commit", "-m", "Update after batch merge"],
        ["git", "-C", GH_DIR, "push"],
    ]:
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0 and "nothing to commit" not in r.stdout:
            print(f"  git: {r.stderr.strip()[:150]}", flush=True)

def eta_string(still_missing):
    batches_left = math.ceil(still_missing / EIKS_PER_BATCH)
    mins         = batches_left * 20   # ~20 min per batch (15 work + 5 overhead)
    if mins < 60:
        return f"{mins}min"
    return f"{mins/60:.1f}h"

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    state = load_state()
    print("=" * 60, flush=True)
    print("AUTO RUNNER — 15-min EPZEU batches", flush=True)
    print(f"Starting at batch {state['next_batch']}", flush=True)
    print("Stop rules: 0 cities found OR success rate < 90%", flush=True)
    print("=" * 60, flush=True)

    while state["next_batch"] < TOTAL_BATCHES:
        batch = state["next_batch"]
        print(f"\n{'='*50}", flush=True)
        print(f"BATCH {batch}", flush=True)

        companies, fieldnames = load_csv()
        total         = len(companies)
        with_city     = sum(1 for r in companies.values() if r.get("Registered Address", "").strip())
        missing_count = total - with_city
        print(f"Before: {with_city:,} with city | {missing_count:,} missing", flush=True)

        if missing_count == 0:
            notify("Bulgaria Data — COMPLETE", "All companies have a city!")
            print("All done!", flush=True)
            break

        # Trigger
        run_id = state.get("last_run_id")
        if not run_id:
            print(f"Triggering batch {batch}...", flush=True)
            if not trigger_workflow(batch):
                time.sleep(60)
                if not trigger_workflow(batch):
                    notify("Bulgaria Data — ERROR", "Could not trigger workflow.")
                    return
            time.sleep(15)
            run_id = get_latest_run_id()
            print(f"Run: {run_id} → https://github.com/{REPO}/actions/runs/{run_id}", flush=True)
            state["last_run_id"] = run_id
            save_state(state)

        conclusion = wait_for_run(run_id)
        print(f"Conclusion: {conclusion}", flush=True)

        if conclusion == "cancelled":
            print("Run was cancelled — stopping.", flush=True)
            notify("Bulgaria Data — STOPPED", f"Batch {batch} was cancelled. Start again when ready.")
            return

        if conclusion == "timeout":
            notify("Bulgaria Data — TIMEOUT", f"Batch {batch} timed out. Check GitHub.")
            state["next_batch"] = batch + 1
            state["last_run_id"] = None
            save_state(state)
            return

        # Download & merge
        try:
            city_map = download_artifact(run_id, batch)
        except Exception as e:
            print(f"ERROR downloading artifacts: {e}", flush=True)
            notify("Bulgaria Data — ERROR", f"Batch {batch}: could not download artifact.")
            return

        cities_found  = len(city_map)
        eiks_attempted = min(EIKS_PER_BATCH, missing_count)
        success_rate  = cities_found / eiks_attempted if eiks_attempted > 0 else 0

        print(f"Cities found: {cities_found:,} / {eiks_attempted:,} ({success_rate*100:.1f}%)", flush=True)

        if cities_found == 0:
            notify(
                "Bulgaria Data — STOPPED",
                f"Batch {batch}: 0 cities found. Something is broken — check workers."
            )
            print("STOPPING: 0 cities found.", flush=True)
            return

        # Apply to CSV
        companies, fieldnames = load_csv()
        applied = apply_cities(companies, fieldnames, city_map)
        save_csv(companies, fieldnames)
        update_done(city_map)
        still_missing = regenerate_missing()

        emails_in_batch = sum(1 for v in city_map.values() if isinstance(v, dict) and v.get("email"))
        print(f"Applied {applied:,} cities, {emails_in_batch:,} emails. Still missing: {still_missing:,}", flush=True)

        state["next_batch"] = batch + 1
        state["last_run_id"] = None
        save_state(state)

        email_rate = emails_in_batch / eiks_attempted if eiks_attempted > 0 else 0
        eta = eta_string(still_missing)

        if success_rate >= SUCCESS_THRESH:
            notify(
                f"Batch {batch} ✓ — Bulgaria Data",
                f"Cities: {cities_found:,} ({success_rate*100:.0f}%) | Emails: {emails_in_batch:,} ({email_rate*100:.0f}%) | Missing: {still_missing:,} | ETA: {eta}"
            )
            git_push()
            print(f"Success rate {success_rate*100:.1f}% >= 90% — auto-starting batch {batch+1}", flush=True)
        else:
            notify(
                f"Batch {batch} — LOW SUCCESS RATE",
                f"Cities: {success_rate*100:.0f}% ({cities_found:,}/{eiks_attempted:,}) | Emails: {email_rate*100:.0f}% | Stopping."
            )
            print(f"STOPPING: success rate {success_rate*100:.1f}% < 90%.", flush=True)
            return

    print("Pipeline complete.", flush=True)

if __name__ == "__main__":
    main()
