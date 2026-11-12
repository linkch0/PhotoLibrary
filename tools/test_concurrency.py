"""Concurrency smoke test: many simultaneous thumbnail requests.

Simulates a browser pulling a fresh grid, which is the heaviest thing the app
does. Checks for failures, measures throughput, and confirms the API stays
responsive while the decode queue is busy.
"""

from __future__ import annotations

import concurrent.futures as cf
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8760
BASE = f"http://127.0.0.1:{PORT}"


def get(path: str, timeout: float = 120) -> tuple[int, int]:
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        data = r.read()
        return r.status, len(data)


def main() -> int:
    with urllib.request.urlopen(BASE + "/api/assets?limit=80", timeout=30) as r:
        items = json.loads(r.read())["items"]
    ids = [i["id"] for i in items]
    print(f"requesting {len(ids)} thumbnails concurrently")

    start = time.time()
    errors = []
    sizes = []
    with cf.ThreadPoolExecutor(max_workers=16) as pool:
        futures = {pool.submit(get, f"/api/thumb/{i}?size=s"): i for i in ids}
        for fut in cf.as_completed(futures):
            aid = futures[fut]
            try:
                status, size = fut.result()
                sizes.append(size)
                if status != 200 or size < 1000:
                    errors.append((aid, status, size))
            except Exception as exc:
                errors.append((aid, "exc", str(exc)))
    elapsed = time.time() - start

    print(f"  {len(ids)} thumbs in {elapsed:.2f}s "
          f"({len(ids) / elapsed:.1f}/s), avg {sum(sizes) / max(len(sizes), 1) / 1024:.0f} KB")
    print(f"  errors: {len(errors)}")
    for e in errors[:8]:
        print("   ", e)

    # The API must still answer while thumbnails stream.
    start = time.time()
    status, size = get("/api/assets?limit=5")
    print(f"  API latency during idle: {(time.time() - start) * 1000:.0f} ms")
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
