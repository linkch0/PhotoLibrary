"""Fallback-path tests: libraries where Photos left no derivatives behind.

Some libraries (older ones, or ones whose resources/derivatives were pruned)
have no thumbnails at all, so every tile has to come from decoding the
original - HEIC included. This measures that path and checks it produces
correctly oriented images.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PIL import Image

from app import media
from app.library import PhotosLibrary

LIB = Path(sys.argv[1]) if len(sys.argv) > 1 else None
EMPTY_ROOT = Path(tempfile.gettempdir()) / "photos_reader_fallback_test"


def run() -> int:
    if LIB is None:
        print(__doc__)
        print(f"usage: python {Path(__file__).name} <library>")
        return 2
    lib = PhotosLibrary(LIB)
    photos = [a for a in lib.assets if a.media_type == "photo" and a.uti == "public.heic"][:5]
    print(f"decoding {len(photos)} HEIC originals with derivatives disabled")

    start = time.time()
    for a in photos:
        with patch("app.media.tiny_derivative", return_value=None), \
             patch("app.media.preview_derivative", return_value=None):
            path, ctype = media.thumbnail(a, EMPTY_ROOT, 360)
    elapsed = time.time() - start
    print(f"  {elapsed:.2f}s total ({elapsed / len(photos) * 1000:.0f} ms each)")
    failures = 0
    for a in photos:
        with patch("app.media.tiny_derivative", return_value=None), \
             patch("app.media.preview_derivative", return_value=None):
            path, _ = media.thumbnail(a, EMPTY_ROOT, 360)
        im = Image.open(path)
        # DB dimensions are display dimensions; the decoded tile must agree.
        db_portrait = a.height > a.width
        tile_portrait = im.size[1] > im.size[0]
        match = db_portrait == tile_portrait
        if not match:
            failures += 1
        print(f"  {a.pk} db={a.width}x{a.height} tile={im.size} "
              f"{'ok' if match else 'MISMATCH'}")

    # Also confirm a video poster frame can be pulled when no derivative exists.
    vids = [a for a in lib.assets if a.is_video][:2]
    print(f"extracting {len(vids)} video poster frames")
    for a in vids:
        start = time.time()
        with patch("app.media.tiny_derivative", return_value=None), \
             patch("app.media.preview_derivative", return_value=None):
            path, _ = media.thumbnail(a, EMPTY_ROOT, 360)
        im = Image.open(path)
        print(f"  {a.pk} {im.size} in {time.time() - start:.2f}s")

    print(f"\nfailures: {failures}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(run())
