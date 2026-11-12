"""CLIP embeddings and image-quality scoring.

Two independent capabilities:

* **Image search** - CLIP (ViT-B-32) maps photos and text into one space, so
  "fireworks at night" retrieves the right photos without any label training.
* **Image picker** - no single model decides whether a photo is one a person
  would keep. The score blends signals that are all cheap and defensible:
  sharpness (variance of Laplacian), exposure, contrast, colourfulness, plus
  CLIP's own aesthetic prompt similarity, and for portraits a face-presence
  and face-size term.

Everything is optional at runtime: if torch/open_clip are missing the app still
runs, and only the AI features report themselves unavailable.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path

import numpy as np

try:
    from PIL import Image, ImageOps, ImageFilter

    try:
        import pillow_heif

        pillow_heif.register_heif_opener()
    except Exception:  # pragma: no cover
        pass
    PIL_OK = True
except Exception:  # pragma: no cover
    PIL_OK = False

try:
    import torch
    import open_clip

    TORCH_OK = True
except Exception:  # pragma: no cover
    TORCH_OK = False

MODEL_NAME = "ViT-B-32"
PRETRAINED = "laion2b_s34b_b79k"
EMBED_DIM = 512

# Prompts that separate a portrait from a scene. CLIP understands "a photo of
# a person" far better than a Haar cascade, which fires on any large round face
# (on this library it flagged costumed mascots as portraits - 9/9 false positives).
PORTRAIT_PROMPTS = [
    "a close-up portrait of a person's face",
    "a photo of a person posing for the camera",
    "a selfie of a person",
    "two people smiling at the camera",
]
SCENE_PROMPTS = [
    "a wide shot of a stage show with costumed characters",
    "a landscape or cityscape with no people",
    "a photo of fireworks in the night sky",
    "a mascot costume character",
    "a photo of objects or food on a table",
]

# Prompts that correlate with "a photo someone would pick". Kept short and
# concrete; CLIP's zero-shot strength is in describing appearance.
AESTHETIC_POSITIVE = [
    "a beautiful professional photograph",
    "a sharp well composed photo of a person",
    "a stunning landscape photograph",
    "a clean well lit photograph",
]
AESTHETIC_NEGATIVE = [
    "a blurry out of focus photo",
    "a badly lit dark noisy photo",
    "an accidental photo of the ground",
    "a blurry photo of a finger covering the lens",
]

_model_lock = threading.Lock()
_cache: dict[str, object] = {}


def available() -> dict:
    """Report which capabilities this install can actually provide."""
    return {
        "torch": TORCH_OK,
        "pillow": PIL_OK,
        "clip": TORCH_OK and PIL_OK,
        "model": MODEL_NAME if TORCH_OK else None,
        "dim": EMBED_DIM if TORCH_OK else 0,
    }


def _load():
    """Load and cache the CLIP model + preprocess + tokenizer."""
    with _model_lock:
        if "model" in _cache:
            return _cache["model"], _cache["preprocess"], _cache["tokenizer"]
        model, _, preprocess = open_clip.create_model_and_transforms(
            MODEL_NAME, pretrained=PRETRAINED
        )
        model.eval()
        tokenizer = open_clip.get_tokenizer(MODEL_NAME)
        _cache["model"] = model
        _cache["preprocess"] = preprocess
        _cache["tokenizer"] = tokenizer
        return model, preprocess, tokenizer


def _as_rgb(path: Path) -> "Image.Image":
    im = Image.open(path)
    im = ImageOps.exif_transpose(im)
    return im.convert("RGB")


def embed_image(path: str | Path) -> np.ndarray | None:
    """Return a normalised 512-d CLIP vector for one image file."""
    if not (TORCH_OK and PIL_OK):
        return None
    model, preprocess, _ = _load()
    try:
        im = _as_rgb(Path(path))
        with torch.no_grad():
            x = preprocess(im).unsqueeze(0)
            f = model.encode_image(x)
            f = f / f.norm(dim=-1, keepdim=True)
        return f[0].numpy().astype(np.float32)
    except Exception:
        return None


def embed_text(text: str) -> np.ndarray | None:
    """Return a normalised 512-d CLIP vector for a text query."""
    if not (TORCH_OK and text.strip()):
        return None
    model, _, tokenizer = _load()
    try:
        with torch.no_grad():
            tf = tokenizer([text])
            t = model.encode_text(tf)
            t = t / t.norm(dim=-1, keepdim=True)
        return t[0].numpy().astype(np.float32)
    except Exception:
        return None


def embed_texts(texts: list[str]) -> np.ndarray | None:
    if not (TORCH_OK and texts):
        return None
    model, _, tokenizer = _load()
    try:
        with torch.no_grad():
            tf = tokenizer(texts)
            t = model.encode_text(tf)
            t = t / t.norm(dim=-1, keepdim=True)
        return t.numpy().astype(np.float32)
    except Exception:
        return None


# --------------------------------------------------------------- scoring

@dataclass
class Quality:
    sharpness: float
    exposure: float
    contrast: float
    colorfulness: float
    aesthetic: float
    face_present: float
    face_area: float
    portrait: bool

    def score(self) -> float:
        """Blend the signals into 0..1.

        Weights favour the things that actually make a photo unusable as a
        keeper: blur and bad exposure dominate, aesthetics break ties.
        """
        if self.portrait:
            s = (
                0.34 * self.sharpness
                + 0.18 * self.exposure
                + 0.10 * self.contrast
                + 0.08 * self.colorfulness
                + 0.20 * self.aesthetic
                + 0.10 * self.face_area
            )
        else:
            s = (
                0.34 * self.sharpness
                + 0.20 * self.exposure
                + 0.14 * self.contrast
                + 0.12 * self.colorfulness
                + 0.20 * self.aesthetic
            )
        return float(max(0.0, min(1.0, s)))

    def to_dict(self) -> dict:
        return {
            "score": round(self.score(), 4),
            "sharpness": round(self.sharpness, 4),
            "exposure": round(self.exposure, 4),
            "contrast": round(self.contrast, 4),
            "colorfulness": round(self.colorfulness, 4),
            "aesthetic": round(self.aesthetic, 4),
            "face": round(self.face_present, 4),
            "faceArea": round(self.face_area, 4),
            "portrait": self.portrait,
        }


def _laplacian_var(gray: np.ndarray) -> float:
    """Variance of the Laplacian: the standard cheap blur detector."""
    k = np.array([[0, 1, 0], [1, -4, 1], [0, 1, 0]], dtype=np.float32)
    h, w = gray.shape
    if h < 3 or w < 3:
        return 0.0
    # Convolve via slicing, avoiding a scipy/cv2 dependency.
    lap = (
        -4 * gray[1:-1, 1:-1]
        + gray[:-2, 1:-1]
        + gray[2:, 1:-1]
        + gray[1:-1, :-2]
        + gray[1:-1, 2:]
    )
    return float(lap.var())


def portrait_score(image_vec: np.ndarray | None, prompt_vecs: np.ndarray | None) -> float:
    """How portrait-like a photo is, from CLIP prompt similarity (0..1).

    Returns 0.5 when unavailable so callers can treat it as neutral.
    """
    if image_vec is None or prompt_vecs is None:
        return 0.5
    try:
        npos = len(PORTRAIT_PROMPTS)
        pos = prompt_vecs[:npos].mean(axis=0)
        neg = prompt_vecs[npos:].mean(axis=0)
        sp = float(np.dot(image_vec, pos))
        sn = float(np.dot(image_vec, neg))
        return float(1.0 / (1.0 + np.exp(-(sp - sn) * 20.0)))
    except Exception:
        return 0.5


def portrait_text_vectors() -> np.ndarray | None:
    return embed_texts(PORTRAIT_PROMPTS + SCENE_PROMPTS)


def _faces(im: "Image.Image") -> tuple[float, float]:
    """Face presence + relative area using OpenCV's Haar cascade.

    Kept for reference only: a Haar cascade detects any large round face, so on
    a library containing costumed characters it flagged mascots as portraits
    (9/9 false positives on a measured sample). Portrait classification now uses
    CLIP prompt similarity instead; this remains unused by score_image.
    """
    try:
        import cv2
    except Exception:
        return 0.0, 0.0
    try:
        small = im.copy()
        small.thumbnail((640, 640), Image.LANCZOS)
        arr = np.asarray(small.convert("RGB"))
        gray = cv2.cvtColor(arr, cv2.COLOR_RGB2GRAY)
        h, w = gray.shape
        cascade = cv2.CascadeClassifier(
            cv2.data.haarcascades + "haarcascade_frontalface_default.xml"
        )
        if cascade.empty():
            return 0.0, 0.0
        boxes = cascade.detectMultiScale(
            gray, scaleFactor=1.08, minNeighbors=5, minSize=(30, 30)
        )
        if len(boxes) == 0:
            return 0.0, 0.0
        areas = sorted(((bw * bh) / float(w * h) for (x, y, bw, bh) in boxes), reverse=True)
        biggest = areas[0]
        if biggest < 0.05:
            return 0.0, 0.0
        return 1.0, float(min(biggest / 0.20, 1.0))
    except Exception:
        return 0.0, 0.0


def score_image(path: str | Path, text_vecs: np.ndarray | None = None,
                image_vec: np.ndarray | None = None,
                portrait_vecs: np.ndarray | None = None) -> Quality | None:
    """Compute the picker score for one image."""
    if not PIL_OK:
        return None
    try:
        im = _as_rgb(Path(path))
    except Exception:
        return None

    small = im.copy()
    small.thumbnail((512, 512), Image.LANCZOS)
    gray = np.asarray(small.convert("L"), dtype=np.float32)

    # Sharpness: normalise the Laplacian variance onto 0..1.
    # Calibrated on real library photos: sharp originals sit around 1600
    # (p05=265, p50=1613, p95=3600), while a blurred copy lands near 800.
    # A divisor of 800 saturated almost everything at 1.0, so use a log scale
    # keyed to the real range: ~250 -> 0.2, ~1600 -> 0.75, ~3600 -> 1.0.
    lap = _laplacian_var(gray)
    sharpness = float(min(max(np.log1p(lap) - np.log1p(120), 0.0)
                          / (np.log1p(4200) - np.log1p(120)), 1.0))

    mean = float(gray.mean()) / 255.0
    # Best exposure is mid-grey; penalise both crushed shadows and blown highs.
    exposure = float(1.0 - min(abs(mean - 0.5) / 0.5, 1.0))

    std = float(gray.std()) / 255.0
    contrast = float(min(std / 0.28, 1.0))

    arr = np.asarray(small, dtype=np.float32)
    r, g, b = arr[..., 0], arr[..., 1], arr[..., 2]
    rg, yb = r - g, 0.5 * (r + g) - b
    colorfulness = float(
        min((np.sqrt(rg.std() ** 2 + yb.std() ** 2) + 0.3 * np.sqrt(rg.mean() ** 2 + yb.mean() ** 2)) / 90.0, 1.0)
    )

    # Aesthetic: CLIP similarity to positive vs negative photo descriptions.
    aesthetic = 0.5
    if text_vecs is not None and image_vec is not None:
        try:
            pos = text_vecs[: len(AESTHETIC_POSITIVE)].mean(axis=0)
            neg = text_vecs[len(AESTHETIC_POSITIVE):].mean(axis=0)
            sp = float(np.dot(image_vec, pos))
            sn = float(np.dot(image_vec, neg))
            # Map the margin to 0..1 around a neutral midpoint.
            aesthetic = float(1.0 / (1.0 + np.exp(-(sp - sn) * 18.0)))
        except Exception:
            aesthetic = 0.5

    # Portrait-ness from CLIP prompt similarity. A Haar cascade was tried and
    # rejected: on a library with costumed characters it flagged mascots as
    # portraits (9/9 false positives). CLIP at least understands "a person".
    p_score = portrait_score(image_vec, portrait_vecs)
    portrait = p_score >= 0.5
    face_present = 1.0 if portrait else 0.0
    # Reuse face_area as "how strongly portrait-like", which is what the picker
    # weight needs downstream.
    face_area = p_score

    return Quality(
        sharpness=sharpness, exposure=exposure, contrast=contrast,
        colorfulness=colorfulness, aesthetic=aesthetic,
        face_present=face_present, face_area=face_area, portrait=portrait,
    )


def aesthetic_text_vectors() -> np.ndarray | None:
    return embed_texts(AESTHETIC_POSITIVE + AESTHETIC_NEGATIVE)
