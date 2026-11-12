"""Semantic index storage.

Embeddings are derived data, exactly like the thumbnail cache: they live in the
app's own cache directory, keyed by library path, and **nothing is ever written
inside a .photoslibrary bundle**. Deleting the cache loses nothing but the
index, which is rebuilt on demand.

Layout per library:

    <CACHE_ROOT>/libs/<hash>/embeddings/
        index.json      metadata: model name, dim, per-asset map
        vectors.f32     raw float32 matrix, N x dim, memory-map friendly
        scores.json     quality scores + face metadata (image picker)

Every row is keyed by the asset UUID, not the per-library database id, so an
index can never be silently reused against a different library.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from . import config


@dataclass
class IndexState:
    """Everything needed to serve semantic features for one library."""

    model: str = ""
    dim: int = 0
    assets: dict[str, dict] = field(default_factory=dict)  # uuid -> {id, name}
    vectors: np.ndarray | None = None                      # (N, dim) float32
    scores: dict[str, dict] = field(default_factory=dict)  # uuid -> picker data
    rows: dict[str, int] = field(default_factory=dict)     # uuid -> row index

    @property
    def count(self) -> int:
        return len(self.rows)


class SemanticIndex:
    """Loads, saves and queries the per-library embedding store."""

    def __init__(self, library_root: Path):
        self.root = Path(library_root).resolve()
        self.dir = config.library_cache_dir(self.root) / "embeddings"
        self._lock = threading.RLock()
        self.state = IndexState()
        self.loaded = False

    # ------------------------------------------------------------- paths

    @property
    def meta_path(self) -> Path:
        return self.dir / "index.json"

    @property
    def vectors_path(self) -> Path:
        return self.dir / "vectors.f32"

    @property
    def scores_path(self) -> Path:
        return self.dir / "scores.json"

    def exists(self) -> bool:
        """True when embeddings are available for search."""
        return self.meta_path.is_file() and self.vectors_path.is_file()

    def has_scores(self) -> bool:
        """True when picker scores exist, which is independent of embeddings.

        A score-only run writes scores without vectors; requiring an embedding
        index for those would make the picker silently report nothing.
        """
        return self.scores_path.is_file()

    # -------------------------------------------------------------- load

    def load(self) -> IndexState:
        with self._lock:
            if self.loaded:
                return self.state
            st = IndexState()

            # Scores load on their own: they are meaningful without vectors.
            if self.has_scores():
                try:
                    st.scores = json.loads(self.scores_path.read_text(encoding="utf-8"))
                except (OSError, ValueError, json.JSONDecodeError):
                    st.scores = {}

            if self.exists():
                try:
                    meta = json.loads(self.meta_path.read_text(encoding="utf-8"))
                    st.model = meta.get("model", "")
                    st.dim = int(meta.get("dim") or 0)
                    st.assets = meta.get("assets") or {}
                    n = len(st.assets)
                    if n and st.dim:
                        raw = np.fromfile(self.vectors_path, dtype=np.float32)
                        if raw.size == n * st.dim:
                            st.vectors = raw.reshape(n, st.dim)
                            order = meta.get("order") or list(st.assets)
                            st.rows = {u: i for i, u in enumerate(order) if i < n}
                        else:
                            # Shape mismatch means a torn/old file: ignore it
                            # rather than serving garbage similarities.
                            st.vectors = None
                except (OSError, ValueError, json.JSONDecodeError):
                    st.vectors = None
                    st.rows = {}
            self.state = st
            self.loaded = True
            return st

    # -------------------------------------------------------------- save

    def save(self, state: IndexState) -> None:
        """Atomically persist the index (write temp, then replace)."""
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            if state.vectors is None or not state.rows:
                return
            order = list(state.rows)
            vectors = state.vectors

            tmp = self.vectors_path.with_suffix(".f32.tmp")
            vectors.astype(np.float32).tofile(tmp)
            os.replace(tmp, self.vectors_path)

            meta = {
                "model": state.model,
                "dim": state.dim,
                "count": len(order),
                "order": order,
                "assets": state.assets,
            }
            tmpm = self.meta_path.with_suffix(".json.tmp")
            tmpm.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            os.replace(tmpm, self.meta_path)

            if state.scores:
                tmps = self.scores_path.with_suffix(".json.tmp")
                tmps.write_text(json.dumps(state.scores, ensure_ascii=False), encoding="utf-8")
                os.replace(tmps, self.scores_path)

            self.state = state
            self.loaded = True

    def save_scores(self, scores: dict[str, dict]) -> None:
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            self.state.scores = scores
            tmp = self.scores_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(scores, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.scores_path)

    # ------------------------------------------------------------- query

    def search(self, query_vec: np.ndarray, limit: int = 100, threshold: float = 0.0):
        """Cosine similarity search. Vectors are pre-normalised at index time."""
        st = self.state
        if st.vectors is None or not st.rows:
            return []
        q = np.asarray(query_vec, dtype=np.float32).reshape(-1)
        if q.size != st.dim:
            return []
        n = q / (np.linalg.norm(q) or 1.0)
        sims = st.vectors @ n
        idx = np.argsort(sims)[::-1]
        order = list(st.rows)
        out = []
        for i in idx[: max(limit * 3, limit)]:
            score = float(sims[i])
            if score < threshold:
                break
            uuid = order[int(i)]
            meta = st.assets.get(uuid) or {}
            out.append({"uuid": uuid, "id": meta.get("id"), "score": score})
            if len(out) >= limit:
                break
        return out

    def clear(self) -> None:
        with self._lock:
            for p in (self.meta_path, self.vectors_path, self.scores_path):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    pass
            self.state = IndexState()
            self.loaded = False
