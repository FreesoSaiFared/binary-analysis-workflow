#!/usr/bin/env python3
"""F5A: real original-RV1126 runtime quality acceptance for the sealed F4 model.

This intentionally refuses simulator-only execution. Host-side Python/ADB call timing is
recorded as diagnostic only; it is never promoted to pure RV1126 silicon timing.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import math
import os
import re
import subprocess
import time
from pathlib import Path

import numpy as np

W, H = 640, 360
FRAME_BYTES = W * H * 3
SOURCE_FPS = 60
EVAL_START = 600
EVAL_FRAMES = 180
EXTRACT_FRAMES = 192
RATES = (5, 10, 15)
STEPS = {5: 12, 10: 6, 15: 4}
EXPECTED_SOURCE_FRAMES = 2340
EXPECTED_SOURCE_SHA = "af7324ac7edb9dfe6a8ed95824d5a645156f6e875286181934f20a9bad23392a"
EXPECTED_RKNN_SHA = "f0dc43ff2c836cbab05d2b836538b1bb4912c404354d19425a1b837d4fff4dae"
EXPECTED_RKNN_SIZE = 39074
F4_FLOAT = {
    5: {"psnr_db": 22.729881, "ssim": 0.706963},
    10: {"psnr_db": 24.268552, "ssim": 0.752045},
    15: {"psnr_db": 25.124605, "ssim": 0.777858},
}
F4_LINEAR = {
    5: {"psnr_db": 22.399179, "ssim": 0.702335},
    10: {"psnr_db": 23.836483, "ssim": 0.745442},
    15: {"psnr_db": 24.690565, "ssim": 0.770549},
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def run(cmd, *, capture=False, check=True, timeout=None):
    print("RUN", " ".join(map(str, cmd)), flush=True)
    return subprocess.run(
        list(map(str, cmd)), check=check, text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
        timeout=timeout,
    )


def adb_devices() -> list[str]:
    cp = run(["adb", "devices"], capture=True)
    out = []
    for line in cp.stdout.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2 and parts[1] == "device":
            out.append(parts[0])
    return out


def adb_shell(serial: str, command: str, *, optional=False) -> str:
    cp = run(["adb", "-s", serial, "shell", command], capture=True, check=not optional, timeout=15)
    if cp.returncode != 0 and optional:
        return f"UNAVAILABLE: {cp.stderr.strip()}"
    return cp.stdout.replace("\x00", " ").strip()


def identify_real_original_rv1126(requested: str | None) -> dict:
    devices = adb_devices()
    if requested:
        if requested not in devices:
            raise RuntimeError(f"requested adb device {requested!r} not in connected device-state devices {devices}")
        serial = requested
    else:
        if len(devices) != 1:
            raise RuntimeError(f"F5 requires exactly one selected real adb device; found {devices}")
        serial = devices[0]
    compatible = adb_shell(serial, "cat /proc/device-tree/compatible")
    low = compatible.lower()
    if "rv1126" not in low:
        raise RuntimeError(f"device-tree compatible does not identify RV1126: {compatible!r}")
    if "rv1126b" in low:
        raise RuntimeError(f"F5 targets original RV1126, not RV1126B: {compatible!r}")
    return {
        "adb_serial": serial,
        "adb_get_state": adb_shell(serial, "getprop sys.boot_completed", optional=True),
        "device_tree_compatible": compatible,
        "uname_a": adb_shell(serial, "uname -a", optional=True),
        "cpuinfo": adb_shell(serial, "cat /proc/cpuinfo", optional=True),
        "product_model": adb_shell(serial, "getprop ro.product.model", optional=True),
        "product_device": adb_shell(serial, "getprop ro.product.device", optional=True),
        "rknpu_processes": adb_shell(serial, "ps | grep -E 'npu|rknn|transfer'", optional=True),
        "npu_driver_log_tail": adb_shell(serial, "dmesg | grep -Ei 'galcore|rknpu|npu' | tail -80", optional=True),
        "identity_gate": "ORIGINAL_RV1126_COMPATIBLE_OBSERVED",
    }


def verify_source(src: Path) -> dict:
    got = sha256(src)
    if got != EXPECTED_SOURCE_SHA:
        raise RuntimeError(f"source sha mismatch {got} != {EXPECTED_SOURCE_SHA}")
    cp = run([
        "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", src,
    ], capture=True)
    frames = int(cp.stdout.strip())
    if frames != EXPECTED_SOURCE_FRAMES:
        raise RuntimeError(f"source frame count mismatch {frames} != {EXPECTED_SOURCE_FRAMES}")
    return {"sha256": got, "frames": frames}


def extract_eval_360(src: Path, raw: Path):
    vf = (
        f"trim=start_frame={EVAL_START}:end_frame={EVAL_START + EXTRACT_FRAMES},"
        f"setpts=PTS-STARTPTS,scale={W}:{H}:flags=lanczos"
    )
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
        "-an", "-vsync", "0", "-vf", vf, "-pix_fmt", "rgb24", "-f", "rawvideo", raw,
    ])
    expected = EXTRACT_FRAMES * FRAME_BYTES
    if raw.stat().st_size != expected:
        raise RuntimeError(f"eval raw size mismatch {raw.stat().st_size} != {expected}")


def build_reference_1080(src: Path, dst: Path):
    vf = (
        f"trim=start_frame={EVAL_START}:end_frame={EVAL_START + EVAL_FRAMES},"
        "setpts=PTS-STARTPTS,scale=1920:1080:flags=lanczos,format=yuv420p"
    )
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", src,
        "-an", "-vf", vf, "-frames:v", str(EVAL_FRAMES), "-c:v", "ffv1", "-level", "3", dst,
    ])
    cp = run([
        "ffprobe", "-v", "error", "-count_frames", "-select_streams", "v:0",
        "-show_entries", "stream=nb_read_frames", "-of", "default=nw=1:nk=1", dst,
    ], capture=True)
    if int(cp.stdout.strip()) != EVAL_FRAMES:
        raise RuntimeError("ground-truth reference is not exactly 180 frames")


def phase_values(offset: int, step: int) -> tuple[int, float]:
    phase_byte = int(round(255.0 * offset / step))
    return phase_byte, phase_byte / 255.0


def pack_input(left: np.ndarray, right: np.ndarray, phase_byte: int) -> np.ndarray:
    x = np.empty((1, H, W, 7), dtype=np.uint8)
    x[0, ..., 0:3] = left
    x[0, ..., 3:6] = right
    x[0, ..., 6] = phase_byte
    return x


def normalize_residual(output) -> tuple[np.ndarray, dict]:
    a = np.asarray(output)
    original_shape = list(a.shape)
    original_dtype = str(a.dtype)
    if np.issubdtype(a.dtype, np.integer):
        raise RuntimeError(
            f"RKNN returned integer output {a.dtype}; F5 refuses to invent dequantization without runtime output quantization parameters"
        )
    if a.ndim == 4 and a.shape[0] == 1:
        a = a[0]
    if a.shape == (3, H, W):
        a = np.transpose(a, (1, 2, 0))
    if a.shape != (H, W, 3):
        raise RuntimeError(f"unexpected RKNN residual shape {original_shape} -> {a.shape}")
    a = np.asarray(a, dtype=np.float32)
    if not np.isfinite(a).all():
        raise RuntimeError("non-finite values in RKNN residual output")
    return a, {
        "original_shape": original_shape,
        "original_dtype": original_dtype,
        "normalized_shape": [H, W, 3],
        "min": float(a.min()),
        "max": float(a.max()),
    }


def infer_once(rknn, packed: np.ndarray) -> tuple[np.ndarray, float, dict]:
    t0 = time.perf_counter_ns()
    outputs = rknn.inference(inputs=[packed], data_format=["nhwc"])
    elapsed_ms = (time.perf_counter_ns() - t0) / 1e6
    if not isinstance(outputs, (list, tuple)) or len(outputs) != 1:
        raise RuntimeError(f"expected one RKNN output, got {type(outputs)} len={len(outputs) if hasattr(outputs, '__len__') else 'n/a'}")
    residual, meta = normalize_residual(outputs[0])
    return residual, elapsed_ms, meta


def percentile_summary(values: list[float]) -> dict:
    a = np.asarray(values, dtype=np.float64)
    if a.size == 0:
        return {"count": 0}
    return {
        "count": int(a.size),
        "min_ms": float(a.min()),
        "mean_ms": float(a.mean()),
        "p50_ms": float(np.percentile(a, 50)),
        "p95_ms": float(np.percentile(a, 95)),
        "p99_ms": float(np.percentile(a, 99)),
        "max_ms": float(a.max()),
        "classification": "HOST_CONNECTED_PYTHON_ADB_CALL_WALL_TIME_DIAGNOSTIC_ONLY",
    }


def write_candidate_raw(path: Path, frames: list[np.ndarray]):
    if len(frames) != EVAL_FRAMES:
        raise RuntimeError(f"candidate frame count {len(frames)} != {EVAL_FRAMES}")
    with path.open("wb") as f:
        for frame in frames:
            f.write(np.ascontiguousarray(frame, dtype=np.uint8).tobytes())
    expected = EVAL_FRAMES * FRAME_BYTES
    if path.stat().st_size != expected:
        raise RuntimeError(f"candidate bytes {path.stat().st_size} != {expected}")


def ffmpeg_metric(reference: Path, cand_raw: Path, work: Path, label: str) -> dict:
    cand_video = work / f"{label}.mkv"
    run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
        "-pix_fmt", "rgb24", "-s:v", f"{W}x{H}", "-r", "60", "-i", cand_raw,
        "-frames:v", str(EVAL_FRAMES), "-vf", "scale=1920:1080:flags=lanczos,format=yuv420p",
        "-c:v", "ffv1", "-level", "3", cand_video,
    ])
    cp = run([
        "ffmpeg", "-hide_banner", "-i", reference, "-i", cand_video,
        "-lavfi", "[0:v][1:v]psnr", "-f", "null", "-",
    ], capture=True)
    psnr_match = re.findall(r"average:([0-9.]+)", cp.stderr)
    cp2 = run([
        "ffmpeg", "-hide_banner", "-i", reference, "-i", cand_video,
        "-lavfi", "[0:v][1:v]ssim", "-f", "null", "-",
    ], capture=True)
    ssim_match = re.findall(r"All:([0-9.]+)", cp2.stderr)
    if not psnr_match or not ssim_match:
        raise RuntimeError("failed to parse PSNR/SSIM")
    return {"psnr_db": float(psnr_match[-1]), "ssim": float(ssim_match[-1]), "frames": EVAL_FRAMES}


def determinism_probe(rknn, frames) -> dict:
    step = STEPS[10]
    left = np.asarray(frames[0], dtype=np.uint8)
    right = np.asarray(frames[step], dtype=np.uint8)
    pb, _ = phase_values(step // 2, step)
    packed = pack_input(left, right, pb)
    hashes = []
    metas = []
    times = []
    for _ in range(3):
        residual, ms, meta = infer_once(rknn, packed)
        hashes.append(hashlib.sha256(np.ascontiguousarray(residual).tobytes()).hexdigest())
        metas.append(meta)
        times.append(ms)
    return {
        "repetitions": 3,
        "byte_exact_float_output_hashes": hashes,
        "all_equal": len(set(hashes)) == 1,
        "output_meta": metas[0],
        "host_call_timing": percentile_summary(times),
    }


def evaluate_rate(rknn, frames, rate: int, reference: Path, work: Path) -> tuple[dict, list[float], dict]:
    step = STEPS[rate]
    outputs = []
    timings = []
    output_meta = None
    reconstructed = 0
    authoritative = 0
    for n in range(EVAL_FRAMES):
        gt = np.asarray(frames[n], dtype=np.uint8)
        left_rel = (n // step) * step
        offset = n - left_rel
        if offset == 0:
            outputs.append(gt.copy())
            authoritative += 1
            continue
        left = np.asarray(frames[left_rel], dtype=np.uint8)
        right = np.asarray(frames[left_rel + step], dtype=np.uint8)
        phase_byte, phase_norm = phase_values(offset, step)
        packed = pack_input(left, right, phase_byte)
        residual, ms, meta = infer_once(rknn, packed)
        output_meta = output_meta or meta
        base = left.astype(np.float32) * (1.0 - phase_norm) + right.astype(np.float32) * phase_norm
        pred = np.clip(np.rint(base + residual), 0, 255).astype(np.uint8)
        outputs.append(pred)
        timings.append(ms)
        reconstructed += 1
    raw = work / f"f5_device_{rate}_to_60.rgb"
    write_candidate_raw(raw, outputs)
    quality = ffmpeg_metric(reference, raw, work, f"f5_device_{rate}_to_60")
    quality.update({
        "authoritative_fps": rate,
        "authoritative_positions": authoritative,
        "runtime_reconstructed_positions": reconstructed,
    })
    return quality, timings, output_meta or {}


def safe_json(value):
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): safe_json(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [safe_json(v) for v in value]
    if isinstance(value, np.ndarray):
        return {"shape": list(value.shape), "dtype": str(value.dtype), "values": value.tolist() if value.size <= 64 else "omitted"}
    return repr(value)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rknn", required=True, help="exact sealed F4 trained .rknn")
    ap.add_argument("--source", required=True, help="exact sealed native-60 source")
    ap.add_argument("--out", required=True)
    ap.add_argument("--device-id", default=None)
    ap.add_argument("--warmup", type=int, default=10)
    args = ap.parse_args()

    model = Path(args.rknn)
    src = Path(args.source)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    work = out / "work"
    work.mkdir(exist_ok=True)

    model_sha = sha256(model)
    if model_sha != EXPECTED_RKNN_SHA or model.stat().st_size != EXPECTED_RKNN_SIZE:
        raise RuntimeError(f"F4 RKNN mismatch sha={model_sha} size={model.stat().st_size}")
    source_info = verify_source(src)
    device = identify_real_original_rv1126(args.device_id)

    raw = work / "eval_360.rgb"
    reference = work / "reference_eval_1080.mkv"
    extract_eval_360(src, raw)
    build_reference_1080(src, reference)
    frames = np.memmap(raw, dtype=np.uint8, mode="r", shape=(EXTRACT_FRAMES, H, W, 3))

    from rknn.api import RKNN
    rknn = RKNN(verbose=True)
    record = {
        "protocol": "RV1126_F5A_REAL_DEVICE_RUNTIME_QUALITY/1",
        "status": "STARTED_NOT_ACCEPTED",
        "device": device,
        "model": {"sha256": model_sha, "size_bytes": model.stat().st_size},
        "source": source_info,
        "runtime": {
            "target": "rv1126",
            "load_rknn_ret": None,
            "init_runtime_ret": None,
            "simulator_allowed": False,
        },
        "quality": {},
        "timing": {
            "host_connected_wall_time_claim": "DIAGNOSTIC_ONLY_NOT_PURE_RV1126_SILICON_TIMING",
            "silicon_timing_status": "NOT_PROVEN_BY_HOST_WALL_CLOCK",
            "full_pipeline_1080p60_timing_status": "NOT_PROVEN",
        },
    }
    try:
        load_ret = rknn.load_rknn(str(model))
        record["runtime"]["load_rknn_ret"] = int(load_ret)
        if load_ret != 0:
            raise RuntimeError(f"load_rknn failed ret={load_ret}")
        init_ret = rknn.init_runtime(target="rv1126", device_id=device["adb_serial"])
        record["runtime"]["init_runtime_ret"] = int(init_ret)
        if init_ret != 0:
            raise RuntimeError(f"init_runtime failed ret={init_ret}")
        record["runtime"]["real_target_runtime_initialized"] = True

        step = STEPS[10]
        pb, _ = phase_values(step // 2, step)
        probe = pack_input(np.asarray(frames[0]), np.asarray(frames[step]), pb)
        warmup_ms = []
        for _ in range(max(0, args.warmup)):
            _, ms, _ = infer_once(rknn, probe)
            warmup_ms.append(ms)
        record["timing"]["warmup_host_calls"] = percentile_summary(warmup_ms)
        record["determinism"] = determinism_probe(rknn, frames)
        if not record["determinism"]["all_equal"]:
            raise RuntimeError("same-input repeated RV1126 inference was not deterministic")

        perf_stdout = io.StringIO()
        perf_stderr = io.StringIO()
        try:
            with contextlib.redirect_stdout(perf_stdout), contextlib.redirect_stderr(perf_stderr):
                perf_ret = rknn.eval_perf(inputs=[probe])
            record["timing"]["rknn_eval_perf"] = {
                "status": "CAPTURED_FROM_REAL_TARGET_RUNTIME",
                "return": safe_json(perf_ret),
                "python_stdout": perf_stdout.getvalue(),
                "python_stderr": perf_stderr.getvalue(),
                "claim_scope": "ROCKCHIP_DEVICE_REPORTED_PERF_EVIDENCE_ONLY; NOT FULL_PIPELINE_TIMING",
            }
        except Exception as exc:
            record["timing"]["rknn_eval_perf"] = {"status": "UNAVAILABLE", "exception": repr(exc)}

        all_quality_pass = True
        for rate in RATES:
            q, times, meta = evaluate_rate(rknn, frames, rate, reference, work)
            q["delta_vs_f4_linear"] = {
                "psnr_db": q["psnr_db"] - F4_LINEAR[rate]["psnr_db"],
                "ssim": q["ssim"] - F4_LINEAR[rate]["ssim"],
            }
            q["delta_vs_f4_float_decoder"] = {
                "psnr_db": q["psnr_db"] - F4_FLOAT[rate]["psnr_db"],
                "ssim": q["ssim"] - F4_FLOAT[rate]["ssim"],
            }
            q["beats_f4_linear_psnr"] = q["psnr_db"] > F4_LINEAR[rate]["psnr_db"]
            q["beats_f4_linear_ssim"] = q["ssim"] > F4_LINEAR[rate]["ssim"]
            q["output_meta"] = meta
            record["quality"][str(rate)] = q
            record["timing"][f"host_connected_inference_calls_{rate}fps"] = percentile_summary(times)
            all_quality_pass = all_quality_pass and q["beats_f4_linear_psnr"] and q["beats_f4_linear_ssim"]
            print("F5_QUALITY_RESULT", rate, json.dumps(q, sort_keys=True), flush=True)

        record["acceptance"] = {
            "real_original_rv1126_identity": True,
            "real_target_runtime_initialized": True,
            "same_input_deterministic": record["determinism"]["all_equal"],
            "beats_linear_psnr_and_ssim_all_rates": bool(all_quality_pass),
            "f5a_runtime_image_quality": "PASS" if all_quality_pass else "FAIL",
            "f5b_model_only_silicon_timing": "NOT_PROVEN_BY_THIS_HOST_WALL_CLOCK_GATE",
            "full_pipeline_1080p60": "NOT_PROVEN",
        }
        record["status"] = "F5A_REAL_RV1126_RUNTIME_QUALITY_PASS" if all_quality_pass else "F5A_REAL_RV1126_RUNTIME_QUALITY_FAIL"
    except Exception as exc:
        record["failure"] = repr(exc)
        record["status"] = "F5A_REAL_RV1126_RUNTIME_QUALITY_FAIL"
        raise
    finally:
        try:
            rknn.release()
        except Exception:
            pass
        result_path = out / "RV1126_F5A_REAL_DEVICE_RESULT.json"
        result_path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
        print("F5_RESULT_PATH", result_path, flush=True)
        print(record["status"], flush=True)

    return 0 if record["status"] == "F5A_REAL_RV1126_RUNTIME_QUALITY_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
