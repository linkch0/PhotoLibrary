"""A partially failing index must not report clean success.

Regression for a real miss: probe_clip.py exited 0 after failing to read 14 of
20 HEIC files, because the check only looked at score spread. The same flaw
existed in the indexer, which reported plain "done" even when files could not be
read. This makes a run with unreadable files report done_with_errors.

Builds a throwaway library whose assets point at corrupt files, indexes it, and
asserts the job surfaces the failures.
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config
from app.indexer import Indexer
from app.library import PhotosLibrary
from app.semantic import SemanticIndex

SRC = Path(sys.argv[1]) if len(sys.argv) > 1 else None
if SRC is None:
    print(__doc__)
    print(f"usage: python {Path(__file__).resolve().name} <valid-library>")
    raise SystemExit(2)

TMP = Path(config.CACHE_ROOT) / "test_partial_index"
failures = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global failures
    if cond:
        print(f"  PASS  {name}")
    else:
        failures += 1
        print(f"  FAIL  {name}  {extra}")


def main() -> int:
    if TMP.exists():
        shutil.rmtree(TMP)
    (TMP / "database").mkdir(parents=True)
    (TMP / "originals").mkdir()

    # Copy the source DB, then truncate half the originals to be unreadable.
    shutil.copy2(SRC / "database" / "Photos.sqlite", TMP / "database" / "Photos.sqlite")
    con = sqlite3.connect(TMP / "database" / "Photos.sqlite")
    rows = con.execute(
        "SELECT Z_PK,ZFILENAME,ZDIRECTORY FROM ZASSET WHERE ZFILENAME IS NOT NULL LIMIT 20"
    ).fetchall()
    con.close()

    con = sqlite3.connect(TMP / "database" / "Photos.sqlite")
    for i, (pk, fn, d) in enumerate(rows):
        src = SRC / "originals" / (d or "") / fn
        dst = TMP / "originals" / (d or "") / fn
        dst.parent.mkdir(parents=True, exist_ok=True)
        if i % 2 == 0 and src.is_file():
            shutil.copy2(src, dst)
        else:
            dst.write_bytes(b"not an image")  # corrupt on purpose
    # Keep only these 20 assets so the library is small. Core Data installs
    # triggers that call into Apple's runtime, which fail on any DELETE, so drop
    # them in this throwaway copy first.
    keep = [r[0] for r in rows]
    for (name,) in con.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall():
        con.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    con.execute(f"DELETE FROM ZASSET WHERE Z_PK NOT IN ({','.join('?' * len(keep))})", keep)
    con.commit()
    con.close()

    lib = PhotosLibrary(TMP)
    print(f"test library: {len(lib.assets)} assets (half corrupt)")

    index = SemanticIndex(TMP)
    index_dir = config.library_cache_dir(TMP) / "embeddings"
    if index_dir.exists():
        shutil.rmtree(index_dir)

    indexer = Indexer()
    job = indexer.start(lib, index, score_only=False)

    for _ in range(240):
        if job.state != "running":
            break
        time.sleep(0.5)

    d = job.to_dict()
    print(f"\njob: state={d['state']} failed={d['failed']} scored={d['scored']}")
    print(f"message: {d['message']}")

    check("job finished", d["state"] in ("done", "done_with_errors"),
          f"state {d['state']}")
    check("unreadable files counted", d["failed"] > 0, f"failed={d['failed']}")
    check("state reflects partial failure", d["state"] == "done_with_errors",
          f"state {d['state']} - a run with failures must not look clean")
    check("message mentions the failures", "could not be read" in d["message"],
          d["message"])
    check("readable files still indexed", len(index.load().rows) > 0,
          f"{len(index.load().rows)} vectors")

    lib.close()
    shutil.rmtree(TMP, ignore_errors=True)
    shutil.rmtree(index_dir, ignore_errors=True)

    print(f"\nfailures: {failures}")
    print("RESULT:", "PASS" if failures == 0 else "FAIL")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
