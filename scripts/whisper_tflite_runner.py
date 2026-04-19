#!/usr/bin/env python3
"""
whisper_tflite_runner.py -- laptop-side TFLite runner for Whisper Tiny.en.

Used by demo/robot_daemon.py when --stt-backend=tflite.

DEFAULT pipeline (encoder-only TFLite + pytorch greedy decoder):

    16 kHz mono WAV
      -> openai-whisper log_mel_spectrogram  (80, 3000) float32
      -> transpose to (1, 3000, 80) for the onnx2tf-exported encoder
      -> tflite_runtime Interpreter.invoke() on whisper_enc_tiny_en_fp32
         (XNNPack, 4 threads, fp32)  -> (1, 1500, 384) audio features
      -> openai-whisper's pytorch decoder (DecodingTask with _get_audio_features
         patched to return the TFLite output); emits correct English transcript
         with language=en, task=transcribe, without_timestamps=True.

This captures the encoder speedup (the expensive stage, ~505 ms on phone /
~550 ms on this laptop) while reusing a decoder that is known to generate
correct tokens.

LEGACY pipeline (--model-mode full): the baked-in greedy-decoder model at
/tmp/tflite-models/whisper-tiny.en.tflite (nyadla-sys community port). This
graph's decoder exits on the first real prediction and emits only prologue
tokens — kept for regression reference only.

Exit codes:
    0   — success, transcript printed on stdout
    1   — hard failure (model missing, decode exception, etc.) — caller
          should fall back to the whisper-cli path.

Usage:
    python3 scripts/whisper_tflite_runner.py <wav_path>
          [--model-mode {encoder-only,full}]      # default: encoder-only
          [--encoder /tmp/tflite-models/whisper_enc_tiny_en_fp32.tflite]
          [--full-model /tmp/tflite-models/whisper-tiny.en.tflite]
          [--threads 4]
          [--json]                                # emit JSON
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_ENCODER = "/tmp/tflite-models/whisper_enc_tiny_en_fp32.tflite"
DEFAULT_FULL = "/tmp/tflite-models/whisper-tiny.en.tflite"


def _load_interpreter(model_path: str, threads: int):
    try:
        from tflite_runtime.interpreter import Interpreter
    except ImportError:
        try:
            from tensorflow.lite import Interpreter  # type: ignore
        except ImportError as e:
            print(f"ERROR: neither tflite_runtime nor tensorflow.lite is "
                  f"available ({e})", file=sys.stderr)
            sys.exit(1)
    return Interpreter(model_path=model_path, num_threads=threads)


# Module-level singletons so a long-lived caller (e.g. importing this
# module in-process instead of forking a subprocess) only pays encoder/
# decoder load once. The CLI subprocess path re-imports and still pays
# Python startup — to capture the full speedup end-to-end the daemon
# needs to import run_encoder_only directly rather than shelling out.
_CACHED_INTERPRETER = None
_CACHED_INTERPRETER_PATH = None
_CACHED_WHISPER_MODEL = None


def _get_interpreter(model_path: str, threads: int):
    global _CACHED_INTERPRETER, _CACHED_INTERPRETER_PATH
    if (_CACHED_INTERPRETER is None
            or _CACHED_INTERPRETER_PATH != model_path):
        it = _load_interpreter(model_path, threads)
        it.allocate_tensors()
        _CACHED_INTERPRETER = it
        _CACHED_INTERPRETER_PATH = model_path
    return _CACHED_INTERPRETER


def _get_whisper_model(name: str = "tiny.en"):
    global _CACHED_WHISPER_MODEL
    if _CACHED_WHISPER_MODEL is None:
        import whisper
        _CACHED_WHISPER_MODEL = whisper.load_model(name)
    return _CACHED_WHISPER_MODEL


def run_encoder_only(wav_path: str, encoder_path: str,
                     threads: int) -> tuple[str, float, dict]:
    """Encoder-only TFLite + pytorch greedy decoder."""
    import numpy as np

    try:
        import whisper  # openai-whisper
        import torch
    except ImportError as e:
        print(f"ERROR: openai-whisper / torch not installed ({e}). "
              f"pip install openai-whisper torch", file=sys.stderr)
        sys.exit(1)

    if not Path(encoder_path).exists():
        print(f"ERROR: encoder tflite not found at {encoder_path}",
              file=sys.stderr)
        sys.exit(1)
    if not Path(wav_path).exists():
        print(f"ERROR: wav not found at {wav_path}", file=sys.stderr)
        sys.exit(1)

    t_wall = time.time()

    # 1) Mel features (whisper canonical log-mel, pad/trim to 30 s).
    audio = whisper.pad_or_trim(whisper.load_audio(wav_path))
    t_mel = time.time()
    mel = whisper.log_mel_spectrogram(audio).numpy()  # (80, 3000) f32
    # onnx2tf-exported encoder expects (1, 3000, 80).
    mel_in = mel.T[None].astype(np.float32)
    mel_ms = (time.time() - t_mel) * 1000

    # 2) TFLite encoder (XNNPack auto-delegates in tflite_runtime 2.14).
    #    Interpreter is cached at module scope so re-invocations in the
    #    same process skip allocate_tensors().
    it = _get_interpreter(encoder_path, threads)
    in0 = it.get_input_details()[0]
    out0 = it.get_output_details()[0]
    it.set_tensor(in0["index"], mel_in)
    t_enc = time.time()
    it.invoke()
    enc_ms = (time.time() - t_enc) * 1000
    audio_features = it.get_tensor(out0["index"])  # (1, 1500, 384) f32

    # 3) Pytorch greedy decoder fed with our TFLite audio_features.
    #    We patch DecodingTask._get_audio_features to skip the pytorch
    #    encoder entirely — that is the whole point of this split.
    from whisper.decoding import DecodingOptions, DecodingTask

    model = _get_whisper_model("tiny.en")
    device = next(model.parameters()).device
    dtype = next(model.parameters()).dtype

    af = torch.from_numpy(audio_features).to(device=device, dtype=dtype)
    dummy_mel = torch.zeros(1, 80, 3000, device=device, dtype=dtype)

    opts = DecodingOptions(
        task="transcribe",
        language="en",
        without_timestamps=True,
        fp16=(dtype == torch.float16),
    )
    task = DecodingTask(model, opts)
    task._get_audio_features = lambda mel: af  # type: ignore

    t_dec = time.time()
    results = task.run(dummy_mel)
    dec_ms = (time.time() - t_dec) * 1000

    text = results[0].text.strip()
    wall = time.time() - t_wall
    timings = {
        "mel_ms": round(mel_ms, 1),
        "encoder_ms": round(enc_ms, 1),
        "decoder_ms": round(dec_ms, 1),
    }
    return text, wall, timings


def run_full(wav_path: str, model_path: str,
             threads: int) -> tuple[str, float, dict]:
    """Legacy path: nyadla-sys full graph (broken decoder, kept for ref)."""
    import numpy as np

    try:
        import whisper
    except ImportError as e:
        print(f"ERROR: openai-whisper not installed ({e})", file=sys.stderr)
        sys.exit(1)
    try:
        from transformers import WhisperTokenizer
    except ImportError as e:
        print(f"ERROR: transformers not installed ({e})", file=sys.stderr)
        sys.exit(1)

    if not Path(model_path).exists():
        print(f"ERROR: full tflite model not found at {model_path}",
              file=sys.stderr)
        sys.exit(1)
    if not Path(wav_path).exists():
        print(f"ERROR: wav not found at {wav_path}", file=sys.stderr)
        sys.exit(1)

    t_wall = time.time()
    audio = whisper.pad_or_trim(whisper.load_audio(wav_path))
    mel = whisper.log_mel_spectrogram(audio).numpy()[None].astype(np.float32)

    it = _load_interpreter(model_path, threads)
    it.allocate_tensors()
    it.set_tensor(it.get_input_details()[0]["index"], mel)
    t_inv = time.time()
    it.invoke()
    inv_ms = (time.time() - t_inv) * 1000
    tokens = it.get_tensor(it.get_output_details()[0]["index"])[0].tolist()

    tok = WhisperTokenizer.from_pretrained("openai/whisper-tiny.en")
    text = tok.decode(tokens, skip_special_tokens=True).strip()

    return text, time.time() - t_wall, {"invoke_ms": round(inv_ms, 1)}


def main() -> None:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("wav", help="16 kHz mono WAV path")
    p.add_argument("--model-mode", choices=["encoder-only", "full"],
                   default="encoder-only",
                   help="default: encoder-only TFLite + pytorch decoder "
                        "(correct transcripts). full = legacy nyadla-sys "
                        "graph (broken decoder, empty output).")
    p.add_argument("--encoder", default=DEFAULT_ENCODER,
                   help="path to encoder-only TFLite (fp32)")
    p.add_argument("--full-model", default=DEFAULT_FULL,
                   help="path to full-graph TFLite (legacy mode only)")
    # --model kept for back-compat with the old CLI — treated as encoder
    # unless --model-mode=full, in which case it's the full graph.
    p.add_argument("--model", default=None,
                   help="[deprecated] alias for --encoder or --full-model "
                        "depending on --model-mode")
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--json", action="store_true",
                   help="emit JSON on stdout: "
                        "{transcript, wall_s, backend, timings}")
    args = p.parse_args()

    if args.json:
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

    try:
        if args.model_mode == "encoder-only":
            enc = args.model or args.encoder
            text, wall, timings = run_encoder_only(args.wav, enc, args.threads)
            backend = "tflite-enc-xnnpack-4t+pytorch-dec"
            model_name = os.path.basename(enc)
        else:
            full = args.model or args.full_model
            text, wall, timings = run_full(args.wav, full, args.threads)
            backend = "tflite-full-xnnpack-4t"
            model_name = os.path.basename(full)
    except Exception as e:
        print(f"ERROR: tflite runner crashed: {type(e).__name__}: {e}",
              file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({
            "transcript": text,
            "wall_s": round(wall, 3),
            "backend": backend,
            "model": model_name,
            "timings_ms": timings,
        }))
    else:
        print(text)


if __name__ == "__main__":
    main()
