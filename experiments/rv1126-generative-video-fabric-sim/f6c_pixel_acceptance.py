#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np


def load_adapter(path: Path):
    spec = importlib.util.spec_from_file_location("comfyui_rvfabric_node", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("adapter import spec unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def deterministic_source(frame_count=6, height=360, width=640):
    yy, xx = np.indices((height, width), dtype=np.uint32)
    frames = np.empty((frame_count, height, width, 3), dtype=np.uint8)
    for n in range(frame_count):
        frames[n, ..., 0] = (xx + 17 * n + yy // 3) % 256
        frames[n, ..., 1] = (2 * yy + 29 * n + xx // 5) % 256
        frames[n, ..., 2] = (xx // 2 + yy // 2 + 43 * n) % 256
    return frames


def phase_byte(offset: int, step: int) -> int:
    return int(np.floor((255.0 * offset / step) + 0.5))


def independent_reference(source: np.ndarray, source_fps: int, target_fps: int):
    step = target_fps // source_fps
    out = []
    for interval in range(source.shape[0] - 1):
        left = source[interval].astype(np.uint32)
        right = source[interval + 1].astype(np.uint32)
        out.append(source[interval].copy())
        for offset in range(1, step):
            phase = phase_byte(offset, step)
            pixels = ((left * (255 - phase) + right * phase + 127) // 255).astype(np.uint8)
            out.append(pixels)
    out.append(source[-1].copy())
    return np.stack(out)


def digest(array: np.ndarray) -> str:
    return hashlib.sha256(np.ascontiguousarray(array).tobytes()).hexdigest()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--adapter", default="comfyui_rvfabric_node.py")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=19020)
    ap.add_argument("--generation", type=int, required=True)
    ap.add_argument("--mode", choices=("clean", "fault"), required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    adapter = load_adapter(Path(args.adapter))
    assert "RVFabricExpand" in adapter.NODE_CLASS_MAPPINGS
    source = deterministic_source()
    reference = independent_reference(source, 10, 60)
    output, metadata = adapter.expand_rgb_numpy(
        source, 10, 60, args.host, args.port, args.generation, return_metadata=True
    )
    if not np.array_equal(output, reference):
        mismatch = int(np.count_nonzero(output != reference))
        raise RuntimeError(f"F6C pixel mismatch count={mismatch}")
    if output.shape != (31, 360, 640, 3):
        raise RuntimeError(f"unexpected output shape {output.shape}")
    if args.mode == "clean":
        assert metadata["fallback_count"] == 0, metadata
        assert metadata["worker_generated_count"] == 25, metadata
    else:
        assert metadata["fallback_count"] > 0, metadata
        assert metadata["worker_generated_count"] < 25, metadata

    record = {
        "protocol": "RV1126_F6C_PIXEL_ACCEPTANCE/1",
        "mode": args.mode,
        "status": "PASS",
        "generation": args.generation,
        "source_shape": list(source.shape),
        "output_shape": list(output.shape),
        "source_sha256": digest(source),
        "independent_reference_sha256": digest(reference),
        "fabric_output_sha256": digest(output),
        "byte_exact_against_independent_reference": True,
        "metadata": metadata,
        "claims": {
            "real_rgb_bytes_crossed_native_fabric_protocol": True,
            "comfyui_compatible_numpy_adapter_executed": True,
            "trained_neural_decoder_executed": False,
            "rv1126_silicon_executed": False,
        },
    }
    Path(args.out).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, sort_keys=True))
    print(f"RV1126_F6C_{args.mode.upper()}_PIXEL_ACCEPTANCE_PASS")


if __name__ == "__main__":
    main()
