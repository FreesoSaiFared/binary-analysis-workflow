#!/usr/bin/env python3
"""Generate one exact real-video F4 semantic tensor for F5B on-board timing.

Probe = native60 eval frame 600 as left, frame 606 as right, target phase 3/6.
The packed tensor is 1x360x640x7 uint8 NHWC; phase byte is round(255*3/6)=128.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np

W, H = 640, 360
FRAME_BYTES = W * H * 3
EXPECTED_SOURCE_SHA = "af7324ac7edb9dfe6a8ed95824d5a645156f6e875286181934f20a9bad23392a"
EXPECTED_SOURCE_FRAMES = 2340
LEFT_ABS = 600
RIGHT_ABS = 606
TARGET_ABS = 603
PHASE_BYTE = 128


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, capture=False):
    return subprocess.run(list(map(str, cmd)), check=True, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    src = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    got = sha256(src)
    if got != EXPECTED_SOURCE_SHA:
        raise RuntimeError(f"source sha mismatch {got} != {EXPECTED_SOURCE_SHA}")
    cp = run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
              "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", src], capture=True)
    if int(cp.stdout.strip()) != EXPECTED_SOURCE_FRAMES:
        raise RuntimeError("source frame count mismatch")

    raw = out / "f5b_probe_frames_600_606.rgb"
    vf = f"select='eq(n,{LEFT_ABS})+eq(n,{RIGHT_ABS})',scale={W}:{H}:flags=lanczos"
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
         "-an", "-vsync", "0", "-vf", vf, "-frames:v", "2",
         "-pix_fmt", "rgb24", "-f", "rawvideo", raw])
    if raw.stat().st_size != 2 * FRAME_BYTES:
        raise RuntimeError(f"probe frame bytes mismatch {raw.stat().st_size}")
    frames = np.memmap(raw, dtype=np.uint8, mode="r", shape=(2, H, W, 3))
    packed = np.empty((1, H, W, 7), dtype=np.uint8)
    packed[0, ..., 0:3] = frames[0]
    packed[0, ..., 3:6] = frames[1]
    packed[0, ..., 6] = PHASE_BYTE
    probe = out / "f5b_semantic_probe_1x360x640x7_u8_nhwc.bin"
    probe.write_bytes(np.ascontiguousarray(packed).tobytes())
    expected_bytes = W * H * 7
    if probe.stat().st_size != expected_bytes:
        raise RuntimeError("packed probe size mismatch")
    manifest = {
        "protocol": "RV1126_F5B_REAL_SEMANTIC_TIMING_PROBE/1",
        "source_sha256": got,
        "source_frames": EXPECTED_SOURCE_FRAMES,
        "shape_nhwc": [1, H, W, 7],
        "dtype": "uint8",
        "layout": ["left_rgb_r", "left_rgb_g", "left_rgb_b", "right_rgb_r", "right_rgb_g", "right_rgb_b", "interpolation_phase_byte"],
        "left_frame_absolute": LEFT_ABS,
        "right_frame_absolute": RIGHT_ABS,
        "ground_truth_target_frame_absolute": TARGET_ABS,
        "interval_frames": RIGHT_ABS - LEFT_ABS,
        "intermediate_offset_frames": TARGET_ABS - LEFT_ABS,
        "phase_formula": "round(255 * intermediate_offset_frames / interval_frames)",
        "phase_byte": PHASE_BYTE,
        "probe_file": probe.name,
        "probe_size_bytes": probe.stat().st_size,
        "probe_sha256": sha256(probe),
        "use": "F5B on-board RKNPU1 model-only timing; not itself image-quality evidence"
    }
    mp = out / "F5B_SEMANTIC_PROBE_MANIFEST.json"
    mp.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(json.dumps(manifest, sort_keys=True))
    print("RV1126_F5B_REAL_SEMANTIC_PROBE_READY")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
