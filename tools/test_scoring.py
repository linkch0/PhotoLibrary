"""Unit-check the picker score responds to its inputs.

Guards the bugs found while building this: a saturated sharpness term, and an
aesthetic term stuck at exactly 0.5 because no text vectors were supplied.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np
from PIL import Image, ImageFilter, ImageOps

from app import scene
from app.library import PhotosLibrary

LIB = Path(sys.argv[1]) if len(sys.argv) > 1 else None
failures = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global failures
    if cond:
        print(f"  PASS  {name}")
    else:
        failures += 1
        print(f"  FAIL  {name}  {extra}")


def main() -> int:
    tv = scene.aesthetic_text_vectors()
    check("aesthetic prompt vectors available", tv is not None and tv.shape[0] == 8,
          f"got {None if tv is None else tv.shape}")

    lib = PhotosLibrary(LIB)
    photos = [a for a in lib.assets if a.media_type in ("photo", "live") and a.path][:6]

    # Aesthetic term must depend on the image vector, not be constant.
    vals = []
    for a in photos:
        vec = scene.embed_image(a.path)
        if vec is None:
            continue
        q = scene.score_image(a.path, tv, vec)
        vals.append(q.aesthetic)
    print(f"\naesthetic across {len(vals)} photos: {[round(v,3) for v in vals]}")
    check("aesthetic is not constant 0.5", len(set(round(v, 4) for v in vals)) > 1,
          f"values {vals}")

    # Sharpness must separate a sharp photo from a blurred copy of itself.
    a = photos[0]
    im = ImageOps.exif_transpose(Image.open(a.path)).convert("RGB")
    sharp_path = Path(__file__).parent / "_sharp.jpg"
    blur_path = Path(__file__).parent / "_blur.jpg"
    im.save(sharp_path, quality=92)
    im.filter(ImageFilter.GaussianBlur(3)).save(blur_path, quality=92)
    vec = scene.embed_image(a.path)
    qs = scene.score_image(sharp_path, tv, vec)
    qb = scene.score_image(blur_path, tv, vec)
    print(f"\nsharpness: sharp={qs.sharpness:.3f} blur={qb.sharpness:.3f}")
    check("blur scores lower than sharp", qb.sharpness < qs.sharpness - 0.08,
          f"{qb.sharpness:.3f} vs {qs.sharpness:.3f}")
    check("total score prefers sharp", qb.score() < qs.score(),
          f"{qb.score():.3f} vs {qs.score():.3f}")

    # Sharpness must not be saturated across the sample.
    sharp_vals = []
    for a in photos:
        q = scene.score_image(a.path, tv, scene.embed_image(a.path))
        if q:
            sharp_vals.append(q.sharpness)
    print(f"sharpness sample: {[round(v,3) for v in sharp_vals]}")
    check("sharpness has range (not all 1.0)",
          len(sharp_vals) > 1 and max(sharp_vals) - min(sharp_vals) > 0.05,
          f"spread {max(sharp_vals)-min(sharp_vals) if sharp_vals else 0:.3f}")

    # Portrait-ness must come from CLIP now, not the old Haar cascade. A zero
    # for every photo would mean the vectors are not reaching score_image.
    pvecs = scene.portrait_text_vectors()
    check("portrait prompt vectors available", pvecs is not None and pvecs.shape[0] == 9,
          f"got {None if pvecs is None else pvecs.shape}")
    port_vals = []
    for a in photos:
        vec = scene.embed_image(a.path)
        if vec is None:
            continue
        q = scene.score_image(a.path, tv, vec, pvecs)
        port_vals.append((q.portrait, q.face_area))
    print(f"\nportrait (flag, score): {[(p, round(s,2)) for p, s in port_vals]}")
    check("portrait score varies", len(set(round(s, 3) for _, s in port_vals)) > 1,
          f"{port_vals}")
    check("portrait flag consistent with score",
          all(p == (s >= 0.5) for p, s in port_vals), f"{port_vals}")
    # Without vectors it must be neutral, not 0 - a 0 would penalise every photo.
    deg = scene.score_image(photos[0].path, tv, None, None)
    check("missing portrait vectors are neutral", deg is not None and abs(deg.face_area - 0.5) < 1e-6,
          f"got {None if deg is None else deg.face_area}")

    for p in (sharp_path, blur_path):
        p.unlink(missing_ok=True)

    print(f"\nfailures: {failures}")
    print("RESULT:", "PASS" if failures == 0 else "FAIL")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
