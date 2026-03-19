"""
GitHub Actions worker — runs as one job in a matrix.
Usage: python worker.py <worker_index> <total_workers> <batch_number>
  worker_index  : 0-based index of this worker (0 to total_workers-1)
  total_workers : how many parallel workers in this run (e.g. 20)
  batch_number  : which batch of the full list to process (0, 1, 2, ...)

Each run covers: total_workers * MAX_PER_WORKER EIKs
Each worker runs at 1 req/10s (same rate as step3_final.py) = 180 EIKs per 30 min.
"""

import sys, os, json, time, requests

INTER_REQ_S    = 10     # 1 request every 10s — same safe rate as original step3_final.py
MAX_PER_WORKER = 90     # 15 min × 6 req/min = 90 EIKs per worker
REQ_TIMEOUT    = 20     # per-request timeout in seconds

SEAT_URL = "https://portal.registryagency.bg/CR/api/Deeds/{eik}/Seat"

def fetch_city(eik, session):
    try:
        r = session.get(SEAT_URL.format(eik=eik), timeout=REQ_TIMEOUT)
        if r.status_code == 200:
            data = r.json()
            return data.get('address', {}).get('settlement', '') or ''
        if r.status_code == 429:
            print(f"  [429] rate limit hit — backing off 60s", flush=True)
            time.sleep(60)
            # one retry after backoff
            r2 = session.get(SEAT_URL.format(eik=eik), timeout=REQ_TIMEOUT)
            if r2.status_code == 200:
                data = r2.json()
                return data.get('address', {}).get('settlement', '') or ''
        return ''
    except Exception as e:
        print(f"  [err] {eik}: {e}", flush=True)
        return None

def main():
    if len(sys.argv) != 4:
        print("Usage: python worker.py <worker_index> <total_workers> <batch_number>")
        sys.exit(1)

    worker_idx    = int(sys.argv[1])
    total_workers = int(sys.argv[2])
    batch_num     = int(sys.argv[3])

    with open("missing_eiks.txt") as f:
        all_eiks = [line.strip() for line in f if line.strip()]

    per_run   = total_workers * MAX_PER_WORKER
    run_start = batch_num * per_run
    run_eiks  = all_eiks[run_start : run_start + per_run]

    if not run_eiks:
        print(f"Worker {worker_idx}: nothing to do (batch {batch_num} is empty)")
        with open(f"results_{worker_idx}.json", "w") as f:
            json.dump({}, f)
        return

    worker_eiks = run_eiks[worker_idx::total_workers][:MAX_PER_WORKER]

    print(f"Worker {worker_idx}/{total_workers} | Batch {batch_num} | "
          f"{len(worker_eiks):,} EIKs (run offset: {run_start:,})", flush=True)

    session = requests.Session()
    session.headers.update({
        'Accept': 'application/json',
        'User-Agent': 'Mozilla/5.0'
    })

    results = {}
    found   = 0
    errors  = 0

    for i, eik in enumerate(worker_eiks):
        req_start = time.time()

        city = fetch_city(eik, session)
        if city is None:
            errors += 1
        elif city:
            results[eik] = city
            found += 1

        if (i + 1) % 50 == 0 or i == len(worker_eiks) - 1:
            print(f"  [{i+1:,}/{len(worker_eiks):,}] found={found} errors={errors}", flush=True)

        elapsed = time.time() - req_start
        sleep_time = max(0, INTER_REQ_S - elapsed)
        if sleep_time > 0 and i < len(worker_eiks) - 1:
            time.sleep(sleep_time)

    out_file = f"results_{worker_idx}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False)

    print(f"\nWorker {worker_idx} done: {found} cities found, {errors} errors → {out_file}", flush=True)

if __name__ == "__main__":
    main()
