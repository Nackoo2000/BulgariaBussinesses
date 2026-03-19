"""
GitHub Actions worker — runs as one job in a matrix.
Usage: python worker.py <worker_index> <total_workers> <batch_number>

Saves partial results on SIGTERM (timeout/cancel) so no work is lost.
"""

import sys, os, json, time, signal, requests

INTER_REQ_S    = 10     # 1 request every 10s — same safe rate as step3_final.py
MAX_PER_WORKER = 90     # 15 min × 6 req/min = 90 EIKs per worker
REQ_TIMEOUT    = 20
BACKOFF_429    = 30     # seconds to wait after a 429 (reduced from 60)

SEAT_URL = "https://portal.registryagency.bg/CR/api/Deeds/{eik}/Seat"

# Global so SIGTERM handler can access it
results  = {}
out_file = "results_0.json"

def save_results():
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)

def handle_sigterm(signum, frame):
    """Called when GitHub Actions cancels or times out the job."""
    print(f"\n[SIGTERM] Saving {len(results)} partial results to {out_file}", flush=True)
    save_results()
    sys.exit(0)

signal.signal(signal.SIGTERM, handle_sigterm)

def fetch_city(eik, session):
    try:
        r = session.get(SEAT_URL.format(eik=eik), timeout=REQ_TIMEOUT)
        if r.status_code == 200:
            return r.json().get('address', {}).get('settlement', '') or ''
        if r.status_code == 429:
            print(f"  [429] backing off {BACKOFF_429}s", flush=True)
            time.sleep(BACKOFF_429)
            r2 = session.get(SEAT_URL.format(eik=eik), timeout=REQ_TIMEOUT)
            if r2.status_code == 200:
                return r2.json().get('address', {}).get('settlement', '') or ''
        return ''
    except Exception as e:
        print(f"  [err] {eik}: {e}", flush=True)
        return None

def main():
    global results, out_file

    if len(sys.argv) != 4:
        print("Usage: python worker.py <worker_index> <total_workers> <batch_number>")
        sys.exit(1)

    worker_idx    = int(sys.argv[1])
    total_workers = int(sys.argv[2])
    batch_num     = int(sys.argv[3])
    out_file      = f"results_{worker_idx}.json"

    with open("missing_eiks.txt") as f:
        all_eiks = [line.strip() for line in f if line.strip()]

    per_run   = total_workers * MAX_PER_WORKER
    run_start = batch_num * per_run
    run_eiks  = all_eiks[run_start : run_start + per_run]

    if not run_eiks:
        print(f"Worker {worker_idx}: nothing to do (batch {batch_num} is empty)")
        save_results()
        return

    worker_eiks = run_eiks[worker_idx::total_workers][:MAX_PER_WORKER]

    print(f"Worker {worker_idx}/{total_workers} | Batch {batch_num} | "
          f"{len(worker_eiks):,} EIKs (offset {run_start:,})", flush=True)

    session = requests.Session()
    session.headers.update({'Accept': 'application/json', 'User-Agent': 'Mozilla/5.0'})

    found  = 0
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
            print(f"  [{i+1}/{len(worker_eiks)}] found={found} errors={errors}", flush=True)

        elapsed    = time.time() - req_start
        sleep_time = max(0, INTER_REQ_S - elapsed)
        if sleep_time > 0 and i < len(worker_eiks) - 1:
            time.sleep(sleep_time)

    save_results()
    print(f"\nWorker {worker_idx} done: {found} cities, {errors} errors → {out_file}", flush=True)

if __name__ == "__main__":
    main()
