#!/usr/bin/env python3
"""Prepare a deterministic real-video-derived 7-channel calibration corpus.

This is a quantization calibration proxy for the proven F2 converter graph, not a
claim about final production model input semantics or reconstruction quality.
Packing: left RGB (3), right RGB (3), phase byte (1), all HWC uint8 at 640x360.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

import numpy as np

W, H, C = 640, 360, 3
FRAME_BYTES = W * H * C


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ffprobe_duration(src: Path) -> float:
    cp = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(src)
    ], check=True, text=True, capture_output=True)
    return float(cp.stdout.strip())


def frame_at(src: Path, timestamp: float) -> np.ndarray:
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.6f}",
        "-i", str(src), "-an", "-frames:v", "1",
        "-vf", f"scale={W}:{H}:flags=lanczos", "-pix_fmt", "rgb24",
        "-f", "rawvideo", "pipe:1"
    ]
    cp = subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if len(cp.stdout) != FRAME_BYTES:
        raise RuntimeError(f"expected {FRAME_BYTES} RGB bytes at {timestamp}, got {len(cp.stdout)}")
    return np.frombuffer(cp.stdout, dtype=np.uint8).reshape(H, W, C).copy()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--count", type=int, default=16)
    ap.add_argument("--pair-delta", type=float, default=1.0 / 30.0)
    args = ap.parse_args()

    if args.count < 4:
        raise SystemExit("count must be >= 4")
    src = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    duration = ffprobe_duration(src)
    start = 2.0
    end = max(start, duration - 2.0 - args.pair_delta)
    timestamps = np.linspace(start, end, args.count)
    phases = np.rint(np.linspace(0, 255, args.count)).astype(np.uint8)

    dataset_rows = []
    samples = []
    for idx, (timestamp, phase) in enumerate(zip(timestamps, phases)):
        left = frame_at(src, float(timestamp))
        right = frame_at(src, float(timestamp + args.pair_delta))
        packed = np.empty((H, W, 7), dtype=np.uint8)
        packed[..., 0:3] = left
        packed[..., 3:6] = right
        packed[..., 6] = phase
        p = out / f"daybreak_real_calibration_{idx:02d}.npy"
        np.save(p, packed)
        dataset_rows.append(str(p.resolve()))
        channel_stats = []
        for ch in range(7):
            v = packed[..., ch]
            channel_stats.append({
                "channel": ch,
                "min": int(v.min()),
                "max": int(v.max()),
                "mean": float(v.mean()),
            })
        samples.append({
            "index": idx,
            "left_timestamp_seconds": float(timestamp),
            "right_timestamp_seconds": float(timestamp + args.pair_delta),
            "phase_byte": int(phase),
            "file": p.name,
            "sha256": sha256(p),
            "channel_stats": channel_stats,
        })

    dataset = out / "dataset.txt"
    dataset.write_text("\n".join(dataset_rows) + "\n", encoding="utf-8")
    manifest = {
        "protocol": "RV1126_F3_REAL_VIDEO_CALIBRATION_CORPUS/1",
        "status": "CALIBRATION_PROXY_NOT_PRODUCTION_INPUT_SEMANTICS",
        "source": {
            "filename": src.name,
            "sha256": sha256(src),
            "duration_seconds": duration,
        },
        "tensor": {
            "shape_hwc": [H, W, 7],
            "dtype": "uint8",
            "layout": [
                "0:left_rgb_r", "1:left_rgb_g", "2:left_rgb_b",
                "3:right_rgb_r", "4:right_rgb_g", "5:right_rgb_b",
                "6:interpolation_phase_0_to_255"
            ],
            "pair_delta_seconds": args.pair_delta,
            "semantic_scope": "Calibration-proxy packing chosen only to expose the proven 7-channel converter graph to real-video RGB distributions plus a full-range phase feature. The final trained decoder may define different channel semantics and must regenerate calibration if so."
        },
        "sample_count": args.count,
        "dataset_sha256": sha256(dataset),
        "samples": samples,
        "quality_claim": false,
        "rv1126_timing_claim": false,
    }
    (out / "CALIBRATION_MANIFEST.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print("RV1126_F3_REAL_VIDEO_CALIBRATION_CORPUS_PASS", json.dumps({
        "samples": args.count,
        "source_sha256": manifest["source"]["sha256"],
        "dataset_sha256": manifest["dataset_sha256"],
        "layout": manifest["tensor"]["layout"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
