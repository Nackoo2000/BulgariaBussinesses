"""
Automated pipeline runner.
- Polls GitHub for the current batch to complete
- Downloads the artifact (all_cities.json)
- Merges cities into commercial_register_final.csv
- Pushes updated missing_eiks.txt to GitHub
- Triggers the next batch
- Repeats until all companies have a city

Usage: python auto_runner.py
"""
import sys, os, json, time, csv, glob, zipfile, io, subprocess, requests

sys.stdout.reconfigure(encoding='utf-8', write_through=True)

# ── Config ─────────────────────────────────────────────────────────────────
GITHUB_TOKEN = "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"
REPO         = "Nackoo2000/BulgariaBussinesses"
WORKFLOW     = "fill_cities.yml"
BRANCH       = "main"

BASE_DIR     = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GH_DIR       = os.path.dirname(os.path.abspath(__file__))
CSV_FILE     = os.path.join(BASE_DIR, "commercial_register_final.csv")
DONE_FILE    = os.path.join(BASE_DIR, "epzeu_done.json")
MISSING_FILE = os.path.join(GH_DIR, "missing_eiks.txt")
STATE_FILE   = os.path.join(GH_DIR, "runner_state.json")

POLL_INTERVAL   = 120    # seconds between run status checks
MAX_WAIT_HOURS  = 7      # max hours to wait for a batch before giving up
TOTAL_BATCHES   = 15     # 0 through 14

API = "https://api.github.com"
HEADERS = {
    "Accept": "application/vnd.github+json",
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "X-GitHub-Api-Version": "2022-11-28",
}

# ── State ──────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"next_batch": 0, "last_run_id": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

# ── GitHub API helpers ─────────────────────────────────────────────────────

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
        data = api_get(f"/repos/{REPO}/actions/runs/{run_id}")
        status     = data["status"]
        conclusion = data.get("conclusion")
        print(f"  Run {run_id}: status={status} conclusion={conclusion}", flush=True)
        if status == "completed":
            return conclusion
        time.sleep(POLL_INTERVAL)
    print(f"  Timeout waiting for run {run_id}", flush=True)
    return "timeout"

def download_artifact(run_id, batch_number):
    """Download all_cities_batch_N artifact and return the city map dict."""
    data = api_get(f"/repos/{REPO}/actions/runs/{run_id}/artifacts")
    artifacts = data.get("artifacts", [])

    # Find merged artifact first, fall back to individual worker results
    target = f"all_cities_batch_{batch_number}"
    art = next((a for a in artifacts if a["name"] == target), None)
    if not art:
        # Try to merge from individual worker artifacts
        print(f"  No merged artifact '{target}', collecting worker artifacts...", flush=True)
        city_map = {}
        for a in artifacts:
            if a["name"].startswith("results-worker-"):
                worker_map = download_single_artifact(a["archive_download_url"])
                city_map.update(worker_map)
        return city_map

    return download_single_artifact(art["archive_download_url"])

def download_single_artifact(url):
    """Download a ZIP artifact and extract JSON city maps."""
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
        reader = csv.DictReader(f)
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

def apply_cities(companies, city_map):
    applied = 0
    for eik, city in city_map.items():
        if eik in companies and not companies[eik].get("Registered Address", "").strip():
            companies[eik]["Registered Address"] = city
            applied += 1
    return applied

def update_done(city_map):
    done = set()
    if os.path.exists(DONE_FILE):
        with open(DONE_FILE) as f:
            done = set(json.load(f))
    done.update(city_map.keys())
    with open(DONE_FILE, "w") as f:
        json.dump(list(done), f)
    return done

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

def git_push_updated_list():
    """Commit and push the updated missing_eiks.txt."""
    cmds = [
        ["git", "-C", GH_DIR, "add", "missing_eiks.txt", "runner_state.json"],
        ["git", "-C", GH_DIR, "commit", "-m", "Update missing_eiks.txt after batch merge"],
        ["git", "-C", GH_DIR, "push"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 and "nothing to commit" not in result.stdout:
            print(f"  git warning: {result.stderr.strip()[:200]}", flush=True)

# ── Main loop ──────────────────────────────────────────────────────────────

def main():
    state = load_state()
    print("=" * 60)
    print("AUTO RUNNER — GitHub Actions batch pipeline")
    print("=" * 60)
    print(f"Starting at batch {state['next_batch']} / {TOTAL_BATCHES - 1}", flush=True)

    while state["next_batch"] < TOTAL_BATCHES:
        batch = state["next_batch"]
        print(f"\n{'='*50}")
        print(f"BATCH {batch}/{TOTAL_BATCHES - 1}", flush=True)

        # Check stats
        companies, fieldnames = load_csv()
        total = len(companies)
        with_city = sum(1 for r in companies.values() if r.get("Registered Address", "").strip())
        missing_count = total - with_city
        print(f"Current: {with_city:,} with city | {missing_count:,} missing ({missing_count/total*100:.1f}%)", flush=True)

        if missing_count == 0:
            print("All companies have a city! Done.", flush=True)
            break

        # If there's already a running batch, get its run ID
        run_id = state.get("last_run_id")

        if not run_id:
            # Trigger new batch
            print(f"Triggering batch {batch}...", flush=True)
            ok = trigger_workflow(batch)
            if not ok:
                print("Failed to trigger workflow, retrying in 60s...", flush=True)
                time.sleep(60)
                ok = trigger_workflow(batch)
                if not ok:
                    print("ERROR: Could not trigger workflow. Stopping.", flush=True)
                    return

            time.sleep(15)  # wait for GitHub to register the run
            run_id = get_latest_run_id()
            print(f"Run ID: {run_id} | https://github.com/{REPO}/actions/runs/{run_id}", flush=True)
            state["last_run_id"] = run_id
            save_state(state)

        # Wait for completion
        print(f"Waiting for run {run_id} to complete (polling every {POLL_INTERVAL}s)...", flush=True)
        conclusion = wait_for_run(run_id)
        print(f"Run completed: {conclusion}", flush=True)

        if conclusion in ("success", "failure", "partial"):
            # Download artifacts and merge (even on partial failure — some workers may have succeeded)
            print("Downloading artifacts...", flush=True)
            try:
                city_map = download_artifact(run_id, batch)
                print(f"Downloaded {len(city_map):,} cities from batch {batch}", flush=True)

                if city_map:
                    companies, fieldnames = load_csv()
                    applied = apply_cities(companies, city_map)
                    save_csv(companies, fieldnames)
                    update_done(city_map)
                    print(f"Applied {applied:,} new cities to CSV", flush=True)

                    # Regenerate missing list for next batch
                    still_missing = regenerate_missing()
                    print(f"Still missing after merge: {still_missing:,}", flush=True)

                    if still_missing == 0:
                        print("\nALL DONE! Every company has a city.", flush=True)
                        state["next_batch"] = TOTAL_BATCHES
                        save_state(state)
                        break

                    # Push updated list to GitHub
                    git_push_updated_list()

            except Exception as e:
                print(f"ERROR downloading/merging: {e}", flush=True)

        elif conclusion == "timeout":
            print(f"Batch {batch} timed out — continuing to next batch anyway", flush=True)

        # Advance to next batch
        state["next_batch"] = batch + 1
        state["last_run_id"] = None
        save_state(state)

        # Print updated stats
        companies, _ = load_csv()
        with_city = sum(1 for r in companies.values() if r.get("Registered Address", "").strip())
        total = len(companies)
        print(f"\nProgress: {with_city:,}/{total:,} ({with_city/total*100:.1f}%) have city", flush=True)

    # Final report
    companies, _ = load_csv()
    total     = len(companies)
    with_city = sum(1 for r in companies.values() if r.get("Registered Address", "").strip())
    print(f"\n{'='*60}")
    print(f"PIPELINE COMPLETE")
    print(f"{'='*60}")
    print(f"Total companies : {total:,}")
    print(f"With city       : {with_city:,}  ({with_city/total*100:.1f}%)")
    print(f"Without city    : {total - with_city:,}")
    print(f"Saved → {CSV_FILE}")

if __name__ == "__main__":
    main()
