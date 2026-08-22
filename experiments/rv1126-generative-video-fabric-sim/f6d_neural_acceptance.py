#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

import numpy as np
import onnxruntime as ort

EXPECTED_ONNX_SHA256 = "cc42f9bfc26a7cfc56a4cc8058415d2cb8b91a99614b6e3db9991f3fe4ac24ec"


def sha256_file(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def digest(a):
    return hashlib.sha256(np.ascontiguousarray(a).tobytes()).hexdigest()


def load_adapter(path):
    spec = importlib.util.spec_from_file_location("comfyui_rvfabric_node", path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def source_frames(count=5, height=360, width=640):
    yy, xx = np.indices((height, width), dtype=np.uint32)
    a = np.empty((count, height, width, 3), dtype=np.uint8)
    for n in range(count):
        a[n, ..., 0] = (3 * xx + yy + 31 * n) % 256
        a[n, ..., 1] = (xx + 2 * yy + 47 * n) % 256
        a[n, ..., 2] = (xx // 3 + 5 * yy + 59 * n) % 256
    return a


def phase_byte(offset, step):
    return int(np.floor(255.0 * offset / step + 0.5))


def session(model):
    options = ort.SessionOptions(); options.intra_op_num_threads = 1; options.inter_op_num_threads = 1
    return ort.InferenceSession(str(model), sess_options=options, providers=["CPUExecutionProvider"])


def direct_frame(sess, left, right, pb):
    h, w, _ = left.shape
    x = np.empty((1, 7, h, w), dtype=np.float32)
    x[0, :3] = left.transpose(2, 0, 1)
    x[0, 3:6] = right.transpose(2, 0, 1)
    x[0, 6] = float(pb)
    residual = sess.run(["residual"], {"input": x})[0][0].transpose(1, 2, 0)
    pn = np.float32(pb / 255.0)
    base = left.astype(np.float32) * (np.float32(1.0) - pn) + right.astype(np.float32) * pn
    return np.rint(np.clip(base + residual, 0.0, 255.0)).astype(np.uint8)


def direct_clip(sess, source, source_fps=10, target_fps=60):
    step = target_fps // source_fps
    neural, linear = [], []
    for i in range(source.shape[0] - 1):
        neural.append(source[i].copy()); linear.append(source[i].copy())
        for off in range(1, step):
            pb = phase_byte(off, step)
            neural.append(direct_frame(sess, source[i], source[i + 1], pb))
            pn = np.float32(pb / 255.0)
            base = source[i].astype(np.float32) * (np.float32(1.0) - pn) + source[i + 1].astype(np.float32) * pn
            linear.append(np.rint(np.clip(base, 0.0, 255.0)).astype(np.uint8))
    neural.append(source[-1].copy()); linear.append(source[-1].copy())
    return np.stack(neural), np.stack(linear)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--adapter", default="comfyui_rvfabric_node.py")
    ap.add_argument("--port", type=int, default=19030)
    ap.add_argument("--generation", type=int, default=21)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    if sha256_file(args.model) != EXPECTED_ONNX_SHA256:
        raise RuntimeError("sealed F4 ONNX identity mismatch")
    source = source_frames()
    sess = session(args.model)
    direct, linear = direct_clip(sess, source)
    adapter = load_adapter(args.adapter)
    fabric, metadata = adapter.expand_rgb_numpy(source, 10, 60, port=args.port, generation=args.generation, timeout_seconds=60.0, return_metadata=True)
    if not np.array_equal(fabric, direct):
        raise RuntimeError(f"distributed neural output differs from direct ONNX at {np.count_nonzero(fabric != direct)} channel values")
    changed = int(np.count_nonzero(direct != linear))
    if changed == 0:
        raise RuntimeError("trained decoder produced no pixel changes relative to linear")
    record = {
        "protocol": "RV1126_F6D_TRAINED_ONNX_DISTRIBUTED/1",
        "status": "PASS",
        "model_sha256": EXPECTED_ONNX_SHA256,
        "source_shape": list(source.shape),
        "output_shape": list(fabric.shape),
        "reconstructed_frames": 20,
        "source_sha256": digest(source),
        "direct_onnx_sha256": digest(direct),
        "fabric_output_sha256": digest(fabric),
        "linear_reference_sha256": digest(linear),
        "byte_exact_fabric_vs_direct_onnx": True,
        "channel_values_changed_vs_linear": changed,
        "max_abs_pixel_delta_vs_linear": int(np.abs(direct.astype(np.int16) - linear.astype(np.int16)).max()),
        "metadata": metadata,
        "claims": {
            "sealed_f4_trained_onnx_executed_behind_worker_protocol": True,
            "native_rust_host_data_plane_executed": True,
            "distributed_output_matches_direct_reference": True,
            "rv1126_rknn_runtime_executed": False,
            "rv1126_silicon_executed": False,
            "quality_generalization_claim": False,
        },
    }
    Path(args.out).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    print(json.dumps(record, sort_keys=True))
    print("RV1126_F6D_TRAINED_ONNX_DISTRIBUTED_PASS")


if __name__ == "__main__":
    main()
