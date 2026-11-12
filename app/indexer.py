"""Background indexing job: embed a library and score it for the picker.

Runs in a worker thread with progress the UI can poll. Two phases:

1. **Embed** every photo once (CLIP). Costs ~190 ms/photo on CPU, so a
   2,300-photo library is around 7 minutes — this is why it is opt-in and
   resumable rather than something that happens on open.
2. **Score** each photo for the picker (sharpness/exposure/aesthetic/faces).

Resumable: an existing index is loaded first, and only assets whose UUID is
missing get embedded. Re-running after adding photos only does the new ones.
"""

from __future__ import annotations

import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import scene
from .library import PhotosLibrary
from .semantic import IndexState, SemanticIndex


@dataclass
class Job:
    """Progress of one indexing run."""

    library_root: str
    total: int = 0
    done: int = 0
    scored: int = 0
    skipped: int = 0
    failed: int = 0
    state: str = "idle"      # idle | running | done | error | cancelled
    message: str = ""
    started: float = 0.0
    finished: float = 0.0
    error: str = ""
    cancel: bool = False

    def to_dict(self) -> dict:
        elapsed = (self.finished or time.time()) - self.started if self.started else 0.0
        rate = self.done / elapsed if elapsed > 0.5 and self.done else 0.0
        remaining = max(self.total - self.done, 0)
        return {
            "state": self.state,
            "total": self.total,
            "done": self.done,
            "scored": self.scored,
            "skipped": self.skipped,
            "failed": self.failed,
            "message": self.message,
            "error": self.error,
            "elapsed": round(elapsed, 1),
            "eta": round(remaining / rate, 1) if rate > 0 else None,
            "percent": round(self.done / self.total * 100, 1) if self.total else 0.0,
        }


class Indexer:
    """Owns the single active job for a library index."""

    def __init__(self):
        self._lock = threading.Lock()
        self._job: Job | None = None
        self._thread: threading.Thread | None = None

    # -------------------------------------------------------------- status

    def status(self) -> dict | None:
        job = self._job
        return job.to_dict() if job else None

    def running(self) -> bool:
        return bool(self._job and self._job.state == "running")

    def cancel(self) -> bool:
        if self._job and self._job.state == "running":
            self._job.cancel = True
            self._job.message = "cancelling…"
            return True
        return False

    # --------------------------------------------------------------- start

    def start(self, lib: PhotosLibrary, index: SemanticIndex,
              score_only: bool = False, limit: int | None = None) -> Job:
        with self._lock:
            if self.running():
                return self._job  # type: ignore[return-value]
            job = Job(library_root=str(lib.root), started=time.time(), state="running")
            self._job = job
            self._thread = threading.Thread(
                target=self._run, args=(lib, index, job, score_only, limit), daemon=True
            )
            self._thread.start()
            return job

    # ----------------------------------------------------------------- run

    def _run(self, lib: PhotosLibrary, index: SemanticIndex, job: Job,
             score_only: bool, limit: int | None) -> None:
        try:
            if not scene.available()["pillow"]:
                raise RuntimeError("Pillow is unavailable, cannot read images")

            # Only stills; a video has no meaningful CLIP frame here.
            assets = [a for a in lib.assets
                      if not a.trashed and a.has_original
                      and a.media_type in ("photo", "live", "screenshot")]
            if limit:
                assets = assets[:limit]
            job.total = len(assets)
            job.message = "loading model…"

            state = index.load() if index.exists() else IndexState(model=scene.MODEL_NAME,
                                                                   dim=scene.EMBED_DIM)
            if state.model and state.model != scene.MODEL_NAME:
                # A different model's vectors are not comparable; start over.
                job.message = f"model changed ({state.model} -> {scene.MODEL_NAME}), rebuilding"
                state = IndexState(model=scene.MODEL_NAME, dim=scene.EMBED_DIM)

            # Keep only vectors for assets that still exist.
            live_uuids = {a.uuid for a in assets}
            if state.rows:
                keep = [u for u in state.rows if u in live_uuids]
                if len(keep) != len(state.rows) and state.vectors is not None:
                    keep_rows = [state.rows[u] for u in keep]
                    state.vectors = state.vectors[keep_rows]
                    state.rows = {u: i for i, u in enumerate(keep)}
                    state.assets = {u: v for u, v in state.assets.items() if u in set(keep)}

            need = [a for a in assets if a.uuid not in state.rows]
            job.skipped = len(assets) - len(need)
            job.message = "encoding…" if not score_only else "scoring…"

            # Score-only runs still need the aesthetic prompt vectors, else the
            # aesthetic term silently contributes a flat 0.5 for every photo.
            text_vecs = scene.aesthetic_text_vectors()
            portrait_vecs = scene.portrait_text_vectors()

            # Existing vectors, grown in chunks rather than reallocated per row.
            new_vecs: list[np.ndarray] = []
            new_uuids: list[str] = []
            scored = dict(state.scores)

            for i, a in enumerate(need):
                if job.cancel:
                    job.state = "cancelled"
                    job.message = "cancelled"
                    job.finished = time.time()
                    break

                vec = None
                # The picker's aesthetic term needs the image vector too, so a
                # score-only run must still encode - it just doesn't store it.
                vec = scene.embed_image(a.path) if a.path else None
                if vec is None:
                    job.failed += 1
                elif not score_only:
                    new_vecs.append(vec)
                    new_uuids.append(a.uuid)
                    state.assets[a.uuid] = {"id": a.pk, "name": a.original_filename or a.filename}

                # Score regardless (cheap relative to embedding, and useful on
                # its own for the picker).
                q = scene.score_image(a.path, text_vecs, vec, portrait_vecs) if a.path else None
                if q is not None:
                    scored[a.uuid] = q.to_dict()
                    job.scored += 1

                job.done = i + 1
                if job.done % 10 == 0:
                    pct = job.done / max(job.total, 1) * 100
                    job.message = f"indexed {job.done}/{job.total} ({pct:.0f}%)"

            # If scoring only, still score assets that were previously embedded.
            if score_only:
                for a in assets:
                    if job.cancel:
                        break
                    if a.uuid in scored:
                        continue
                    row = state.rows.get(a.uuid)
                    vec = state.vectors[row] if (state.vectors is not None and row is not None) else None
                    q = scene.score_image(a.path, text_vecs, vec, portrait_vecs) if a.path else None
                    if q is not None:
                        scored[a.uuid] = q.to_dict()
                    job.scored += 1
                    job.done += 1
                    if job.done % 10 == 0:
                        job.message = f"scored {job.done}/{job.total}"

            if new_vecs:
                added = np.vstack(new_vecs).astype(np.float32)
                if state.vectors is not None and len(state.rows):
                    state.vectors = np.vstack([state.vectors, added])
                else:
                    state.vectors = added
                base = len(state.rows)
                for j, uuid in enumerate(new_uuids):
                    state.rows[uuid] = base + j
                state.dim = added.shape[1]

            state.scores = scored
            if state.model == "":
                state.model = scene.MODEL_NAME
                state.dim = state.dim or scene.EMBED_DIM
            index.save(state)
            index.save_scores(scored)

            if job.state != "cancelled":
                job.state = "done"
                job.message = (f"indexed {len(state.rows)} photos, "
                               f"scored {len(scored)}")
        except Exception as exc:  # keep the UI informed instead of dying silently
            job.state = "error"
            job.error = f"{type(exc).__name__}: {exc}"
            job.message = job.error
            traceback.print_exc()
        finally:
            job.finished = time.time()
