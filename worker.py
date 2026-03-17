"""
GitHub Actions worker — runs as one job in a matrix.
Usage: python worker.py <worker_index> <total_workers> <batch_number>
  worker_index  : 0-based index of this worker (0 to total_workers-1)
  total_workers : how many parallel workers in this run (e.g. 20)
  batch_number  : which batch of the full list to process (0, 1, 2, ...)

Each run covers: total_workers * MAX_PER_WORKER EIKs
Each worker runs for up to 5.5 hours at 1 req / 10s = 1,980 EIKs per worker per run.
"""

import sys, os, json, time, requests

INTER_REQ_S   = 10       # seconds between requests (safe: 6 per 60s)
MAX_PER_WORKER = 1980    # 5.5 hours × 360/hr — stays under the 6h GitHub limit
REQ_TIMEOUT   = 20

SEAT_URL = "https://portal.registryagency.bg/CR/api/Deeds/{eik}/Seat"

def fetch_city(eik, session):
    for attempt in range(3):
        try:
            r = session.get(SEAT_URL.format(eik=eik), timeout=REQ_TIMEOUT)
            if r.status_code == 200:
                return r.json().get("address", {}).get("settlement", "") or ""
            if r.status_code == 404:
                return ""
            if r.status_code == 429:
                wait = 60 + attempt * 30
                print(f"  [429] {eik} — waiting {wait}s", flush=True)
                time.sleep(wait)
                continue
            time.sleep(5 * (attempt + 1))
        except Exception as e:
            print(f"  [err] {eik}: {e}", flush=True)
            time.sleep(5 * (attempt + 1))
    return None  # failed

def main():
    if len(sys.argv) != 4:
        print("Usage: python worker.py <worker_index> <total_workers> <batch_number>")
        sys.exit(1)

    worker_idx   = int(sys.argv[1])
    total_workers = int(sys.argv[2])
    batch_num    = int(sys.argv[3])

    # Load full EIK list
    with open("missing_eiks.txt") as f:
        all_eiks = [line.strip() for line in f if line.strip()]

    # Determine which EIKs this run covers
    per_run   = total_workers * MAX_PER_WORKER
    run_start = batch_num * per_run
    run_eiks  = all_eiks[run_start : run_start + per_run]

    if not run_eiks:
        print(f"Worker {worker_idx}: nothing to do (batch {batch_num} is empty)")
        with open(f"results_{worker_idx}.json", "w") as f:
            json.dump({}, f)
        return

    # This worker's slice within the run
    worker_eiks = run_eiks[worker_idx::total_workers]   # interleaved split
    worker_eiks = worker_eiks[:MAX_PER_WORKER]

    print(f"Worker {worker_idx}/{total_workers} | Batch {batch_num} | "
          f"Processing {len(worker_eiks):,} EIKs "
          f"(total in run: {len(run_eiks):,}, offset: {run_start:,})", flush=True)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
        "Accept": "application/json",
    })

    results = {}
    found = 0
    errors = 0

    for i, eik in enumerate(worker_eiks):
        req_start = time.time()

        city = fetch_city(eik, session)
        if city is None:
            errors += 1
        elif city:
            results[eik] = city
            found += 1

        if (i + 1) % 50 == 0 or i == len(worker_eiks) - 1:
            elapsed = time.time() - (req_start - i * INTER_REQ_S)
            print(f"  [{i+1:,}/{len(worker_eiks):,}] found={found} errors={errors}", flush=True)

        elapsed_req = time.time() - req_start
        sleep_time  = max(0, INTER_REQ_S - elapsed_req)
        if sleep_time > 0 and i < len(worker_eiks) - 1:
            time.sleep(sleep_time)

    out_file = f"results_{worker_idx}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)

    print(f"\nWorker {worker_idx} done: {found} cities found, {errors} errors → {out_file}", flush=True)

if __name__ == "__main__":
    main()
