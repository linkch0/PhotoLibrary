"""Reading a macOS Photos `.photoslibrary` bundle on Windows.

The bundle is read strictly read-only. Three facts drive the design:

1. Everything authoritative lives in ``database/Photos.sqlite`` (Core Data).
   ``ZASSET`` holds one row per photo/video; ``ZADDITIONALASSETATTRIBUTES``
   holds the human-facing names, timezone and title, and
   ``ZEXTENDEDATTRIBUTES`` holds camera/EXIF values.
2. Timestamps are Apple epoch (2001-01-01 UTC) in seconds and are UTC.
   ``ZTIMEZONEOFFSET`` converts to the wall-clock time the photo was taken,
   which is what a person expects to see and to group by.
3. Originals sit at ``originals/<ZDIRECTORY>/<ZFILENAME>``, but Photos also
   writes pre-rendered JPEG derivatives that are far cheaper to display:
   ``_4_5005_c`` (small), ``_1_102_o`` (preview, edits applied) and
   ``resources/renders/..._1_201_a`` (full render, edits applied).

Column sets drift between Photos releases, so every optional column is probed
before use instead of being assumed.
"""

from __future__ import annotations

import datetime as dt
import os
import re
import shutil
import sqlite3
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path

APPLE_EPOCH = dt.datetime(2001, 1, 1, tzinfo=dt.timezone.utc)

# ZKIND on ZASSET
KIND_PHOTO = 0
KIND_VIDEO = 1

# ZKINDSUBTYPE
SUBTYPE_NONE = 0
SUBTYPE_LIVE = 2
SUBTYPE_SCREENSHOT = 10
SUBTYPE_TIMELAPSE = 102
SUBTYPE_SLO_MO = 103

# Core Data entity ids for album-ish rows (stable across Photos 5+).
ENT_ALBUM = 34
ENT_SHARED_ALBUM = 35
ENT_FOLDER = 42
ENT_SMART_ALBUM = 41

INTERNAL_ALBUM_KINDS = {3571, 3572, 3573}  # progress-* placeholders


class LibraryError(RuntimeError):
    """Raised when a folder is not a readable Photos library."""


def _apple_time(value):
    """Apple-epoch seconds -> aware UTC datetime (None-safe)."""
    if value is None:
        return None
    try:
        return APPLE_EPOCH + dt.timedelta(seconds=float(value))
    except (TypeError, ValueError, OverflowError):
        return None


def _iso(value: dt.datetime | None) -> str | None:
    return value.isoformat() if value else None


def _fixed_tz(seconds: float, name: str | None) -> dt.timezone:
    """Build a tz from Photos' numeric offset, keeping a readable label."""
    label = None
    if name:
        # Photos writes both real zone names and "GMT+0800" style labels.
        label = name if name not in ("", "GMT") else None
    if label:
        try:
            return dt.timezone(dt.timedelta(seconds=seconds), label)
        except ValueError:
            pass
    return dt.timezone(dt.timedelta(seconds=seconds))


def _localize(utc: dt.datetime | None, offset, name: str | None) -> dt.datetime | None:
    if utc is None:
        return None
    try:
        secs = float(offset or 0)
    except (TypeError, ValueError):
        secs = 0.0
    return utc.astimezone(_fixed_tz(secs, name))


@dataclass
class Asset:
    pk: int
    uuid: str
    filename: str
    directory: str
    kind: int
    subtype: int
    width: int
    height: int
    orientation: int
    duration: float
    favorite: bool
    hidden: bool
    trashed: bool
    screenshot: bool
    edited: bool
    date_utc: dt.datetime | None
    date_local: dt.datetime | None
    added_utc: dt.datetime | None
    latitude: float | None
    longitude: float | None
    uti: str
    title: str | None
    original_filename: str | None
    file_size: int | None
    camera_make: str | None
    camera_model: str | None
    lens_model: str | None
    iso: int | None
    aperture: float | None
    shutter_speed: float | None
    focal_length: float | None
    fps: float | None
    codec: str | None
    timezone_name: str | None
    description: str | None
    path: str | None = None          # absolute original path (if present on disk)
    companion: str | None = None     # live-photo .mov path
    has_original: bool = False

    @property
    def is_video(self) -> bool:
        return self.kind == KIND_VIDEO

    @property
    def is_live(self) -> bool:
        return self.subtype == SUBTYPE_LIVE and bool(self.companion)

    @property
    def media_type(self) -> str:
        if self.is_video:
            return "video"
        if self.subtype == SUBTYPE_LIVE:
            return "live"
        if self.subtype == SUBTYPE_SCREENSHOT or self.screenshot:
            return "screenshot"
        return "photo"

    @property
    def aspect(self) -> float:
        """Width/height as displayed (orientation already applied by Photos)."""
        if self.height:
            return self.width / self.height
        return 1.0

    def to_dict(self) -> dict:
        return {
            "id": self.pk,
            "uuid": self.uuid,
            "kind": self.media_type,
            "w": self.width,
            "h": self.height,
            "orientation": self.orientation,
            "duration": round(self.duration, 2) if self.duration else 0,
            "favorite": self.favorite,
            "edited": self.edited,
            "hidden": self.hidden,
            "trashed": self.trashed,
            "date": _iso(self.date_local or self.date_utc),
            "dateUtc": _iso(self.date_utc),
            "added": _iso(self.added_utc),
            "lat": self.latitude,
            "lon": self.longitude,
            "title": self.title,
            "description": self.description,
            "name": self.original_filename or self.filename,
            "filename": self.filename,
            "uti": self.uti,
            "size": self.file_size,
            "camera": " ".join(x for x in (self.camera_make, self.camera_model) if x) or None,
            "lens": self.lens_model,
            "iso": self.iso,
            "aperture": self.aperture,
            "shutter": self.shutter_speed,
            "focal": self.focal_length,
            "fps": round(self.fps, 2) if self.fps else None,
            "codec": self.codec,
            "tz": self.timezone_name,
            "hasOriginal": self.has_original,
            "hasCompanion": bool(self.companion),
        }


@dataclass
class Album:
    pk: int
    uuid: str
    title: str
    ent: int
    kind: int
    count: int
    start: dt.datetime | None
    end: dt.datetime | None
    parent: int | None
    is_folder: bool
    children: list[int] = field(default_factory=list)

    def to_dict(self) -> dict:
        # An album can legitimately have no title stored; label it by type so
        # the sidebar never shows a bare database id.
        if self.title:
            name = self.title
        elif self.is_folder:
            name = f"Folder {self.pk}"
        else:
            name = f"Untitled Album {self.pk}"
        return {
            "id": self.pk,
            "title": name,
            "kind": "folder" if self.is_folder else "album",
            "count": self.count,
            "parent": self.parent,
            "start": _iso(self.start),
            "end": _iso(self.end),
        }


class PhotosLibrary:
    """Read-only view over one .photoslibrary bundle."""

    def __init__(self, path: str | Path):
        self.root = Path(path).resolve()
        self.db_path = self.root / "database" / "Photos.sqlite"
        if not self.root.is_dir():
            raise LibraryError(f"Not a folder: {self.root}")
        if not self.db_path.is_file():
            raise LibraryError(
                f"{self.root.name} does not look like a Photos library "
                "(database/Photos.sqlite is missing)."
            )
        self._lock = threading.Lock()
        self._conn: sqlite3.Connection | None = None
        self._copied = False
        self.assets: list[Asset] = []
        self.by_id: dict[int, Asset] = {}
        self.albums: dict[int, Album] = {}
        self.album_members: dict[int, list[int]] = {}
        self.version = 0
        self.warnings: list[str] = []
        self._cols: dict[str, set[str]] = {}
        self.reload()

    # ---------------------------------------------------------------- open

    def _connect(self) -> sqlite3.Connection:
        """Open read-only. Falls back to a private copy when the volume is
        read-only, since SQLite needs to touch the -shm file of a WAL db."""
        attempts = [
            f"file:{self.db_path.as_posix()}?mode=ro",
            f"file:{self.db_path.as_posix()}?mode=ro&immutable=1",
        ]
        last: Exception | None = None
        for uri in attempts:
            try:
                con = sqlite3.connect(uri, uri=True, timeout=10.0)
                con.execute("PRAGMA query_only=1")
                con.execute("SELECT count(*) FROM sqlite_master").fetchone()
                return con
            except sqlite3.Error as exc:  # pragma: no cover - environment specific
                last = exc
        copied = self._copy_to_cache()
        if copied is not None:
            con = sqlite3.connect(f"file:{copied.as_posix()}?mode=ro", uri=True, timeout=10.0)
            con.execute("PRAGMA query_only=1")
            self._copied = True
            self.warnings.append("Library volume is read-only; reading a cached snapshot.")
            return con
        raise LibraryError(f"Cannot open Photos database: {last}")

    def _copy_to_cache(self) -> Path | None:
        from . import config

        try:
            target = config.library_cache_dir(self.root) / "db"
            target.mkdir(parents=True, exist_ok=True)
            for suffix in ("", "-wal", "-shm"):
                src = Path(str(self.db_path) + suffix)
                if src.is_file():
                    shutil.copy2(src, target / (self.db_path.name + suffix))
            return target / self.db_path.name
        except OSError:
            return None

    def _table_columns(self, con: sqlite3.Connection, table: str) -> set[str]:
        if table not in self._cols:
            try:
                rows = con.execute(f'PRAGMA table_info("{table}")').fetchall()
                self._cols[table] = {r[1].upper() for r in rows}
            except sqlite3.Error:
                self._cols[table] = set()
        return self._cols[table]

    # --------------------------------------------------------------- load

    def reload(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
            self._conn = self._connect()
            self._cols.clear()
            self._read_version()
            self.assets = self._read_assets()
            self.by_id = {a.pk: a for a in self.assets}
            self.albums, self.album_members = self._read_albums()
            self.version += 1

    def _read_version(self) -> None:
        plist = self.root / "database" / "DataModelVersion.plist"
        self.schema_version = None
        if plist.is_file():
            try:
                text = plist.read_text(encoding="utf-8", errors="replace")
                m = re.search(r"<key>LibrarySchemaVersion</key>\s*<integer>(\d+)", text)
                if m:
                    self.schema_version = int(m.group(1))
            except OSError:
                pass

    def _read_assets(self) -> list[Asset]:
        con = self._conn
        assert con is not None
        a = self._table_columns(con, "ZASSET")
        ad = self._table_columns(con, "ZADDITIONALASSETATTRIBUTES")
        ex = self._table_columns(con, "ZEXTENDEDATTRIBUTES")
        de = self._table_columns(con, "ZASSETDESCRIPTION")

        def col(table_cols: set[str], name: str, table: str) -> str:
            return f'{table}."{name}"' if name in table_cols else "NULL"

        joins = [
            'LEFT JOIN ZADDITIONALASSETATTRIBUTES ad ON ad.ZASSET = a.Z_PK'
            if "ZASSET" in ad
            else "",
            "LEFT JOIN ZEXTENDEDATTRIBUTES ex ON ex.ZASSET = a.Z_PK" if "ZASSET" in ex else "",
            "LEFT JOIN ZASSETDESCRIPTION d ON d.ZASSETATTRIBUTES = ad.Z_PK"
            if ("ZASSETATTRIBUTES" in de and "ZASSET" in ad)
            else "",
        ]

        sql = f"""
            SELECT
                a.Z_PK, a.ZUUID, a.ZFILENAME, a.ZDIRECTORY,
                a.ZKIND, {col(a,'ZKINDSUBTYPE','a')} AS SUBTYPE,
                {col(a,'ZWIDTH','a')} AS W, {col(a,'ZHEIGHT','a')} AS H,
                {col(a,'ZORIENTATION','a')} AS ORIENT,
                {col(a,'ZDURATION','a')} AS DUR,
                {col(a,'ZFAVORITE','a')} AS FAV,
                {col(a,'ZHIDDEN','a')} AS HID,
                {col(a,'ZTRASHEDSTATE','a')} AS TRASH,
                {col(a,'ZISDETECTEDSCREENSHOT','a')} AS SHOT,
                {col(a,'ZADJUSTMENTSSTATE','a')} AS ADJ,
                {col(a,'ZDATECREATED','a')} AS CREATED,
                {col(a,'ZADDEDDATE','a')} AS ADDED,
                {col(a,'ZLATITUDE','a')} AS LAT,
                {col(a,'ZLONGITUDE','a')} AS LON,
                {col(a,'ZUNIFORMTYPEIDENTIFIER','a')} AS UTI,
                {col(ad,'ZTITLE','ad')} AS TITLE,
                {col(ad,'ZORIGINALFILENAME','ad')} AS ORIGNAME,
                {col(ad,'ZORIGINALFILESIZE','ad')} AS FSIZE,
                {col(ad,'ZTIMEZONEOFFSET','ad')} AS TZOFF,
                {col(ad,'ZTIMEZONENAME','ad')} AS TZNAME,
                {col(ex,'ZCAMERAMAKE','ex')} AS MAKE,
                {col(ex,'ZCAMERAMODEL','ex')} AS MODEL,
                {col(ex,'ZLENSMODEL','ex')} AS LENS,
                {col(ex,'ZISO','ex')} AS ISO,
                {col(ex,'ZAPERTURE','ex')} AS APERTURE,
                {col(ex,'ZSHUTTERSPEED','ex')} AS SHUTTER,
                {col(ex,'ZFOCALLENGTH','ex')} AS FOCAL,
                {col(ex,'ZFPS','ex')} AS FPS,
                {col(ex,'ZCODEC','ex')} AS CODEC,
                {col(de,'ZLONGDESCRIPTION','d')} AS DESCR
            FROM ZASSET a
            {' '.join(j for j in joins if j)}
        """
        try:
            rows = con.execute(sql).fetchall()
        except sqlite3.Error as exc:
            raise LibraryError(f"Unexpected Photos schema: {exc}") from exc

        out: list[Asset] = []
        for r in rows:
            (pk, uuid, filename, directory, kind, subtype, w, h, orient, dur, fav, hid,
             trash, shot, adj, created, added, lat, lon, uti, title, origname, fsize,
             tzoff, tzname, make, model, lens, iso, aperture, shutter, focal, fps,
             codec, descr) = r

            directory = directory or ""
            filename = filename or ""
            created_dt = _apple_time(created)
            local_dt = _localize(created_dt, tzoff, tzname)

            path = None
            if filename:
                candidate = self.root / "originals" / directory / filename
                if candidate.is_file():
                    path = str(candidate)

            asset = Asset(
                pk=int(pk),
                uuid=uuid or "",
                filename=filename,
                directory=directory,
                kind=int(kind or 0),
                subtype=int(subtype or 0),
                width=int(w or 0),
                height=int(h or 0),
                orientation=int(orient or 1),
                duration=float(dur or 0.0),
                favorite=bool(fav),
                hidden=bool(hid),
                trashed=bool(trash),
                screenshot=bool(shot),
                edited=bool(adj),
                date_utc=created_dt,
                date_local=local_dt,
                added_utc=_apple_time(added),
                latitude=float(lat) if lat not in (None, -180.0) else None,
                longitude=float(lon) if lon not in (None, -180.0) else None,
                uti=uti or "",
                title=(title or None) or None,
                original_filename=(origname or None) or None,
                file_size=int(fsize) if fsize else None,
                camera_make=(make or None) or None,
                camera_model=(model or None) or None,
                lens_model=(lens or None) or None,
                iso=int(iso) if iso else None,
                aperture=float(aperture) if aperture else None,
                shutter_speed=float(shutter) if shutter else None,
                focal_length=float(focal) if focal else None,
                fps=float(fps) if fps else None,
                codec=(codec or None) or None,
                timezone_name=(tzname or None) or None,
                description=(descr or None) or None,
                path=path,
                has_original=path is not None,
            )
            if asset.subtype == SUBTYPE_LIVE:
                asset.companion = self._find_companion(asset)
            out.append(asset)

        # Newest first, matching Photos' own default ordering.
        out.sort(key=lambda x: (x.date_utc or dt.datetime.min.replace(tzinfo=dt.timezone.utc)), reverse=True)
        return out

    def _find_companion(self, asset: Asset) -> str | None:
        """Locate the .mov that makes a still a Live Photo."""
        if not asset.uuid:
            return None
        base = self.root / "originals" / asset.directory
        if not base.is_dir():
            return None
        stem = Path(asset.filename).stem if asset.filename else asset.uuid
        try:
            names = os.listdir(base)
        except OSError:
            return None
        pattern = re.compile(rf"^{re.escape(stem)}(_\d+)?\.(mov|MOV)$")
        for name in names:
            if name.startswith("._"):
                continue
            if pattern.match(name):
                return str(base / name)
        return None

    # -------------------------------------------------------------- albums

    def _read_albums(self) -> tuple[dict[int, Album], dict[int, list[int]]]:
        con = self._conn
        assert con is not None
        cols = self._table_columns(con, "ZGENERICALBUM")
        if not cols:
            return {}, {}

        title = 'ZTITLE' if 'ZTITLE' in cols else 'NULL'
        ent = 'Z_ENT' if 'Z_ENT' in cols else str(ENT_ALBUM)
        kind = 'ZKIND' if 'ZKIND' in cols else '0'
        parent = 'ZPARENTFOLDER' if 'ZPARENTFOLDER' in cols else 'NULL'
        start = 'ZSTARTDATE' if 'ZSTARTDATE' in cols else 'NULL'
        end = 'ZENDDATE' if 'ZENDDATE' in cols else 'NULL'
        uuid = 'ZUUID' if 'ZUUID' in cols else 'NULL'
        cached = 'ZCACHEDCOUNT' if 'ZCACHEDCOUNT' in cols else '0'
        trashed = 'ZTRASHEDSTATE' if 'ZTRASHEDSTATE' in cols else '0'

        try:
            rows = con.execute(
                f"SELECT Z_PK,{title},{ent},{kind},{parent},{start},{end},{uuid},{cached},{trashed} "
                f"FROM ZGENERICALBUM"
            ).fetchall()
        except sqlite3.Error:
            return {}, {}

        members = self._album_membership(con)

        albums: dict[int, Album] = {}
        for pk, t, e, k, par, st, en, u, cc, tr in rows:
            if tr:
                continue
            if k in INTERNAL_ALBUM_KINDS:
                continue
            e = int(e or ENT_ALBUM)
            count = len(members.get(pk, ()))
            titled = bool((t or "").strip())
            if not titled:
                # Untitled rows are almost always Apple scaffolding: empty
                # folders, import sessions, fetching placeholders. Only keep an
                # untitled row when it demonstrably holds assets (a real album
                # whose title simply was never set).
                if count == 0:
                    continue
            albums[int(pk)] = Album(
                pk=int(pk),
                uuid=u or "",
                title=(t or "").strip(),
                ent=e,
                kind=int(k or 0),
                count=count,
                start=_apple_time(st),
                end=_apple_time(en),
                parent=int(par) if par else None,
                is_folder=(e == ENT_FOLDER),
            )

        # Link folders to children for the sidebar tree.
        for al in albums.values():
            if al.parent and al.parent in albums:
                albums[al.parent].children.append(al.pk)

        # A folder's own count is the union of everything nested inside it,
        # since Photos stores folders as containers with no direct members.
        for al in albums.values():
            if al.is_folder:
                al.count = len(self._folder_ids(albums, members, al.pk))
        return albums, members

    def _folder_ids(self, albums: dict[int, "Album"], members: dict[int, list[int]], pk: int) -> set[int]:
        ids = set(members.get(pk, ()))
        album = albums.get(pk)
        if album:
            for child in album.children:
                ids |= self._folder_ids(albums, members, child)
        return ids

    def _album_membership(self, con: sqlite3.Connection) -> dict[int, list[int]]:
        """Discover the album<->asset join tables instead of hardcoding them.

        Core Data names them ``Z_<entityId>ASSETS`` with an ``Z_<entityId>ALBUMS``
        column; the entity id differs between Photos releases.
        """
        found: dict[int, list[int]] = {}
        try:
            names = [r[0] for r in con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'Z\\_%ASSETS' ESCAPE '\\'"
            ).fetchall()]
        except sqlite3.Error:
            return found
        for name in names:
            try:
                info = con.execute(f'PRAGMA table_info("{name}")').fetchall()
            except sqlite3.Error:
                continue
            cols = [c[1].upper() for c in info]
            # Core Data adds Z_FOK_* shadow columns that also end in ASSETS.
            # The real pair is the shortest matching name, without the FOK prefix.
            album_col = next(
                (c for c in cols if c.endswith("ALBUMS") and not c.startswith("Z_FOK_")),
                None,
            )
            asset_col = next(
                (c for c in cols if c.endswith("ASSETS") and not c.startswith("Z_FOK_")),
                None,
            )
            if not album_col or not asset_col:
                continue
            try:
                for album_pk, asset_pk in con.execute(
                    f'SELECT "{album_col}","{asset_col}" FROM "{name}"'
                ):
                    if album_pk is None or asset_pk is None:
                        continue
                    found.setdefault(int(album_pk), []).append(int(asset_pk))
            except sqlite3.Error:
                continue
        return found

    # ------------------------------------------------------------ queries

    def album_asset_ids(self, album_pk: int) -> set[int]:
        ids = set(self.album_members.get(album_pk, ()))
        # A folder includes everything in its child albums.
        album = self.albums.get(album_pk)
        if album and album.is_folder:
            for child in album.children:
                ids |= self.album_asset_ids(child)
        return ids

    def stats(self) -> dict:
        visible = [a for a in self.assets if not a.trashed and not a.hidden]
        years: dict[str, int] = {}
        for a in visible:
            d = a.date_local or a.date_utc
            if d:
                years[str(d.year)] = years.get(str(d.year), 0) + 1
        return {
            "assets": len(self.assets),
            "visible": len(visible),
            "photos": sum(1 for a in visible if a.media_type == "photo"),
            "videos": sum(1 for a in visible if a.media_type == "video"),
            "live": sum(1 for a in visible if a.media_type == "live"),
            "screenshots": sum(1 for a in visible if a.media_type == "screenshot"),
            "favorites": sum(1 for a in visible if a.favorite),
            "edited": sum(1 for a in visible if a.edited),
            "missingOriginals": sum(1 for a in visible if not a.has_original),
            "sizeBytes": sum(a.file_size or 0 for a in visible),
            "years": dict(sorted(years.items(), reverse=True)),
            "schemaVersion": self.schema_version,
        }

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                except sqlite3.Error:
                    pass
                self._conn = None
