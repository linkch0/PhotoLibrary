"""Prove the app never modifies a .photoslibrary bundle.

Snapshots every file's size + mtime, hammers the API (thumbnails, previews,
video, search, export), then re-snapshots and diffs. Also asserts no new files
appear inside the bundle.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8760"


def snapshot(root: Path) -> dict[str, tuple[int, float]]:
    out: dict[str, tuple[int, float]] = {}
    for p in root.rglob("*"):
        try:
            if p.is_file():
                st = p.stat()
                out[str(p.relative_to(root))] = (st.st_size, round(st.st_mtime, 3))
        except OSError:
            continue
    return out


def call(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(BASE + path)
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
        req.method = "POST"
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read() or b"{}")


def main(argv: list[str]) -> int:
    global BASE
    if len(argv) < 2:
        print(__doc__)
        return 2
    lib = Path(argv[1])
    port = int(argv[2]) if len(argv) > 2 else 8760
    BASE = f"http://127.0.0.1:{port}"
    dest = Path(os.environ.get("PHOTOS_READER_TEST_EXPORT")
                or Path(tempfile.gettempdir()) / "photos_reader_ro_export")

    print(f"library: {lib}")
    before = snapshot(lib)
    print(f"files before: {len(before)}")

    # Make sure the target library is the one open.
    info = call("/api/library/open", {"path": str(lib)})
    print(f"opened: {info['name']} ({info['stats']['visible']} items)")

    assets = call("/api/assets?limit=60")["items"]
    ok = 0
    for a in assets:
        for url in (f"/api/thumb/{a['id']}?size=s", f"/api/thumb/{a['id']}?size=l"):
            try:
                urllib.request.urlopen(BASE + url, timeout=30).read()
                ok += 1
            except Exception as exc:
                print(f"  thumb fail {a['id']}: {exc}")

    for a in assets[:12]:
        try:
            urllib.request.urlopen(f"{BASE}/api/preview/{a['id']}", timeout=60).read()
            ok += 1
        except Exception as exc:
            print(f"  preview fail {a['id']}: {exc}")

    # One video transcode, which is the only path that shells out to ffmpeg.
    vids = call("/api/assets?kind=video&limit=1")["items"]
    if vids:
        try:
            urllib.request.urlopen(f"{BASE}/api/video/{vids[0]['id']}", timeout=600).read()
            ok += 1
            print("  video transcode ok")
        except Exception as exc:
            print(f"  video fail: {exc}")

    for q in ("IMG_", "Disney", "png"):
        call(f"/api/assets?q={q}&limit=20")
    call("/api/timeline")
    call(f"/api/assets/{assets[0]['id']}")

    # Copy a small slice out to a scratch dir.
    exp = call("/api/export", {"dest": str(dest), "kind": "screenshot"})
    print(f"  export: {exp['copied']} copied")

    after = snapshot(lib)
    print(f"files after:  {len(after)}")

    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(k for k in set(before) & set(after) if before[k] != after[k])

    print(f"\nOK requests: {ok}")
    print(f"added:   {len(added)}")
    print(f"removed: {len(removed)}")
    print(f"changed: {len(changed)}")
    for k in added[:12]:
        print("  +", k)
    for k in removed[:12]:
        print("  -", k)
    for k in changed[:12]:
        print("  ~", k, before[k], "->", after[k])

    clean = not added and not removed and not changed
    print("\nRESULT:", "READ-ONLY CONFIRMED" if clean else "LIBRARY WAS MODIFIED")
    return 0 if clean else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
