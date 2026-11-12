"""Turning library records into pixels the browser can actually show.

Display strategy, cheapest first:

* ``resources/derivatives/masters/<D>/<UUID>_4_5005_c.jpeg`` — tiny JPEG that
  Photos keeps for every asset. Used for the grid.
* ``resources/derivatives/<D>/<UUID>_1_102_o.jpeg`` — larger JPEG, edits applied.
  Only written for some assets, so it is an optimisation, not a guarantee.
* ``resources/renders/<D>/<UUID>_1_201_a.*`` — full-size render of an *edited*
  asset. Only present when the photo has adjustments; preferred over the
  original for those.
* ``originals/<D>/<f>`` — the real file. HEIC/HEIF needs a decoder, so those are
  converted on demand and cached as JPEG.

The derivatives are already rotated to display orientation, which is why the
DB's ZWIDTH/ZHEIGHT (display dimensions) match them and not the raw pixels.
Anything we decode ourselves goes through EXIF transpose to match.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import threading
from pathlib import Path

from PIL import Image, ImageOps

from . import config
from .library import Asset

try:  # HEIC support is optional at runtime but bundled in requirements.
    import pillow_heif

    pillow_heif.register_heif_opener()
    HEIF_OK = True
except Exception:  # pragma: no cover
    HEIF_OK = False

Image.MAX_IMAGE_PIXELS = None  # trust our own libraries

_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(key: str) -> threading.Lock:
    with _LOCKS_GUARD:
        if key not in _LOCKS:
            _LOCKS[key] = threading.Lock()
        return _LOCKS[key]


RENDER_SUFFIXES = ("_1_201_a.heic", "_1_201_a.jpeg", "_1_201_a.jpg", "_1_201_a.png")
PREVIEW_SUFFIX = "_1_102_o.jpeg"
TINY_SUFFIX = "_4_5005_c.jpeg"


def tiny_derivative(asset: Asset, root: Path) -> Path | None:
    p = root / "resources" / "derivatives" / "masters" / asset.directory / f"{asset.uuid}{TINY_SUFFIX}"
    return p if p.is_file() else None


def preview_derivative(asset: Asset, root: Path) -> Path | None:
    p = root / "resources" / "derivatives" / asset.directory / f"{asset.uuid}{PREVIEW_SUFFIX}"
    if p.is_file():
        return p
    for sub in ("_1_101_o.jpeg", "_1_105_c.jpeg", "_1_100_o.jpeg"):
        alt = root / "resources" / "derivatives" / asset.directory / f"{asset.uuid}{sub}"
        if alt.is_file():
            return alt
    return None


def render_of_edited(asset: Asset, root: Path) -> Path | None:
    """Full-size render of an edited asset (already has adjustments baked in)."""
    if not asset.edited:
        return None
    base = root / "resources" / "renders" / asset.directory
    for suffix in RENDER_SUFFIXES:
        p = base / f"{asset.uuid}{suffix}"
        if p.is_file():
            return p
    return None


def _cache_path(asset: Asset, size: int, variant: str) -> Path:
    d = config.THUMB_DIR / variant
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{asset.uuid}_{size}.jpg"


def _decode(path: Path, box: int | None = None) -> Image.Image:
    """Decode to RGB, optionally downscaling before conversion.

    Downscaling first matters: a 24MP HEIC is ~70 MB as RGB, so shrinking
    before the colour conversion is both faster and lighter.
    """
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    if box:
        im.thumbnail((box, box), Image.LANCZOS)
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        conv = im.convert("RGBA")
        bg.paste(conv, mask=conv.split()[-1])
        return bg
    return im if im.mode == "RGB" else im.convert("RGB")


def _fit(im: Image.Image, box: int) -> Image.Image:
    im.thumbnail((box, box), Image.LANCZOS)
    return im


def _save_jpeg(im: Image.Image, dest: Path, quality: int = 86) -> None:
    tmp = dest.with_suffix(".tmp")
    im.save(tmp, "JPEG", quality=quality, optimize=True, progressive=True)
    os.replace(tmp, dest)


def _extract_video_frame(asset: Asset, root: Path, box: int = 640) -> Image.Image | None:
    """Last-resort poster frame when Photos left no derivative behind."""
    src = Path(asset.path) if asset.path else None
    if not src or not src.is_file() or not shutil.which("ffmpeg"):
        return None
    probe = config.CACHE_ROOT / "video_frames"
    probe.mkdir(parents=True, exist_ok=True)
    out = probe / f"{asset.uuid}_{box}.jpg"
    seek = min(max(asset.duration * 0.1, 0.0), 3.0) if asset.duration else 0.0
    cmd = [
        "ffmpeg", "-v", "error", "-y", "-ss", f"{seek:.2f}", "-i", str(src),
        "-frames:v", "1", "-vf", f"scale='min({max(box, 320)},iw)':-2", "-q:v", "3", str(out),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=45)
    except (subprocess.SubprocessError, OSError):
        return None
    if not out.is_file():
        return None
    try:
        return _decode(out)
    except Exception:
        return None


def thumbnail(asset: Asset, root: Path, size: int) -> tuple[Path, str]:
    """Return (path, content_type) for a grid thumbnail, generating if needed.

    ``size`` is the longest edge in px.
    """
    cached = _cache_path(asset, size, "grid")
    if cached.is_file():
        return cached, "image/jpeg"

    lock = _lock_for(f"grid:{asset.uuid}:{size}")
    with lock:
        if cached.is_file():
            return cached, "image/jpeg"

        # 1. Photos' own tiny derivative - fastest path, already oriented.
        src = tiny_derivative(asset, root)
        if src is not None:
            try:
                im = _decode(src, size)
                _save_jpeg(_fit(im, size), cached)
                return cached, "image/jpeg"
            except Exception:
                pass

        # 2. Slightly larger derivative, still cheap.
        src = preview_derivative(asset, root)
        if src is not None:
            try:
                im = _decode(src, size)
                _save_jpeg(_fit(im, size), cached)
                return cached, "image/jpeg"
            except Exception:
                pass

        # 3. Video poster frame.
        if asset.is_video:
            im = _extract_video_frame(asset, root, size)
            if im is not None:
                _save_jpeg(_fit(im, size), cached)
                return cached, "image/jpeg"

        # 4. Decode the original (HEIC goes through pillow-heif).
        render = render_of_edited(asset, root)
        for candidate in (render, Path(asset.path) if asset.path else None):
            if not candidate or not candidate.is_file():
                continue
            try:
                im = _decode(candidate, size)
                _save_jpeg(_fit(im, size), cached)
                return cached, "image/jpeg"
            except Exception:
                continue

    return placeholder(size), "image/jpeg"


def preview(asset: Asset, root: Path, max_edge: int = config.PREVIEW_MAX) -> tuple[Path, str]:
    """Best available full look at one asset (edits applied when possible)."""
    # Edited assets: the render already includes the adjustments.
    render = render_of_edited(asset, root)
    if render is not None:
        if render.suffix.lower() in (".jpg", ".jpeg") and _safe_size(render) < 4_500_000:
            return render, "image/jpeg"
        cached = _cache_path(asset, max_edge, "preview")
        if cached.is_file():
            return cached, "image/jpeg"
        lock = _lock_for(f"prev:{asset.uuid}")
        with lock:
            if cached.is_file():
                return cached, "image/jpeg"
            try:
                im = _decode(render)
                _save_jpeg(_fit(im, max_edge), cached, quality=90)
                return cached, "image/jpeg"
            except Exception:
                pass

    prev = preview_derivative(asset, root)
    if prev is not None and _safe_size(prev) >= 200_000 and _within(prev, max_edge):
        return prev, "image/jpeg"

    cached = _cache_path(asset, max_edge, "preview")
    if cached.is_file():
        return cached, "image/jpeg"
    lock = _lock_for(f"prev:{asset.uuid}")
    with lock:
        if cached.is_file():
            return cached, "image/jpeg"
        if prev is not None:
            try:
                im = _decode(prev)
                _save_jpeg(_fit(im, max_edge), cached, quality=90)
                return cached, "image/jpeg"
            except Exception:
                pass
        if asset.path and Path(asset.path).is_file():
            suffix = Path(asset.path).suffix.lower()
            if suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
                return Path(asset.path), _mime_for(suffix)
            try:
                im = _decode(Path(asset.path))
                _save_jpeg(_fit(im, max_edge), cached, quality=90)
                return cached, "image/jpeg"
            except Exception:
                pass
    return placeholder(max_edge), "image/jpeg"


def _safe_size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0


def _within(path: Path, max_edge: int) -> bool:
    """True when the file is already small enough to serve untouched."""
    try:
        with Image.open(path) as im:
            return max(im.size) <= max_edge
    except Exception:
        return False


def _mime_for(suffix: str) -> str:
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".gif": "image/gif", ".webp": "image/webp", ".heic": "image/heic",
        ".heif": "image/heif",
    }.get(suffix, "application/octet-stream")


def placeholder(size: int = 360) -> Path:
    """Neutral tile for assets whose pixels cannot be read at all."""
    d = config.THUMB_DIR / "placeholder"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"ph_{size}.jpg"
    if p.is_file():
        return p
    im = Image.new("RGB", (size, size), (28, 28, 30))
    _save_jpeg(im, p, quality=70)
    return p


# ------------------------------------------------------------------ video

BROWSER_SAFE = (".mp4",)


def playable_video(asset: Asset, root: Path) -> tuple[Path, str] | None:
    """Return something Chrome/Edge can play for this asset.

    Photos stores HEVC in .mov, which most Windows browsers cannot decode, so
    those get transcoded to H.264 once and cached.
    """
    src: Path | None = None
    if asset.is_video and asset.path:
        src = Path(asset.path)
    elif asset.is_live and asset.companion:
        src = Path(asset.companion)
    if src is None or not src.is_file():
        return None

    if src.suffix.lower() in BROWSER_SAFE and _is_h264(src):
        return src, "video/mp4"

    cache_dir = config.VIDEO_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = cache_dir / f"{asset.uuid}.mp4"
    if out.is_file():
        return out, "video/mp4"

    lock = _lock_for(f"vid:{asset.uuid}")
    with lock:
        if out.is_file():
            return out, "video/mp4"
        if not shutil.which("ffmpeg"):
            return None
        tmp = out.with_suffix(".part.mp4")
        cmd = [
            "ffmpeg", "-v", "error", "-y", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
            "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k",
            "-movflags", "+faststart", str(tmp),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, timeout=1800)
        except (subprocess.SubprocessError, OSError):
            if tmp.exists():
                tmp.unlink(missing_ok=True)
            return None
        os.replace(tmp, out)
    return out, "video/mp4"


def _is_h264(path: Path) -> bool:
    if not shutil.which("ffprobe"):
        return False
    try:
        res = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        return res.stdout.strip().lower() in ("h264", "avc1")
    except (subprocess.SubprocessError, OSError):
        return False
