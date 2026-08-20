#!/usr/bin/env python3
"""Prove the RKNN 1.7.5 INT8 build path for the accepted 7-channel F2 graph.

Synthetic calibration is intentionally a converter-path discriminator only. It is
NOT a claim that the resulting quantization ranges are quality-optimal for video.
"""
import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from rknn.api import RKNN


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def make_calibration(root: Path, count: int = 4) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    dataset = root / "dataset.txt"
    rows = []
    yy, xx = np.mgrid[0:360, 0:640]
    for idx in range(count):
        sample = np.empty((360, 640, 7), dtype=np.uint8)
        sample[..., 0] = (xx + 17 * idx) % 256
        sample[..., 1] = (yy * 2 + 29 * idx) % 256
        sample[..., 2] = ((xx // 2 + yy // 2) + 41 * idx) % 256
        sample[..., 3] = (128 + ((xx % 31) - 15) * (idx + 1)) % 256
        sample[..., 4] = (128 + ((yy % 29) - 14) * (idx + 1)) % 256
        sample[..., 5] = ((xx ^ yy) + 53 * idx) % 256
        sample[..., 6] = ((3 * xx + 5 * yy) + 67 * idx) % 256
        p = root / f"calibration_{idx:02d}.npy"
        np.save(p, sample)
        rows.append(str(p.resolve()))
    dataset.write_text("\n".join(rows) + "\n", encoding="utf-8")
    return dataset


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    model = Path(args.model)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    dataset = make_calibration(out / "calibration")
    rknn_path = out / "rv1126_f2_positive_int8.rknn"

    result = {
        "target": "rv1126",
        "model": str(model),
        "quantization": "INT8 calibration path requested",
        "calibration_kind": "synthetic deterministic 7-channel HWC uint8; converter-path proof only",
        "calibration_samples": 4,
        "load_ret": None,
        "build_ret": None,
        "export_ret": None,
        "rknn_exists": False,
        "rknn_size": 0,
        "rknn_sha256": None,
        "exception": None,
    }

    rknn = RKNN(verbose=True)
    try:
        rknn.config(target_platform=["rv1126"])
        result["load_ret"] = int(rknn.load_onnx(
            model=str(model), inputs=["input"], input_size_list=[[7, 360, 640]], outputs=["rgb"]
        ))
        if result["load_ret"] == 0:
            result["build_ret"] = int(rknn.build(do_quantization=True, dataset=str(dataset)))
        if result["build_ret"] == 0:
            result["export_ret"] = int(rknn.export_rknn(str(rknn_path)))
        if result["export_ret"] == 0 and rknn_path.exists():
            result["rknn_exists"] = True
            result["rknn_size"] = rknn_path.stat().st_size
            result["rknn_sha256"] = sha256(rknn_path)
    except Exception as exc:
        result["exception"] = repr(exc)
    finally:
        try:
            rknn.release()
        except Exception:
            pass

    (out / "F2_INT8_CONVERSION_RESULT.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("F2_INT8_CONVERSION_RESULT", json.dumps(result, sort_keys=True))
    ok = (result["load_ret"], result["build_ret"], result["export_ret"], result["rknn_exists"]) == (0, 0, 0, True)
    print("RV1126_F2_INT8_CONVERTER_PATH_PASS" if ok else "RV1126_F2_INT8_CONVERTER_PATH_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
