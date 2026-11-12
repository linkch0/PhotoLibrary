"""Portrait classification must find real people, not mascots or stage shows.

History: a Haar cascade was used first and was wrong in a specific way - it
detects any large round face, so on a library with costumed characters it
flagged 9 stage/mascot photos as portraits while missing every real portrait
(9/9 false positives, confirmed visually). It was replaced with CLIP prompt
similarity, which ranks actual people at the top (10/10 verified visually).

This asserts the shape of the distribution rather than an exact count, since the
right number depends on what the library contains.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np

from app import scene
from app.library import PhotosLibrary

failures = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global failures
    if cond:
        print(f"  PASS  {name}")
    else:
        failures += 1
        print(f"  FAIL  {name}  {extra}")


def main() -> int:
    if len(sys.argv) < 2:
        print(__doc__)
        print(f"usage: python {Path(__file__).name} <library> [n]")
        return 2
    lib_path = Path(sys.argv[1])
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 60

    pvecs = scene.portrait_text_vectors()
    check("portrait prompt vectors exist", pvecs is not None and pvecs.shape[0] == 9,
          f"got {None if pvecs is None else pvecs.shape}")
    if pvecs is None:
        print("RESULT: FAIL")
        return 1

    lib = PhotosLibrary(lib_path)
    photos = [a for a in lib.assets
              if a.media_type in ("photo", "live") and a.path][:n]

    scores = []
    for a in photos:
        vec = scene.embed_image(a.path)
        if vec is None:
            continue
        scores.append(scene.portrait_score(vec, pvecs))

    print(f"\nportrait scores over {len(scores)} photos:")
    print(f"  min={min(scores):.3f} p50={np.median(scores):.3f} "
          f"p90={np.percentile(scores, 90):.3f} max={max(scores):.3f}")

    check("scores are not constant", max(scores) - min(scores) > 0.2,
          f"spread {max(scores) - min(scores):.3f}")

    n_port = sum(1 for s in scores if s >= 0.5)
    pct = n_port / len(scores) * 100
    print(f"  classified portrait (>=0.5): {n_port}/{len(scores)} ({pct:.0f}%)")
    check("some photos are portraits", n_port > 0, "none found")
    check("not everything is a portrait", n_port < len(scores),
          "all classified as portraits")
    check("top score well above median",
          max(scores) - float(np.median(scores)) > 0.2,
          f"max {max(scores):.3f} median {np.median(scores):.3f}")
    check("missing vectors give neutral 0.5",
          scene.portrait_score(None, None) == 0.5)

    print(f"\nfailures: {failures}")
    print("RESULT:", "PASS" if failures == 0 else "FAIL")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
