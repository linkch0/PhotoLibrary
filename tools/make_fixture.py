"""Build a small synthetic .photoslibrary to test albums and folders.

The real libraries on this machine contain no user albums, so the album code
path (ZGENERICALBUM + Z_<entity>ASSETS join tables, folder nesting, counts) is
otherwise never exercised. This constructs a valid library from a subset of
real assets plus album rows, so those paths run against real data.

Usage:  python tools/make_fixture.py <source-library> <output-library>
"""

from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

N_ASSETS = 24


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])

    if dst.exists():
        shutil.rmtree(dst)
    (dst / "database").mkdir(parents=True)
    for sub in ("originals", "resources"):
        (dst / sub).mkdir()

    src_db = src / "database" / "Photos.sqlite"
    con = sqlite3.connect(f"file:{src_db.as_posix()}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT Z_PK,ZUUID,ZFILENAME,ZDIRECTORY,ZKIND FROM ZASSET ORDER BY Z_PK LIMIT ?",
        (N_ASSETS,),
    ).fetchall()
    con.close()
    print(f"copying {len(rows)} assets")

    used_dirs = set()
    for pk, uuid, filename, directory, kind in rows:
        used_dirs.add(directory)
        for rel in (Path("originals") / directory / filename,):
            s = src / rel
            d = dst / rel
            if s.is_file():
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
        # Derivatives for this asset.
        for rel in (
            Path("resources/derivatives/masters") / directory / f"{uuid}_4_5005_c.jpeg",
            Path("resources/derivatives") / directory / f"{uuid}_1_102_o.jpeg",
        ):
            s = src / rel
            if s.is_file():
                d = dst / rel
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)

    # Copy the database and mutate a copy to add albums.
    shutil.copy2(src_db, dst / "database" / "Photos.sqlite")
    plist = src / "database" / "DataModelVersion.plist"
    if plist.is_file():
        shutil.copy2(plist, dst / "database" / "DataModelVersion.plist")

    db = dst / "database" / "Photos.sqlite"
    con = sqlite3.connect(db)
    cur = con.cursor()

    # Clean out any leftovers from the source, then insert our own albums.
    cur.execute("DELETE FROM Z_34ASSETS")
    cur.execute("DELETE FROM ZGENERICALBUM")

    def add_album(pk, title, ent, kind, parent=None, cached=0):
        cur.execute(
            "INSERT INTO ZGENERICALBUM (Z_PK,Z_ENT,Z_OPT,ZTITLE,ZKIND,ZPARENTFOLDER,"
            "ZCACHEDCOUNT,ZTRASHEDSTATE,ZUUID) VALUES (?,?,?,?,?,?,?,?,?)",
            (pk, ent, 1, title, kind, parent, cached, 0, f"FIXTURE-{pk}"),
        )

    # Entity 42 = Folder, 34 = Album.
    add_album(9001, "Trips", 42, 4001)
    add_album(9002, "Tokyo 2025", 34, 4002, parent=9001)
    add_album(9003, "Kyoto 2025", 34, 4003, parent=9001)
    add_album(9004, "Favourites Wall", 34, 4004)  # top-level album
    add_album(9005, "Empty Album", 34, 4005)      # must still be listed

    pks = [r[0] for r in rows]
    assign = {
        9002: pks[0:8],
        9003: pks[8:16],
        9004: pks[16:22],
    }
    for album_pk, members in assign.items():
        for asset_pk in members:
            cur.execute(
                "INSERT INTO Z_34ASSETS (Z_34ALBUMS,Z_3ASSETS,Z_FOK_3ASSETS) VALUES (?,?,0)",
                (album_pk, asset_pk),
            )
            cur.execute(
                "UPDATE ZASSET SET ZALBUMASSOCIATIVITY=1 WHERE Z_PK=?", (asset_pk,)
            )
        cur.execute(
            "UPDATE ZGENERICALBUM SET ZCACHEDCOUNT=? WHERE Z_PK=?",
            (len(members), album_pk),
        )
    con.commit()
    con.close()

    # Keep only the assets we actually copied, so the fixture behaves like a
    # real library rather than a list full of missing files.
    keep = set(pks)
    con = sqlite3.connect(db)
    cur = con.cursor()
    # Core Data installs triggers that call into Apple's runtime; drop them in
    # this throwaway copy so plain deletes work.
    for (name,) in cur.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger'"
    ).fetchall():
        cur.execute(f'DROP TRIGGER IF EXISTS "{name}"')
    placeholders = ",".join("?" * len(keep))
    cur.execute(f"DELETE FROM ZASSET WHERE Z_PK NOT IN ({placeholders})", tuple(keep))
    cur.execute(f"DELETE FROM ZADDITIONALASSETATTRIBUTES WHERE ZASSET NOT IN ({placeholders})", tuple(keep))
    cur.execute(f"DELETE FROM ZEXTENDEDATTRIBUTES WHERE ZASSET NOT IN ({placeholders})", tuple(keep))
    cur.execute(f"DELETE FROM Z_34ASSETS WHERE Z_3ASSETS NOT IN ({placeholders})", tuple(keep))
    con.commit()
    remaining = cur.execute("SELECT COUNT(*) FROM ZASSET").fetchone()[0]
    con.close()

    total = sum(len(v) for v in assign.values())
    print(f"created {dst.name} with {remaining} assets, 3 child albums, "
          f"1 top-level album, 1 folder, 1 empty album, {total} memberships")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
