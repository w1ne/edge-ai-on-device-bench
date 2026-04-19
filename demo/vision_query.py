#!/usr/bin/env python3
"""
vision_query.py — open-vocabulary vision for the robot.

The default vision path (demo/vision_watcher.py -> eyes.py -> YOLO-Fastest v2)
is limited to COCO's 80 classes.  When the user asks "is there a red mug on
the table?" there is no way to answer with YOLO alone.  This module adds an
open-vocabulary path using OpenAI CLIP (via open_clip_torch) zero-shot on
a single webcam frame.

Usage (module):

    vq = VisionQuery(camera_index=0)
    r = vq.query(["person", "laptop", "red mug"])
    # r = {"seen": ["person"], "scores": {...}, "frame_ms": 123}
    vq.close()

Usage (CLI — live test):

    python3 demo/vision_query.py --query "person" "laptop" "red mug"

Design notes:
  * CLIP load is LAZY: first query() pays the one-time model download +
    init cost (~350 MB for ViT-B-32, CPU-OK).  Startup stays cheap so the
    daemon boot path isn't blocked.
  * Webcam is held only for the single read (open -> read -> release).
    vision_watcher.py owns the camera in the long-running path; if it has
    /dev/videoN locked, our VideoCapture.isOpened() returns False and we
    surface {"seen": [], "error": "cam_busy"} so the planner can fall back
    to `look`.
  * Text side uses ImageNet-templated prompts ("a photo of a {phrase}")
    which materially outperforms raw phrases on CLIP.
  * Scores are softmaxed across the phrase list (relative ranking) — a
    phrase passes when prob >= threshold.  For true absent/present
    questions the caller should include at least 2 phrases (or add a
    negation distractor).
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from typing import Optional

import cv2


# ImageNet-style templates used by the original OpenAI CLIP zero-shot evals.
# Using a handful and averaging the text embeddings gives noticeably better
# probs than a single raw phrase.  Keep this list short — each template
# adds a full text encoder forward pass.
_PROMPT_TEMPLATES: tuple[str, ...] = (
    "a photo of a {}.",
    "a photo of the {}.",
    "a close-up photo of a {}.",
    "a cropped photo of a {}.",
    "a photograph of a {}.",
)


class VisionQuery:
    """Open-vocabulary single-frame CLIP query.

    Construction is cheap: we defer model loading until the first query()
    call so startup isn't blocked on the ~350 MB CLIP download.

    Arguments:
        camera_index: /dev/videoN index to open per query.
        model:        open_clip model name (default "ViT-B-32").
        pretrained:   open_clip pretrained tag (default "openai" — the
                      canonical OpenAI CLIP weights).
        device:       "cpu" or "cuda"; defaults to CPU (per on-device story).
        logger:       optional callable(str) -> None for diagnostics.
    """

    def __init__(
        self,
        camera_index: int = 0,
        model: str = "ViT-B-32",
        pretrained: str = "openai",
        device: Optional[str] = None,
        logger=None,
    ) -> None:
        self.camera_index = int(camera_index)
        self.model_name = model
        self.pretrained = pretrained
        self.device = device or "cpu"
        self._log = logger or (lambda *_a, **_kw: None)
        # Lazy-loaded state.
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        # Cache text features keyed by phrase so repeated queries are fast.
        self._text_cache: dict[str, "object"] = {}

    # ---------------------------------------------------------------- lazy load
    def _ensure_loaded(self) -> None:
        if self._model is not None:
            return
        t0 = time.time()
        try:
            import torch  # noqa: F401  (needed later, imported here to catch it early)
            import open_clip
        except ImportError as e:
            raise RuntimeError(
                f"open_clip_torch not installed: {e}. "
                f"Run: pip install --user open_clip_torch"
            ) from e

        # force_quick_gelu=True matches the original OpenAI CLIP activation
        # (open_clip otherwise warns about QuickGELU mismatch on 'openai' tag).
        model, _train_pp, preprocess = open_clip.create_model_and_transforms(
            self.model_name, pretrained=self.pretrained,
            force_quick_gelu=(self.pretrained == "openai"),
        )
        model.eval()
        tokenizer = open_clip.get_tokenizer(self.model_name)
        try:
            model.to(self.device)
        except Exception:
            # cuda not available etc. — fall back to cpu silently.
            self.device = "cpu"
            model.to("cpu")
        self._model = model
        self._preprocess = preprocess
        self._tokenizer = tokenizer
        self._log(
            f"[vision_query] loaded {self.model_name}/{self.pretrained} "
            f"on {self.device} in {time.time() - t0:.2f}s"
        )

    # ---------------------------------------------------------------- frame grab
    def _grab_frame(self):
        """Open camera -> read one frame -> release.  Returns (frame, err).
        If the camera is held by someone else, frame is None and err
        is a short code ('cam_busy' / 'cam_read_fail')."""
        cap = cv2.VideoCapture(self.camera_index, cv2.CAP_V4L2)
        if not cap.isOpened():
            # Fall back to default backend.
            cap = cv2.VideoCapture(self.camera_index)
        if not cap.isOpened():
            return None, "cam_busy"
        try:
            # Some UVC cams return None on the very first read; try a few times.
            frame = None
            for _ in range(3):
                ok, frame = cap.read()
                if ok and frame is not None:
                    break
            if frame is None:
                return None, "cam_read_fail"
            return frame, None
        finally:
            try:
                cap.release()
            except Exception:
                pass

    # ---------------------------------------------------------------- text embed
    def _encode_phrase(self, phrase: str):
        """Return the L2-normalised CLIP text embedding for one phrase,
        averaged across the ImageNet-style prompt templates.  Cached."""
        import torch
        cached = self._text_cache.get(phrase)
        if cached is not None:
            return cached
        prompts = [tpl.format(phrase) for tpl in _PROMPT_TEMPLATES]
        toks = self._tokenizer(prompts).to(self.device)
        with torch.no_grad():
            emb = self._model.encode_text(toks)
            emb = emb / emb.norm(dim=-1, keepdim=True)
            mean = emb.mean(dim=0)
            mean = mean / mean.norm()
        self._text_cache[phrase] = mean
        return mean

    # ---------------------------------------------------------------- public
    def query(self, phrases: list[str], threshold: float = 0.15) -> dict:
        """Run CLIP zero-shot on a single fresh webcam frame.

        Returns:
            {
                "seen":     [phrase, ...]        # phrases whose prob >= threshold
                "scores":   {phrase: prob, ...}  # softmaxed probs
                "frame_ms": int                  # total wall time
                "error":    optional str         # 'cam_busy', 'cam_read_fail',
                                                 # 'no_phrases', 'clip_load_failed'
            }
        """
        t0 = time.time()
        phrases = [p.strip() for p in (phrases or []) if p and p.strip()]
        if not phrases:
            return {"seen": [], "scores": {}, "frame_ms": 0,
                    "error": "no_phrases"}

        frame, err = self._grab_frame()
        if frame is None:
            return {"seen": [], "scores": {}, "frame_ms": int((time.time() - t0) * 1000),
                    "error": err or "cam_unknown"}

        try:
            self._ensure_loaded()
        except Exception as e:
            self._log(f"[vision_query] load failed: {e}")
            return {"seen": [], "scores": {},
                    "frame_ms": int((time.time() - t0) * 1000),
                    "error": "clip_load_failed"}

        import torch

        # Preprocess: BGR (cv2) -> RGB -> PIL -> CLIP normalised tensor.
        from PIL import Image
        img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(img_rgb)
        img_t = self._preprocess(pil).unsqueeze(0).to(self.device)

        with torch.no_grad():
            img_f = self._model.encode_image(img_t)
            img_f = img_f / img_f.norm(dim=-1, keepdim=True)
            # Stack per-phrase text embeddings.
            text_feats = torch.stack(
                [self._encode_phrase(p) for p in phrases], dim=0
            )
            # Cosine similarity; CLIP's reference code scales by 100 before softmax.
            logits = (100.0 * img_f @ text_feats.T).squeeze(0)
            probs = logits.softmax(dim=-1).cpu().tolist()

        scores = {p: float(probs[i]) for i, p in enumerate(phrases)}
        seen = [p for p, s in scores.items() if s >= float(threshold)]
        return {
            "seen": seen,
            "scores": scores,
            "frame_ms": int((time.time() - t0) * 1000),
        }

    # ---------------------------------------------------------------- cleanup
    def close(self) -> None:
        # No persistent webcam handle, nothing to release.  Drop refs so
        # the CLIP model can be GC'd if the caller is done.
        self._model = None
        self._preprocess = None
        self._tokenizer = None
        self._text_cache.clear()


# ================================================================= CLI

def _cli() -> int:
    ap = argparse.ArgumentParser(
        description="Run a one-shot open-vocabulary CLIP query against the webcam."
    )
    ap.add_argument("--camera", type=int, default=0,
                    help="/dev/videoN index (default 0)")
    ap.add_argument("--model", default="ViT-B-32",
                    help="open_clip model (default ViT-B-32)")
    ap.add_argument("--pretrained", default="openai",
                    help="open_clip pretrained tag (default openai)")
    ap.add_argument("--threshold", type=float, default=0.15,
                    help="softmax prob threshold for 'seen' (default 0.15)")
    ap.add_argument("--query", nargs="+", required=True,
                    help="free-form phrases, e.g. --query \"red mug\" laptop")
    args = ap.parse_args()

    def _log(s: str) -> None:
        print(s, file=sys.stderr, flush=True)

    vq = VisionQuery(camera_index=args.camera, model=args.model,
                     pretrained=args.pretrained, logger=_log)
    r = vq.query(args.query, threshold=args.threshold)
    vq.close()
    print(f"seen     : {r.get('seen')}")
    print(f"frame_ms : {r.get('frame_ms')}")
    if r.get("error"):
        print(f"error    : {r['error']}")
    print("scores   :")
    for p, s in sorted((r.get("scores") or {}).items(),
                       key=lambda kv: -kv[1]):
        print(f"  {s:.3f}  {p}")
    return 0 if not r.get("error") else 1


if __name__ == "__main__":
    sys.exit(_cli())
