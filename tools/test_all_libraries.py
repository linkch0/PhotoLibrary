"""Open every discovered library and verify it renders.

Checks each library end to end: opens it, confirms a plausible asset count and
dates, fetches a thumbnail and a preview, and reports stats. This is the test
that proves the schema handling works beyond the one library developed against.

Usage: python tools/test_all_libraries.py [port]
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8760
BASE = f"http://127.0.0.1:{PORT}"


def call(path: str, data: dict | None = None, timeout: float = 90):
    req = urllib.request.Request(BASE + path)
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
        req.method = "POST"
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def fetch(url: str, timeout: float = 90) -> bytes:
    with urllib.request.urlopen(BASE + url, timeout=timeout) as r:
        return r.read()


def main() -> int:
    libs = call("/api/recent")["libraries"]
    print(f"testing {len(libs)} libraries\n")

    rows = []
    failures = []
    for path in libs:
        name = path.split("\\")[-1]
        start = time.time()
        try:
            info = call("/api/library/open", {"path": path})
            elapsed = time.time() - start
            st = info["stats"]
            if st["assets"] == 0:
                failures.append((name, "zero assets"))
                print(f"  {name[:44]:46s} FAIL zero assets")
                continue

            items = call("/api/assets?limit=3")["items"]
            if not items:
                failures.append((name, "no items from /api/assets"))
                print(f"  {name[:44]:46s} FAIL no items")
                continue

            thumb = fetch(f"/api/thumb/{items[0]['id']}?size=s")
            prev = fetch(f"/api/preview/{items[0]['id']}")
            if len(thumb) < 800:
                failures.append((name, f"tiny thumbnail {len(thumb)}B"))
            if len(prev) < 3000:
                failures.append((name, f"tiny preview {len(prev)}B"))

            years = ",".join(st["years"].keys())
            rows.append({
                "name": name, "assets": st["assets"], "photos": st["photos"],
                "videos": st["videos"], "live": st["live"], "albums": len(info["albums"]),
                "years": years, "schema": st["schemaVersion"], "open_s": round(elapsed, 2),
                "thumb": len(thumb), "prev": len(prev),
            })
            print(f"  {name[:44]:46s} OK {st['assets']:5d} assets  "
                  f"{st['photos']:4d}p/{st['videos']:3d}v/{st['live']:4d}live  "
                  f"albums={len(info['albums']):2d}  {elapsed:.2f}s")
        except Exception as exc:
            failures.append((name, str(exc)))
            print(f"  {name[:44]:46s} FAIL {exc}")

    total = sum(r["assets"] for r in rows)
    print(f"\nopened {len(rows)}/{len(libs)} libraries, {total} assets total")
    if rows:
        print(f"slowest open: {max(r['open_s'] for r in rows):.2f}s, "
              f"fastest: {min(r['open_s'] for r in rows):.2f}s")
    print(f"failures: {len(failures)}")
    for f in failures:
        print("  ", f)

    out = Path(__file__).parent / "all_libraries.json"
    out.write_text(json.dumps({"libraries": rows, "failures": failures},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"detail -> {out}")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
