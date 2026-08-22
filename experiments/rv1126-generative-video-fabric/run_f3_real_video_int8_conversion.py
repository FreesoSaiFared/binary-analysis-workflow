#!/usr/bin/env python3
"""Build the proven F2 graph with an externally prepared real-video calibration corpus.

This proves only that RKNN-Toolkit 1.7.5 accepts and uses the real-video-derived
7-channel dataset for RV1126 INT8 conversion. It is not a quality or timing test.
"""
import argparse
import hashlib
import json
from pathlib import Path

from rknn.api import RKNN


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    model = Path(args.model)
    dataset = Path(args.dataset)
    manifest_path = Path(args.manifest)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_path.read_text())
    rknn_path = out / "rv1126_f2_positive_real_video_calibrated_int8.rknn"

    result = {
        "protocol": "RV1126_F3_REAL_VIDEO_INT8_CONVERSION/1",
        "target": "rv1126",
        "model": str(model),
        "model_sha256": sha256(model),
        "dataset": str(dataset),
        "dataset_sha256": sha256(dataset),
        "calibration_manifest_sha256": sha256(manifest_path),
        "calibration_protocol": manifest.get("protocol"),
        "calibration_status": manifest.get("status"),
        "calibration_samples": manifest.get("sample_count"),
        "quality_claim": False,
        "rv1126_timing_claim": False,
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

    (out / "F3_REAL_VIDEO_INT8_CONVERSION_RESULT.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print("F3_REAL_VIDEO_INT8_CONVERSION_RESULT", json.dumps(result, sort_keys=True))
    ok = (result["load_ret"], result["build_ret"], result["export_ret"], result["rknn_exists"]) == (0, 0, 0, True)
    print("RV1126_F3_REAL_VIDEO_INT8_CONVERTER_PASS" if ok else "RV1126_F3_REAL_VIDEO_INT8_CONVERTER_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
