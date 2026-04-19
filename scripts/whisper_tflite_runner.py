#!/usr/bin/env python3
"""
whisper_tflite_runner.py -- laptop-side TFLite runner for Whisper Tiny.en.

Used by demo/robot_daemon.py when --stt-backend=tflite.

Pipeline (laptop CPU, XNNPack, 4 threads):

    16 kHz mono WAV  (pulled from phone via adb push/pull, or laptop path)
      -> openai-whisper log_mel_spectrogram  (1, 80, 3000) float32
      -> tflite_runtime Interpreter.invoke() on the nyadla-sys packaged
         whisper-tiny.en.tflite (encoder + baked greedy decoder)
      -> HF WhisperTokenizer.decode(tokens, skip_special_tokens=True)

Why this runs on the LAPTOP, not the phone:
  - There is no on-phone Python runtime available on Pixel 6 (no Termux
    installed). The prebuilt benchmark_model binary in /data/local/tmp/tflite
    can time the graph but does not emit decoded text.
  - Cross-building a TFLite+XNNPack Android binary that also runs a Whisper
    tokenizer is a multi-hour task outside the 90-minute landing window.
  - Laptop-side TFLite still exercises the XNNPack fp32 kernels and gives
    us a functional end-to-end sanity check. On-phone compute numbers come
    from the existing benchmark_model pass.

Exit codes:
    0   — success, transcript printed on stdout (may be empty if the model
          decoded to only special tokens, which is a known issue with the
          nyadla-sys packaged graph; see README / log).
    1   — hard failure (model missing, decode exception, etc.) — caller
          should fall back to the whisper-cli path.

Usage:
    python3 scripts/whisper_tflite_runner.py <wav_path>
          [--model /tmp/tflite-models/whisper-tiny.en.tflite]
          [--threads 4]
          [--json]                          # emit JSON {transcript, wall_s}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

DEFAULT_MODEL = "/tmp/tflite-models/whisper-tiny.en.tflite"


def run(wav_path: str, model_path: str, threads: int) -> tuple[str, float]:
    # Lazy imports so bare --help / missing-file errors are fast.
    import numpy as np

    try:
        import whisper  # openai-whisper: pip install openai-whisper
    except ImportError as e:
        print(f"ERROR: openai-whisper not installed ({e}). "
              f"pip install openai-whisper", file=sys.stderr)
        sys.exit(1)

    try:
        from tflite_runtime.interpreter import Interpreter
    except ImportError:
        # Fall back to full TensorFlow's interpreter if tflite_runtime is
        # missing. The stock nyadla model is pure TFLite (no Flex ops) so
        # both paths work; tflite_runtime is just lighter.
        try:
            from tensorflow.lite import Interpreter  # type: ignore
        except ImportError as e:
            print(f"ERROR: neither tflite_runtime nor tensorflow.lite is "
                  f"available ({e})", file=sys.stderr)
            sys.exit(1)

    try:
        from transformers import WhisperTokenizer
    except ImportError as e:
        print(f"ERROR: transformers not installed ({e})", file=sys.stderr)
        sys.exit(1)

    if not Path(model_path).exists():
        print(f"ERROR: tflite model not found at {model_path}", file=sys.stderr)
        sys.exit(1)
    if not Path(wav_path).exists():
        print(f"ERROR: wav not found at {wav_path}", file=sys.stderr)
        sys.exit(1)

    t_wall = time.time()

    # 1) Mel features (whisper's canonical log10-mel, pad/trim to 30 s).
    audio = whisper.pad_or_trim(whisper.load_audio(wav_path))
    mel = whisper.log_mel_spectrogram(audio).numpy()[None].astype(np.float32)

    # 2) TFLite invoke (XNNPack auto-delegates on CPU in tflite_runtime 2.14).
    it = Interpreter(model_path=model_path, num_threads=threads)
    it.allocate_tensors()
    it.set_tensor(it.get_input_details()[0]["index"], mel)
    it.invoke()
    tokens = it.get_tensor(it.get_output_details()[0]["index"])[0].tolist()

    # 3) Detokenize. skip_special_tokens handles the SOT/notimestamps prefix
    #    and trailing EOT padding uniformly.
    tok = WhisperTokenizer.from_pretrained("openai/whisper-tiny.en")
    text = tok.decode(tokens, skip_special_tokens=True).strip()

    return text, time.time() - t_wall


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("wav", help="16 kHz mono WAV path")
    p.add_argument("--model", default=DEFAULT_MODEL)
    p.add_argument("--threads", type=int, default=4)
    p.add_argument("--json", action="store_true",
                   help="emit JSON on stdout: {transcript, wall_s, backend}")
    args = p.parse_args()

    # Silence TF/tflite banner chatter on stderr when JSON is requested so
    # the daemon can parse stdout cleanly.
    if args.json:
        os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "3")

    try:
        text, wall = run(args.wav, args.model, args.threads)
    except Exception as e:
        print(f"ERROR: tflite runner crashed: {type(e).__name__}: {e}",
              file=sys.stderr)
        sys.exit(1)

    if args.json:
        print(json.dumps({"transcript": text,
                          "wall_s": round(wall, 3),
                          "backend": "tflite-laptop-xnnpack-4t",
                          "model": os.path.basename(args.model)}))
    else:
        # Plain stdout: one line of transcript (may be empty).
        print(text)


if __name__ == "__main__":
    main()
