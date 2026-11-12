"""Check library discovery honours PHOTOS_READER_ROOTS and the scrubbed defaults."""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8760


def main() -> int:
    from app import config, launcher

    print("SEARCH_ROOTS from env:", config.SEARCH_ROOTS)
    found = launcher.discover_libraries()
    print(f"discover_libraries(): {len(found)}")
    for p in found[:3]:
        print("  ", p)

    with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/recent", timeout=15) as r:
        api_libs = json.loads(r.read())["libraries"]
    print(f"/api/recent: {len(api_libs)}")
    for p in api_libs[:3]:
        print("  ", p)

    ok = len(found) > 0 and len(api_libs) == len(found)
    print("RESULT:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
