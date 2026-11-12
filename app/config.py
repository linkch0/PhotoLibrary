"""Filesystem locations and tunables.

Nothing in here ever writes inside a .photoslibrary bundle: all generated
artifacts (thumbnails, transcoded video) live in a separate cache directory.
"""

from __future__ import annotations

import os
import hashlib
from pathlib import Path

APP_NAME = "MacPhotosReader"

# Root for generated artifacts. Kept off the library drive by default so
# browsing a library on a read-only or network volume still works.
def _default_cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/.cache")
    return Path(base) / APP_NAME


CACHE_ROOT = Path(os.environ.get("PHOTOS_READER_CACHE") or _default_cache_root())
THUMB_DIR = CACHE_ROOT / "thumbs"
VIDEO_DIR = CACHE_ROOT / "video"
PREVIEW_DIR = CACHE_ROOT / "previews"


def library_cache_dir(library_path: str | Path) -> Path:
    """Stable per-library cache folder keyed by absolute path."""
    key = hashlib.sha1(str(Path(library_path).resolve()).encode("utf-8")).hexdigest()[:16]
    d = CACHE_ROOT / "libs" / key
    d.mkdir(parents=True, exist_ok=True)
    return d


def ensure_dirs() -> None:
    for d in (CACHE_ROOT, THUMB_DIR, VIDEO_DIR, PREVIEW_DIR):
        d.mkdir(parents=True, exist_ok=True)


# Thumbnail sizes (longest edge, px) that can be requested from the API.
GRID_SIZES = {"xs": 240, "s": 360, "m": 560, "l": 900}
PREVIEW_MAX = 2560

# Directories searched when auto-discovering libraries on startup.
# Override with the PHOTOS_READER_ROOTS env var (os.pathsep-separated) to point
# at wherever your libraries actually live.
SEARCH_ROOTS = [
    *[p for p in os.environ.get("PHOTOS_READER_ROOTS", "").split(os.pathsep) if p],
    str(Path.home() / "Pictures"),
]

EXTERNAL_RESOURCE_PREFIXES = ("_3", "_5")  # live-photo video, adjusted video
