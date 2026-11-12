"""Print album tree + membership checks for the fixture library."""

from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BASE = "http://127.0.0.1:8760"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=60) as r:
        return json.loads(r.read())


def main() -> int:
    info = get("/api/library")
    print(f"library: {info['name']}  assets={info['stats']['assets']}")
    print("\nalbums as reported:")
    for a in info["albums"]:
        print(f"  {a['kind']:6s} id={a['id']:5d} {a['title']!r:18s} "
              f"count={a['count']:2d} parent={a['parent']}")

    print("\nasset counts per album (via /api/assets?album=):")
    failures = []
    expected = {}
    for a in info["albums"]:
        data = get(f"/api/assets?album={a['id']}&limit=1")
        expected[a["id"]] = data["total"]
        print(f"  id={a['id']:5d} {a['title']!r:18s} api_total={data['total']:2d} "
              f"(sidebar said {a['count']})")
        if data["total"] != a["count"]:
            failures.append((a["id"], a["title"], a["count"], data["total"]))

    # Folder must aggregate its children: 8 + 8 = 16.
    folder = next((a for a in info["albums"] if a["kind"] == "folder"), None)
    if folder:
        total = get(f"/api/assets?album={folder['id']}&limit=1")["total"]
        print(f"\nfolder {folder['title']!r} aggregates {total} assets "
              f"(children sum expected 16)")
        if total != 16:
            failures.append((folder["id"], "folder aggregate", 16, total))

    # An album's assets must all actually belong to it.
    alb = next((a for a in info["albums"] if a["title"] == "Tokyo 2025"), None)
    if alb:
        items = get(f"/api/assets?album={alb['id']}&limit=100")["items"]
        wrong = [i["id"] for i in items
                 if "Tokyo 2025" not in get(f"/api/assets/{i['id']}").get("folders", [])]
        print(f"Tokyo 2025: {len(items)} assets, {len(wrong)} not reporting that album")
        if wrong:
            failures.append((alb["id"], "membership", 0, len(wrong)))

    print(f"\nfailures: {len(failures)}")
    for f in failures:
        print("  ", f)
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
