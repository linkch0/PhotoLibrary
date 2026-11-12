"""HTTP API over one or more Photos libraries.

Browsing state (which library is open) lives in memory; the interesting logic
is in ``library`` and ``media``.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config, media
from .library import Album, Asset, LibraryError, PhotosLibrary

STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Mac Photos Library Reader", version="1.0.0")

_state_lock = threading.Lock()
_lib: PhotosLibrary | None = None
_recent: list[str] = []


# --------------------------------------------------------------- helpers

def get_library() -> PhotosLibrary:
    if _lib is None:
        raise HTTPException(409, "No library is open yet.")
    return _lib


def _library_token(lib: PhotosLibrary) -> str:
    """Short stable hash of the library identity.

    Asset ids are per-library database primary keys, so ``/api/thumb/846`` means
    different photos in different libraries. Media is served with a long-lived
    immutable cache, so the token is what forces the browser to fetch the right
    bytes after a switch instead of reusing the previous library's pixels.
    """
    import hashlib

    return hashlib.sha1(str(lib.root).encode("utf-8")).hexdigest()[:10]


# Decoding HEIC is CPU-heavy, and the browser requests dozens of thumbnails at
# once. A small semaphore keeps a 24MP decode from starving every other
# request, while still using several cores.
_DECODE_SLOTS = threading.Semaphore(max(2, min(6, (os.cpu_count() or 4) - 1)))


def _with_decode_slot(fn):
    with _DECODE_SLOTS:
        return fn()


def _split_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.isdigit():
            out.add(int(part))
    return out


def _matches(asset: Asset, *, kind: str, query: str, year: str) -> bool:
    if kind and kind != "all":
        if kind == "photo" and asset.media_type != "photo":
            return False
        if kind == "video" and asset.media_type != "video":
            return False
        if kind == "live" and asset.media_type != "live":
            return False
        if kind == "screenshot" and asset.media_type != "screenshot":
            return False
        if kind == "favorite" and not asset.favorite:
            return False
    if year:
        d = asset.date_local or asset.date_utc
        if not d or str(d.year) != year:
            return False
    if query:
        q = query.lower().strip()
        haystack = " ".join(filter(None, [
            asset.original_filename, asset.filename, asset.title,
            asset.description, asset.camera_model, asset.camera_make,
            asset.lens_model, asset.timezone_name,
        ])).lower()
        if q not in haystack:
            return False
    return True


def _sort_assets(assets: list[Asset], sort: str) -> list[Asset]:
    if sort == "oldest":
        return sorted(assets, key=lambda a: a.date_utc or dt.datetime.min.replace(tzinfo=dt.timezone.utc))
    if sort == "added":
        return sorted(assets, key=lambda a: a.added_utc or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)
    if sort == "name":
        return sorted(assets, key=lambda a: (a.original_filename or a.filename).lower())
    if sort == "size":
        return sorted(assets, key=lambda a: a.file_size or 0, reverse=True)
    return sorted(assets, key=lambda a: a.date_utc or dt.datetime.min.replace(tzinfo=dt.timezone.utc), reverse=True)


# ----------------------------------------------------------------- routes

@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "library": str(_lib.root) if _lib else None}


@app.get("/api/recent")
def recent() -> dict:
    return {"libraries": _recent}


@app.post("/api/library/open")
async def open_library(request: Request) -> dict:
    """Open a library by path, or via the native folder picker if omitted."""
    global _lib
    body: dict[str, Any] = {}
    try:
        body = await request.json()
    except Exception:
        pass
    path = (body or {}).get("path")

    if not path:
        path = _pick_folder()
        if not path:
            raise HTTPException(400, "No folder selected.")

    path = _normalize_library_path(path)
    try:
        lib = PhotosLibrary(path)
    except LibraryError as exc:
        raise HTTPException(400, str(exc)) from exc

    with _state_lock:
        if _lib is not None:
            _lib.close()
        _lib = lib
        _remember(str(lib.root))

    return {
        "path": str(lib.root),
        "name": lib.root.name.replace(".photoslibrary", ""),
        "token": _library_token(lib),
        "stats": lib.stats(),
        "warnings": lib.warnings,
        "albums": [a.to_dict() for a in lib.albums.values()],
    }


def _normalize_library_path(raw: str) -> Path:
    p = Path(raw.strip().strip('"')).expanduser()
    if p.is_file():
        # Someone picked database/Photos.sqlite directly.
        if p.name == "Photos.sqlite" and p.parent.name == "database":
            p = p.parent.parent
    if not p.is_dir():
        raise HTTPException(400, f"Not a folder: {p}")
    return p


def _pick_folder() -> str | None:
    """Native Windows folder picker (tkinter ships with CPython)."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception:
        return None
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    try:
        chosen = filedialog.askdirectory(title="Select a .photoslibrary folder")
    finally:
        root.destroy()
    return chosen or None


def _remember(path: str) -> None:
    if path in _recent:
        _recent.remove(path)
    _recent.insert(0, path)
    del _recent[40:]


@app.get("/api/library")
def library_info() -> dict:
    lib = get_library()
    return {
        "path": str(lib.root),
        "name": lib.root.name.replace(".photoslibrary", ""),
        "token": _library_token(lib),
        "stats": lib.stats(),
        "warnings": lib.warnings,
        "albums": [a.to_dict() for a in lib.albums.values()],
    }


@app.post("/api/library/reload")
def reload_library() -> dict:
    lib = get_library()
    lib.reload()
    return {"ok": True, "stats": lib.stats()}


@app.get("/api/assets")
def list_assets(
    album: str | None = None,
    kind: str = "all",
    q: str = "",
    year: str = "",
    sort: str = "newest",
    offset: int = 0,
    limit: int = Query(200, ge=1, le=2000),
    include_hidden: bool = False,
    include_trashed: bool = False,
) -> dict:
    lib = get_library()
    albums = _split_ids(album)

    if albums:
        allowed: set[int] = set()
        for a in albums:
            allowed |= lib.album_asset_ids(a)
        pool = [lib.by_id[i] for i in allowed if i in lib.by_id]
    else:
        pool = list(lib.assets)

    pool = [
        a for a in pool
        if (include_hidden or not a.hidden)
        and (include_trashed or not a.trashed)
        and _matches(a, kind=kind, query=q, year=year)
    ]
    pool = _sort_assets(pool, sort)

    total = len(pool)
    page = pool[offset: offset + limit]
    return {
        "total": total,
        "offset": offset,
        "limit": limit,
        "items": [a.to_dict() for a in page],
    }


@app.get("/api/assets/{asset_id}")
def asset_detail(asset_id: int) -> dict:
    lib = get_library()
    asset = lib.by_id.get(asset_id)
    if asset is None:
        raise HTTPException(404, "No such asset")
    data = asset.to_dict()
    data.update({
        "path": asset.path,
        "companion": asset.companion,
        "playable": asset.is_video or asset.is_live,
        "folders": _folders_for(lib, asset_id),
    })
    return data


def _folders_for(lib: PhotosLibrary, asset_id: int) -> list[str]:
    names = []
    for pk, ids in lib.album_members.items():
        if asset_id in ids:
            alb = lib.albums.get(pk)
            if alb and alb.title:
                names.append(alb.title)
    return sorted(set(names))


@app.get("/api/thumb/{asset_id}")
def thumb(asset_id: int, size: str = "s") -> FileResponse:
    lib = get_library()
    asset = lib.by_id.get(asset_id)
    if asset is None:
        raise HTTPException(404, "No such asset")
    px = config.GRID_SIZES.get(size, config.GRID_SIZES["s"])
    path, ctype = _with_decode_slot(lambda: media.thumbnail(asset, lib.root, px))
    return FileResponse(path, media_type=ctype, headers=_cache_headers(path))


@app.get("/api/preview/{asset_id}")
def preview(asset_id: int) -> FileResponse:
    lib = get_library()
    asset = lib.by_id.get(asset_id)
    if asset is None:
        raise HTTPException(404, "No such asset")
    path, ctype = _with_decode_slot(lambda: media.preview(asset, lib.root))
    return FileResponse(path, media_type=ctype, headers=_cache_headers(path))


def _cache_headers(path: Path) -> dict[str, str]:
    return {"Cache-Control": "public, max-age=31536000, immutable"}


@app.get("/api/video/{asset_id}")
def video(asset_id: int) -> FileResponse:
    lib = get_library()
    asset = lib.by_id.get(asset_id)
    if asset is None:
        raise HTTPException(404, "No such asset")
    result = media.playable_video(asset, lib.root)
    if result is None:
        raise HTTPException(415, "No playable video for this asset (ffmpeg missing?)")
    path, ctype = result
    return FileResponse(path, media_type=ctype)


@app.get("/api/original/{asset_id}")
def original(asset_id: int, download: bool = False) -> FileResponse:
    lib = get_library()
    asset = lib.by_id.get(asset_id)
    if asset is None or not asset.path:
        raise HTTPException(404, "Original file is not present in this library")
    name = asset.original_filename or asset.filename
    return FileResponse(
        asset.path,
        media_type=None,
        filename=name if download else None,
        content_disposition_type="attachment" if download else "inline",
    )


@app.post("/api/export")
async def export_assets(request: Request) -> dict:
    """Copy selected originals out to a folder, preserving names."""
    lib = get_library()
    try:
        body = await request.json()
    except Exception:
        body = {}
    body = body or {}

    dest_raw = body.get("dest")
    if dest_raw:
        dest = Path(str(dest_raw)).expanduser()
    else:
        picked = _pick_folder()
        if not picked:
            raise HTTPException(400, "No destination folder selected.")
        dest = Path(picked)

    try:
        dest.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise HTTPException(400, f"Cannot use destination: {exc}") from exc

    album = body.get("album")
    kind = body.get("kind", "all")
    q = body.get("q", "")
    year = body.get("year", "")

    albums = _split_ids(str(album) if album else None)
    if albums:
        allowed: set[int] = set()
        for a in albums:
            allowed |= lib.album_asset_ids(a)
        pool = [lib.by_id[i] for i in allowed if i in lib.by_id]
    else:
        pool = list(lib.assets)
    pool = [a for a in pool if not a.trashed and _matches(a, kind=kind, query=q, year=year)]

    copied, skipped = 0, 0
    for a in pool:
        if not a.path or not Path(a.path).is_file():
            skipped += 1
            continue
        base = a.original_filename or a.filename
        out = _unique_path(dest / base)
        try:
            import shutil
            shutil.copy2(a.path, out)
            copied += 1
        except OSError:
            skipped += 1
    return {"copied": copied, "skipped": skipped, "destination": str(dest)}


def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    for i in range(1, 10000):
        cand = p.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
    return p


@app.get("/api/timeline")
def timeline(album: str | None = None) -> dict:
    """Buckets for the scroll-timeline scrubber: year -> month -> count."""
    lib = get_library()
    albums = _split_ids(album)
    if albums:
        allowed: set[int] = set()
        for a in albums:
            allowed |= lib.album_asset_ids(a)
        pool = [lib.by_id[i] for i in allowed if i in lib.by_id]
    else:
        pool = [a for a in lib.assets if not a.trashed and not a.hidden]

    buckets: dict[str, dict[str, int]] = {}
    for a in pool:
        d = a.date_local or a.date_utc
        if not d:
            continue
        y = buckets.setdefault(str(d.year), {})
        m = f"{d.month:02d}"
        y[m] = y.get(m, 0) + 1
    return {"years": {y: dict(sorted(m.items())) for y, m in sorted(buckets.items(), reverse=True)}}


# ------------------------------------------------------------ static app

@app.middleware("http")
async def no_cache_shell(request: Request, call_next):
    """Keep the UI itself out of the browser cache.

    Thumbnails are immutable and cached aggressively; the HTML/JS/CSS shell is
    the opposite, so a code change is picked up on the next refresh.
    """
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.endswith((".html", ".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, must-revalidate"
    return response


if STATIC_DIR.is_dir():
    app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
