#!/usr/bin/env python3
"""F3 quality-harness baseline for RV1126 generative-video work.

This measures deterministic x86/FFmpeg reconstruction quality only. It makes no
RV1126 timing claim and does not evaluate the untrained F2 CNN as a useful model.
The pinned Daybreak source is normalized to a 60-fps reference; source-native
cadence is measured independently from decoded frame count and duration rather
than trusting container avg_frame_rate metadata.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
from pathlib import Path


def run(cmd, capture=False):
    print("RUN", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def probe(path: Path) -> dict:
    cp = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration",
              "-of", "json", str(path)], capture=True)
    return json.loads(cp.stdout)["streams"][0]


def source_probe(path: Path) -> dict:
    cp = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,nb_frames,duration",
              "-show_entries", "format=duration,size,format_name", "-of", "json", str(path)], capture=True)
    payload = json.loads(cp.stdout)
    count = run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)], capture=True)
    stream = payload["streams"][0]
    fmt = payload["format"]
    decoded_frames = int(json.loads(count.stdout)["streams"][0]["nb_read_frames"])
    duration = float(fmt["duration"])
    return {
        "stream": stream,
        "format": fmt,
        "decoded_frames": decoded_frames,
        "decoded_fps": decoded_frames / duration,
    }


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def encode(src: Path, dst: Path, vf: str, start=None, duration=None, frames=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    if start is not None:
        cmd += ["-ss", str(start)]
    cmd += ["-i", str(src)]
    if duration is not None:
        cmd += ["-t", str(duration)]
    cmd += ["-an", "-vf", vf]
    if frames is not None:
        cmd += ["-frames:v", str(frames)]
    cmd += ["-c:v", "ffv1", "-level", "3", str(dst)]
    run(cmd)


def metric(reference: Path, candidate: Path, kind: str) -> float:
    if kind == "psnr":
        filt = "[0:v][1:v]psnr"
        pattern = re.compile(r"average:([0-9.]+)")
    elif kind == "ssim":
        filt = "[0:v][1:v]ssim"
        pattern = re.compile(r"All:([0-9.]+)")
    else:
        raise ValueError(kind)
    cp = subprocess.run(["ffmpeg", "-hide_banner", "-i", str(reference), "-i", str(candidate),
                         "-lavfi", filt, "-f", "null", "-"],
                        text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    matches = pattern.findall(cp.stderr)
    if not matches:
        raise RuntimeError(f"could not parse {kind}: {cp.stderr[-4000:]}")
    return float(matches[-1])


def measure(reference: Path, candidate: Path, label: str, source_fps) -> dict:
    p = probe(candidate)
    row = {
        "case": label,
        "authoritative_fps": source_fps,
        "candidate_width": int(p["width"]),
        "candidate_height": int(p["height"]),
        "candidate_avg_frame_rate": p["avg_frame_rate"],
        "candidate_sha256": sha256(candidate),
        "psnr_db": metric(reference, candidate, "psnr"),
        "ssim": metric(reference, candidate, "ssim"),
    }
    print("QUALITY_RESULT", json.dumps(row, sort_keys=True), flush=True)
    return row


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--start", type=float, default=20.0)
    ap.add_argument("--duration", type=float, default=3.0)
    args = ap.parse_args()

    src = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    source = source_probe(src)
    meta = source["stream"]
    if (int(meta["width"]), int(meta["height"])) != (1920, 1080):
        raise SystemExit(f"SOURCE_CONTRACT_FAIL {source}")

    print("SOURCE_NATIVE_CADENCE", json.dumps({
        "r_frame_rate": meta.get("r_frame_rate"),
        "avg_frame_rate": meta.get("avg_frame_rate"),
        "decoded_frames": source["decoded_frames"],
        "duration_seconds": float(source["format"]["duration"]),
        "decoded_fps": source["decoded_fps"],
        "reference_normalized_fps": 60,
    }, sort_keys=True), flush=True)

    reference = out / "reference_1080p60.mkv"
    encode(src, reference, "fps=60,format=yuv420p", start=args.start, duration=args.duration, frames=180)
    rows = []

    spatial = out / "spatial_360p60_to_1080p60.mkv"
    encode(reference, spatial, "scale=640:360:flags=lanczos,scale=1920:1080:flags=lanczos", frames=180)
    rows.append(measure(reference, spatial, "spatial_only_360p60", 60))

    for afps in (5, 10, 15):
        auth = out / f"authoritative_360p{afps}.mkv"
        encode(reference, auth, f"fps={afps},scale=640:360:flags=lanczos")

        hold = out / f"hold_360p{afps}_to_1080p60.mkv"
        encode(auth, hold,
               "tpad=stop_mode=clone:stop_duration=1,fps=60,scale=1920:1080:flags=lanczos",
               frames=180)
        rows.append(measure(reference, hold, f"hold_{afps}_to_60", afps))

        linear = out / f"linear_360p{afps}_to_1080p60.mkv"
        encode(auth, linear,
               "tpad=stop_mode=clone:stop_duration=1,framerate=fps=60:interp_start=0:interp_end=255:scene=100,scale=1920:1080:flags=lanczos",
               frames=180)
        rows.append(measure(reference, linear, f"linear_{afps}_to_60", afps))

    result = {
        "protocol": "RV1126_F3_GROUND_TRUTH_QUALITY_BASELINE/2",
        "source": {
            "filename": src.name,
            "sha256": sha256(src),
            "width": int(meta["width"]),
            "height": int(meta["height"]),
            "r_frame_rate": meta.get("r_frame_rate"),
            "avg_frame_rate_metadata": meta.get("avg_frame_rate"),
            "decoded_frames": source["decoded_frames"],
            "duration_seconds": float(source["format"]["duration"]),
            "decoded_fps": source["decoded_fps"],
            "license": "CC BY 3.0; FreeSwissVideo via Wikimedia Commons",
        },
        "reference": {
            "normalization": "source normalized with FFmpeg fps=60 before spatial/temporal baseline construction",
            "fps": 60,
            "native_60fps_source": False,
        },
        "segment": {"start_seconds": args.start, "duration_seconds": args.duration, "reference_frames": 180},
        "scope": "x86 FFmpeg quality harness only; source-native cadence is ~30 fps and is normalized to 60 fps; no RV1126 timing claim; no trained-neural quality claim",
        "results": rows,
    }
    (out / "F3_QUALITY_BASELINE.json").write_text(json.dumps(result, indent=2, sort_keys=True)+"\n")
    with (out / "F3_QUALITY_BASELINE.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print("RV1126_F3_GROUND_TRUTH_BASELINE_PASS cases=7 source_native_cadence_recorded=true")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
