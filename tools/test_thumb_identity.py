"""Prove thumbnails differ per library for the same numeric asset id.

Asset ids are per-library SQLite primary keys, so id 846 exists in both a 2023
and a 2026 library while referring to different photos. Media is served with an
immutable cache, so the client must version the URL with a library token or the
browser will show the previous library's pixels.

Usage: python tools/test_thumb_identity.py <port> <libA> <libB>
"""

from __future__ import annotations

import hashlib
import json
import sys
import urllib.request
from pathlib import Path

if len(sys.argv) < 4:
    print(__doc__)
    print("usage: python tools/test_thumb_identity.py <port> <libA> <libB>")
    raise SystemExit(2)

PORT = int(sys.argv[1])
LIB_A, LIB_B = sys.argv[2], sys.argv[3]
BASE = f"http://127.0.0.1:{PORT}"


def call(path: str, data: dict | None = None) -> dict:
    req = urllib.request.Request(BASE + path)
    if data is not None:
        req.data = json.dumps(data).encode()
        req.add_header("Content-Type", "application/json")
        req.method = "POST"
    with urllib.request.urlopen(req, timeout=90) as r:
        return json.loads(r.read() or b"{}")


def raw(path: str) -> tuple[int, str, bytes]:
    with urllib.request.urlopen(BASE + path, timeout=90) as r:
        return r.status, r.headers.get("Cache-Control", ""), r.read()


def all_ids() -> dict[int, str]:
    """Every asset id -> name for the open library, paged (server caps limit)."""
    out: dict[int, str] = {}
    offset = 0
    while True:
        page = call(f"/api/assets?limit=2000&offset={offset}")
        items = page["items"]
        out.update({i["id"]: i["name"] for i in items})
        offset += len(items)
        if not items or offset >= page["total"]:
            break
    return out


def main() -> int:
    failures: list[str] = []

    a = call("/api/library/open", {"path": LIB_A})
    tok_a = a.get("token")
    ids_a = all_ids()

    b = call("/api/library/open", {"path": LIB_B})
    tok_b = b.get("token")
    ids_b = all_ids()

    print(f"A token={tok_a}  B token={tok_b}")
    if not tok_a or not tok_b:
        failures.append("library response has no token")
    if tok_a == tok_b:
        failures.append("tokens identical for different libraries")

    shared = sorted(set(ids_a) & set(ids_b))
    print(f"shared numeric ids between A and B: {len(shared)}")
    if not shared:
        print("  (no overlap; cannot compare the same id across libraries)")
        print("RESULT:", "PASS" if not failures else "FAIL")
        return 0 if not failures else 1

    # Same id, different library: the bytes must differ, and the versioned URLs
    # must differ so the browser cannot reuse its cache entry.
    diffs = 0
    for aid in shared[:5]:
        call("/api/library/open", {"path": LIB_A})
        s1, cc1, d1 = raw(f"/api/thumb/{aid}?size=s&lib={tok_a}")
        call("/api/library/open", {"path": LIB_B})
        s2, cc2, d2 = raw(f"/api/thumb/{aid}?size=s&lib={tok_b}")
        h1, h2 = hashlib.sha1(d1).hexdigest()[:10], hashlib.sha1(d2).hexdigest()[:10]
        same = h1 == h2
        print(f"  id {aid}: A={ids_a[aid][:16]:16s} {h1}   B={ids_b[aid][:16]:16s} {h2}"
              f"  {'SAME (bug)' if same else 'differ ok'}")
        if not same:
            diffs += 1
        else:
            failures.append(f"id {aid} returned identical bytes for both libraries")

    print(f"\ncompared {min(5, len(shared))} shared ids, {diffs} differ correctly")

    # Tokens must be attached to the URLs the client builds (checked in JS).
    js = (Path(__file__).parent.parent / "app" / "static" / "app.js").read_text(encoding="utf-8")
    for needle, label in [
        ("function mediaUrl", "mediaUrl helper exists"),
        ("state.lib.token", "mediaUrl uses the library token"),
        ("mediaUrl('thumb'", "thumbnails use mediaUrl"),
        ("mediaUrl('preview'", "previews use mediaUrl"),
        ('mediaUrl("video"', "videos use mediaUrl"),
        ('mediaUrl("original"', "originals use mediaUrl"),
    ]:
        ok = needle in js
        print(f"  {'PASS' if ok else 'FAIL'}  {label}")
        if not ok:
            failures.append(label)

    print(f"\nfailures: {len(failures)}")
    for f in failures:
        print("  ", f)
    print("RESULT:", "PASS" if not failures else "FAIL")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
