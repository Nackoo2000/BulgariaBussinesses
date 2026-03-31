"""
Automated pipeline runner — BRRA full addresses via GitHub Actions.

- 3,600 EIKs per batch (20 workers × 180 EIKs)
- After each batch:
    * success rate >= 95%  → merge CSV, push, auto-start next batch
    * success rate <  95%  → STOP and notify (investigate before continuing)
    * 0 streets found      → STOP and notify (something is broken)
"""
import sys, os, json, time, csv, glob, zipfile, io, subprocess, requests, math

sys.stdout.reconfigure(encoding='utf-8', write_through=True)

TOKEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gh_token.txt")
with open(TOKEN_FILE) as f:
    GITHUB_TOKEN = f.read().strip()

REPO     = "Nackoo2000/BulgariaBussinesses"
WORKFLOW = "fill_addresses.yml"
BRANCH   = "main"

BASE_DIR      = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GH_DIR        = os.path.dirname(os.path.abspath(__file__))
CSV_FILE      = os.path.join(BASE_DIR, "commercial_register_enriched.csv")
DONE_FILE     = os.path.join(BASE_DIR, "address_done.json")
MISSING_FILE  = os.path.join(GH_DIR, "missing_addresses_eiks.txt")
STATE_FILE    = os.path.join(GH_DIR, "address_runner_state.json")
LOG_FILE      = os.path.join(BASE_DIR, "address_runner.txt")

POLL_INTERVAL  = 60       # seconds between status checks
MAX_WAIT_HOURS = 2        # max wait per batch before timeout (workflow timeout = 75min)
TOTAL_BATCHES  = 250      # safe upper bound
WORKERS        = 20
MAX_PER_WORKER = 180
EIKS_PER_BATCH = WORKERS * MAX_PER_WORKER   # 3,600
SUCCESS_THRESH = 0.95

API     = "https://api.github.com"
HEADERS = {
    "Accept":               "application/vnd.github+json",
    "Authorization":        f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ── Logging ────────────────────────────────────────────────────────────────

_log_fh = open(LOG_FILE, "a", encoding="utf-8", buffering=1)

def log(msg):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)
    _log_fh.write(line + "\n")

# ── Notifications ──────────────────────────────────────────────────────────

def notify(title, message):
    t = title.replace("'", "")
    m = message.replace("'", "")
    ps = f"""
Add-Type -AssemblyName System.Windows.Forms
$n = New-Object System.Windows.Forms.NotifyIcon
$n.Icon = [System.Drawing.SystemIcons]::Information
$n.Visible = $true
$n.ShowBalloonTip(8000, '{t}', '{m}', [System.Windows.Forms.ToolTipIcon]::Info)
Start-Sleep -Seconds 9
$n.Dispose()
"""
    subprocess.Popen(["powershell", "-WindowStyle", "Hidden", "-Command", ps],
                     creationflags=0x08000000)

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

def api_get(path, retries=5):
    for attempt in range(retries):
        try:
            r = requests.get(f"{API}{path}", headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.json()
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < retries - 1:
                wait = 30 * (attempt + 1)
                log(f"  [net] {e.__class__.__name__}, retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                raise

def trigger_workflow(batch_number, retries=5):
    for attempt in range(retries):
        try:
            r = requests.post(
                f"{API}/repos/{REPO}/actions/workflows/{WORKFLOW}/dispatches",
                headers=HEADERS,
                json={"ref": BRANCH, "inputs": {"batch_number": str(batch_number)}},
                timeout=30,
            )
            return r.status_code == 204
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < retries - 1:
                wait = 30 * (attempt + 1)
                log(f"  [net] trigger: {e.__class__.__name__}, retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                return False

def get_latest_run_id():
    data = api_get(f"/repos/{REPO}/actions/runs?per_page=5")
    for run in data.get("workflow_runs", []):
        if run.get("path", "").endswith(WORKFLOW) or run.get("name") == "Fill Company Addresses":
            return run["id"]
    return None

def wait_for_run(run_id):
    deadline = time.time() + MAX_WAIT_HOURS * 3600
    while time.time() < deadline:
        data       = api_get(f"/repos/{REPO}/actions/runs/{run_id}")
        status     = data["status"]
        conclusion = data.get("conclusion")
        log(f"  Run {run_id}: {status} / {conclusion}")
        if status == "completed":
            return conclusion
        time.sleep(POLL_INTERVAL)
    return "timeout"

def download_artifact(run_id, batch_number):
    data      = api_get(f"/repos/{REPO}/actions/runs/{run_id}/artifacts")
    artifacts = data.get("artifacts", [])

    # Try merged artifact first
    target = f"all_addresses_batch_{batch_number}"
    art    = next((a for a in artifacts if a["name"] == target), None)
    if art:
        log(f"  Downloading merged artifact: {target}")
        return fetch_zip(art["archive_download_url"])

    # Fall back to individual worker artifacts
    log("  No merged artifact — collecting worker artifacts...")
    addr_map = {}
    for a in artifacts:
        if a["name"].startswith("address-results-worker-"):
            addr_map.update(fetch_zip(a["archive_download_url"]))
    return addr_map

def fetch_zip(url, retries=5):
    for attempt in range(retries):
        try:
            r = requests.get(url, headers=HEADERS, timeout=120)
            r.raise_for_status()
            break
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            if attempt < retries - 1:
                wait = 30 * (attempt + 1)
                log(f"  [net] fetch_zip: {e.__class__.__name__}, retry {attempt+1}/{retries} in {wait}s")
                time.sleep(wait)
            else:
                raise
    addr_map = {}
    with zipfile.ZipFile(io.BytesIO(r.content)) as zf:
        for name in zf.namelist():
            if name.endswith(".json"):
                with zf.open(name) as f:
                    try:
                        data = json.load(f)
                        if isinstance(data, dict):
                            addr_map.update(data)
                    except Exception:
                        pass
    return addr_map

# ── Address formatting ─────────────────────────────────────────────────────

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

# ── CSV helpers ────────────────────────────────────────────────────────────

def apply_addresses(addr_map):
    rows       = []
    fieldnames = None
    with open(CSV_FILE, encoding="utf-8-sig", newline="") as f:
        reader     = csv.DictReader(f)
        fieldnames = list(reader.fieldnames)
        for row in reader:
            rows.append(row)

    row_map = {r["EIK"]: r for r in rows}
    applied = 0
    for eik, data in addr_map.items():
        if eik not in row_map:
            continue
        row = row_map[eik]
        if row.get("Address", "").strip():
            continue   # never overwrite existing address
        formatted = format_address(
            data.get("settlement", ""),
            data.get("street", ""),
            data.get("post_code", ""),
        )
        if formatted:
            row["Address"] = formatted
            applied += 1

    tmp = CSV_FILE + ".tmp"
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, CSV_FILE)

    return applied, rows

def update_done(addr_map):
    done = set()
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            done = set(json.load(f))
    done.update(addr_map.keys())
    with open(DONE_FILE, "w") as f:
        json.dump(list(done), f)
    return len(done)

def regenerate_missing():
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
                continue
            if eik in done:
                continue
            missing.append(eik)
    with open(MISSING_FILE, "w", encoding="utf-8") as f:
        f.write("\n".join(missing))
    return len(missing)

def git_push():
    subprocess.run(["git", "-C", GH_DIR, "add",
                    "missing_addresses_eiks.txt", "address_runner_state.json"],
                   capture_output=True, text=True)
    r = subprocess.run(["git", "-C", GH_DIR, "commit", "-m",
                        "Update missing_addresses_eiks after batch merge"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        if "nothing to commit" in r.stdout + r.stderr:
            return True
        log(f"  git commit failed: {(r.stdout+r.stderr).strip()[:200]}")
        return False
    for attempt in range(3):
        r = subprocess.run(["git", "-C", GH_DIR, "push"], capture_output=True, text=True)
        if r.returncode == 0:
            return True
        log(f"  git push attempt {attempt+1} failed: {r.stderr.strip()[:200]}")
        subprocess.run(["git", "-C", GH_DIR, "pull", "--rebase"], capture_output=True, text=True)
        time.sleep(5)
    log("  ERROR: git push failed after 3 attempts!")
    return False

def eta_string(still_missing):
    batches_left = math.ceil(still_missing / EIKS_PER_BATCH)
    mins         = batches_left * 40   # ~40 min per batch
    if mins < 60:
        return f"{mins}min"
    return f"{mins/60:.1f}h"

# ── Main ───────────────────────────────────────────────────────────────────

def main():
    notify("BRRA Address Runner", "Starting...")
    state = load_state()
    log("=" * 60)
    log("AUTO RUNNER — BRRA FULL ADDRESSES")
    log(f"Starting at batch {state['next_batch']}")
    log("Stop rules: success rate < 95% OR 0 streets found")
    log("=" * 60)

    while state["next_batch"] < TOTAL_BATCHES:
        batch = state["next_batch"]
        log(f"\n{'='*50}")
        log(f"BATCH {batch}")

        # Count how many still need an address
        still_before = regenerate_missing()
        if still_before == 0:
            notify("BRRA Address Runner — COMPLETE", "All companies have a full address!")
            log("All done!")
            break

        # Trigger or reuse existing run
        run_id = state.get("last_run_id")
        if not run_id:
            log(f"Triggering batch {batch}...")
            if not trigger_workflow(batch):
                time.sleep(60)
                if not trigger_workflow(batch):
                    notify("BRRA Runner — ERROR", "Could not trigger workflow.")
                    return
            time.sleep(15)
            run_id = get_latest_run_id()
            log(f"Run: {run_id} → https://github.com/{REPO}/actions/runs/{run_id}")
            state["last_run_id"] = run_id
            save_state(state)

        conclusion = wait_for_run(run_id)
        log(f"Conclusion: {conclusion}")

        if conclusion == "cancelled":
            log("Run was cancelled — stopping.")
            notify("BRRA Runner — STOPPED", f"Batch {batch} cancelled. Restart when ready.")
            return

        if conclusion == "timeout":
            notify("BRRA Runner — TIMEOUT", f"Batch {batch} timed out. Check GitHub.")
            state["next_batch"] = batch + 1
            state["last_run_id"] = None
            save_state(state)
            return

        # Download & evaluate
        try:
            addr_map = download_artifact(run_id, batch)
        except Exception as e:
            log(f"ERROR downloading artifacts: {e}")
            notify("BRRA Runner — ERROR", f"Batch {batch}: could not download artifact.")
            return

        eiks_attempted  = len(addr_map)
        streets_found   = sum(1 for v in addr_map.values() if v.get("street"))
        success_rate    = streets_found / eiks_attempted if eiks_attempted > 0 else 0

        log(f"Streets found: {streets_found:,} / {eiks_attempted:,} ({success_rate*100:.1f}%)")

        if streets_found == 0:
            notify("BRRA Runner — STOPPED",
                   f"Batch {batch}: 0 streets found. Something is broken — check workers.")
            log("STOPPING: 0 streets found.")
            return

        if success_rate < SUCCESS_THRESH:
            notify("BRRA Runner — LOW SUCCESS",
                   f"Batch {batch}: {success_rate*100:.1f}% streets (threshold 95%). Stopping.")
            log(f"STOPPING: success rate {success_rate*100:.1f}% < 95%.")
            return

        # Apply to CSV
        applied, rows = apply_addresses(addr_map)
        total_done    = update_done(addr_map)
        still_after   = regenerate_missing()

        total         = len(rows)
        with_addr     = sum(1 for r in rows if r.get("Address", "").strip())
        overall_pct   = with_addr / total * 100
        eta           = eta_string(still_after)

        log(f"Applied {applied:,} addresses. Coverage: {with_addr:,}/{total:,} ({overall_pct:.1f}%). Remaining: {still_after:,}")

        state["next_batch"] = batch + 1
        state["last_run_id"] = None
        save_state(state)

        notify(
            f"BRRA Batch {batch} OK",
            f"Batch {success_rate*100:.0f}% | Scraped: {total_done:,}/{total:,} | "
            f"Address: {with_addr:,}/{total:,} ({overall_pct:.1f}%) | "
            f"Left: {still_after:,} ~{eta}"
        )

        if not git_push():
            notify("BRRA Runner — GIT PUSH FAILED",
                   f"Batch {batch}: git push failed. Fix and restart.")
            log("STOPPING: git push failed — workers would use stale missing_addresses_eiks.txt")
            return

        log(f"Auto-starting batch {batch+1}...")

    log("Pipeline complete.")

if __name__ == "__main__":
    main()
