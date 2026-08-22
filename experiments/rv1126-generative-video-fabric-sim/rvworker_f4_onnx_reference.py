#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import socket

import numpy as np
import onnxruntime as ort

PROTOCOL = "RVFABRIC/1"


def send_json(stream, value):
    stream.write(json.dumps(value, separators=(",", ":")).encode() + b"\n")


def read_exact(stream, size):
    data = bytearray()
    while len(data) < size:
        chunk = stream.read(size - len(data))
        if not chunk:
            raise EOFError(f"payload truncated {len(data)}/{size}")
        data.extend(chunk)
    return bytes(data)


def build_session(model):
    options = ort.SessionOptions()
    options.intra_op_num_threads = 1
    options.inter_op_num_threads = 1
    return ort.InferenceSession(model, sess_options=options, providers=["CPUExecutionProvider"])


def reconstruct(session, left, right, phase_byte):
    height, width, _ = left.shape
    x = np.empty((1, 7, height, width), dtype=np.float32)
    x[0, 0:3] = left.transpose(2, 0, 1)
    x[0, 3:6] = right.transpose(2, 0, 1)
    x[0, 6] = float(phase_byte)
    residual = session.run(["residual"], {"input": x})[0][0].transpose(1, 2, 0)
    phase = np.float32(phase_byte / 255.0)
    base = left.astype(np.float32) * (np.float32(1.0) - phase) + right.astype(np.float32) * phase
    return np.rint(np.clip(base + residual, 0.0, 255.0)).astype(np.uint8)


def serve_connection(conn, session, state):
    stream = conn.makefile("rwb", buffering=0)
    while True:
        line = stream.readline()
        if not line:
            return
        cmd = json.loads(line)
        kind = cmd.get("type")
        if kind == "hello":
            if cmd.get("protocol") != PROTOCOL:
                send_json(stream, {"type": "rejected", "reason": "protocol_mismatch"})
            else:
                send_json(stream, {"type": "hello_ack", "protocol": PROTOCOL})
        elif kind == "cache_interval_pixels":
            generation = int(cmd["generation"])
            interval = int(cmd["interval_id"])
            if generation < state["generation"]:
                send_json(stream, {"type": "rejected", "reason": "stale_generation"})
                continue
            width, height = int(cmd["width"]), int(cmd["height"])
            left_bytes, right_bytes = int(cmd["left_bytes"]), int(cmd["right_bytes"])
            expected = width * height * 3
            if left_bytes != expected or right_bytes != expected:
                send_json(stream, {"type": "rejected", "reason": "pixel_shape_or_size_mismatch"})
                continue
            send_json(stream, {"type": "payload_ready", "generation": generation, "interval_id": interval, "total_bytes": left_bytes + right_bytes})
            payload = read_exact(stream, left_bytes + right_bytes)
            if generation > state["generation"]:
                state["seen_jobs"].clear()
            state["generation"] = generation
            state["interval_id"] = interval
            state["left"] = np.frombuffer(payload[:left_bytes], dtype=np.uint8).reshape(height, width, 3).copy()
            state["right"] = np.frombuffer(payload[left_bytes:], dtype=np.uint8).reshape(height, width, 3).copy()
            send_json(stream, {"type": "cached", "generation": generation, "interval_id": interval})
        elif kind == "phase_pixels":
            generation, interval, job_id = int(cmd["generation"]), int(cmd["interval_id"]), int(cmd["job_id"])
            if generation != state["generation"]:
                send_json(stream, {"type": "rejected", "reason": "generation_mismatch"}); continue
            if interval != state["interval_id"] or state["left"] is None:
                send_json(stream, {"type": "rejected", "reason": "pixel_cache_miss"}); continue
            if job_id in state["seen_jobs"]:
                send_json(stream, {"type": "rejected", "reason": "duplicate_job"}); continue
            state["seen_jobs"].add(job_id)
            phase_byte = int(cmd["phase_u8"])
            pixels = reconstruct(session, state["left"], state["right"], phase_byte)
            raw = pixels.tobytes(order="C")
            send_json(stream, {
                "type": "result_pixels", "generation": generation, "interval_id": interval,
                "job_id": job_id, "phase_u8": phase_byte, "simulated_compute_ms": 0.0,
                "payload_bytes": len(raw)
            })
            stream.write(raw)
        elif kind == "reset":
            generation = int(cmd["generation"])
            if generation < state["generation"]:
                send_json(stream, {"type": "rejected", "reason": "stale_reset"}); continue
            state.update(generation=generation, interval_id=None, left=None, right=None, seen_jobs=set())
            send_json(stream, {"type": "reset_ack", "generation": generation})
        else:
            send_json(stream, {"type": "rejected", "reason": f"unsupported:{kind}"})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--port", type=int, required=True)
    args = ap.parse_args()
    session = build_session(args.model)
    state = {"generation": 0, "interval_id": None, "left": None, "right": None, "seen_jobs": set()}
    with socket.socket() as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", args.port))
        server.listen()
        print(f"RVWORKER_F4_ONNX_READY port={args.port}", flush=True)
        while True:
            conn, _ = server.accept()
            with conn:
                try:
                    serve_connection(conn, session, state)
                except (ConnectionError, EOFError, OSError) as exc:
                    print(f"RVWORKER_F4_ONNX_CONNECTION_END {exc}", flush=True)


if __name__ == "__main__":
    main()
