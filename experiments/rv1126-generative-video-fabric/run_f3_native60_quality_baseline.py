#!/usr/bin/env python3
"""Native-60 F3 quality baseline for RV1126 video-fabric work.

Unlike the Daybreak harness, this script hard-rejects source footage whose decoded
cadence is not genuinely ~60 fps. The 1080p reference is spatially normalized only;
there is no fps=60 temporal normalization before baseline construction.

This is host-side FFmpeg image-quality evidence only. It makes no RV1126 timing or
trained-neural quality claim.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import subprocess
from fractions import Fraction
from pathlib import Path


def run(cmd, capture=False):
    print("RUN", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(cmd, check=True, text=True,
                          stdout=subprocess.PIPE if capture else None,
                          stderr=subprocess.PIPE if capture else None)


def probe_source(path: Path) -> dict:
    cp = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=codec_name,width,height,r_frame_rate,avg_frame_rate",
              "-show_entries", "format=duration,size,format_name", "-of", "json", str(path)], capture=True)
    payload = json.loads(cp.stdout)
    count = run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
                 "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)], capture=True)
    stream = payload["streams"][0]
    fmt = payload["format"]
    decoded_frames = int(json.loads(count.stdout)["streams"][0]["nb_read_frames"])
    duration = float(fmt["duration"])
    r_fps = float(Fraction(stream["r_frame_rate"]))
    return {
        "stream": stream,
        "format": fmt,
        "decoded_frames": decoded_frames,
        "decoded_fps": decoded_frames / duration,
        "r_frame_rate_fps": r_fps,
    }


def probe(path: Path) -> dict:
    cp = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
              "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate",
              "-of", "json", str(path)], capture=True)
    return json.loads(cp.stdout)["streams"][0]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def encode(src: Path, dst: Path, vf: str, start=None, duration=None, frames=None):
    cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
    if start is not None:
        cmd += ["-ss", str(start)]
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


def decoded_frame_count(path: Path) -> int:
    cp = run(["ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
              "-show_entries", "stream=nb_read_frames", "-of", "json", str(path)], capture=True)
    return int(json.loads(cp.stdout)["streams"][0]["nb_read_frames"])


def adjacent_exact_duplicate_count(path: Path) -> dict:
    cp = run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-i", str(path),
              "-an", "-f", "framemd5", "-"], capture=True)
    hashes = []
    for line in cp.stdout.splitlines():
        if not line or line.startswith("#"):
            continue
        parts = [x.strip() for x in line.split(",")]
        if len(parts) >= 6:
            hashes.append(parts[-1])
    duplicates = sum(a == b for a, b in zip(hashes, hashes[1:]))
    return {"frames_hashed": len(hashes), "adjacent_exact_duplicates": duplicates}


def measure(reference: Path, candidate: Path, label: str, authoritative_fps: int) -> dict:
    p = probe(candidate)
    row = {
        "case": label,
        "authoritative_fps": authoritative_fps,
        "candidate_width": int(p["width"]),
        "candidate_height": int(p["height"]),
        "candidate_r_frame_rate": p["r_frame_rate"],
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
    ap.add_argument("--start", type=float, default=10.0)
    ap.add_argument("--duration", type=float, default=3.0)
    ap.add_argument("--license", default="unknown")
    args = ap.parse_args()

    src = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    source = probe_source(src)
    meta = source["stream"]
    if int(meta["width"]) < 1920 or int(meta["height"]) < 1080:
        raise SystemExit(f"NATIVE60_SOURCE_RESOLUTION_FAIL {source}")
    if not (59.9 <= source["r_frame_rate_fps"] <= 60.1):
        raise SystemExit(f"NATIVE60_R_FRAME_RATE_FAIL {source}")
    if not (59.5 <= source["decoded_fps"] <= 60.5):
        raise SystemExit(f"NATIVE60_DECODED_CADENCE_FAIL {source}")

    print("NATIVE60_SOURCE_CONTRACT_PASS", json.dumps({
        "r_frame_rate": meta["r_frame_rate"],
        "avg_frame_rate": meta["avg_frame_rate"],
        "decoded_frames": source["decoded_frames"],
        "duration_seconds": float(source["format"]["duration"]),
        "decoded_fps": source["decoded_fps"],
        "source_width": int(meta["width"]),
        "source_height": int(meta["height"]),
    }, sort_keys=True), flush=True)

    reference = out / "reference_native60_1080p.mkv"
    # Spatial normalization only. No fps filter is allowed here. The exact
    # 180-frame budget, not a redundant timestamp duration cap, defines the segment.
    encode(src, reference, "scale=1920:1080:flags=lanczos,format=yuv420p",
           start=args.start, duration=None, frames=180)
    if decoded_frame_count(reference) != 180:
        raise SystemExit("NATIVE60_REFERENCE_FRAME_COUNT_FAIL")
    duplicate_evidence = adjacent_exact_duplicate_count(reference)
    if duplicate_evidence["frames_hashed"] != 180:
        raise SystemExit(f"NATIVE60_REFERENCE_HASH_COUNT_FAIL {duplicate_evidence}")

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
        "protocol": "RV1126_F3_NATIVE60_QUALITY_BASELINE/1",
        "source": {
            "filename": src.name,
            "sha256": sha256(src),
            "width": int(meta["width"]),
            "height": int(meta["height"]),
            "codec": meta.get("codec_name"),
            "r_frame_rate": meta["r_frame_rate"],
            "avg_frame_rate": meta["avg_frame_rate"],
            "decoded_frames": source["decoded_frames"],
            "duration_seconds": float(source["format"]["duration"]),
            "decoded_fps": source["decoded_fps"],
            "license": args.license,
        },
        "reference": {
            "construction": "spatial normalization to 1920x1080 yuv420p only; no temporal fps normalization; exact 180-frame output budget",
            "fps_filter_applied": False,
            "reference_frames": 180,
            "adjacent_exact_duplicate_evidence": duplicate_evidence,
        },
        "segment": {"start_seconds": args.start, "nominal_duration_seconds": args.duration, "reference_frames": 180},
        "scope": "native-60 host-side FFmpeg baseline quality only; no trained-neural quality and no RV1126 timing claim",
        "results": rows,
    }
    (out / "F3_NATIVE60_QUALITY_BASELINE.json").write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    with (out / "F3_NATIVE60_QUALITY_BASELINE.csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader(); w.writerows(rows)
    print("RV1126_F3_NATIVE60_QUALITY_BASELINE_PASS cases=7")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
